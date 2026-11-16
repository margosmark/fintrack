// Cash-flow Sankey engine (M77, docs/12 §Sankey cash-flow) — the browser twin
// of `app/services/reports.py::cashflow_sankey`.
//
// M160-R2 (t_85311c34): `SankeyPanel` reads the route now
// (`api/reports.ts::getCashflowSankey`), so nothing in production runs this
// compute anymore. It stays — M160-R1 ruling 3 kept it out of the deletion
// list — as the reference implementation the route was written against, and
// because its report shapes below are the wire contract the chart renders
// (`CashflowSankey.tsx`, `api/reports.ts`). The frozen golden
// `tests/fixtures/m77-vectors.json` is replayed against the ROUTE by
// `backend/tests/test_m160_replay_m77.py`, which is what keeps the two
// implementations honest now that `tests/m77Parity.test.ts` (deleted by
// M160/P2-5) no longer diffs them.
//
// Three columns only: sources -> one `Money in` hub -> destinations. Population
// and signs are `cashflowReport`'s exactly (docs/12 §3): positive transaction
// CAD + trade cash inflows are sources; negative transaction CAD + trade cash
// outflows + buy/sell/drip commissions are destinations. Trade principal is not
// income or expense, but `buy`/`drip` principal IS the money that left the cash
// pool for securities, so it lands on `Investments`; the leftover lands on
// `Savings` (or, when outgoings exceed income, a balancing
// `Transfers & savings in` source).
//
// Every converted row is quantized to cents BEFORE it is summed, so the sums
// are exact integers in cents and the graph conserves to the cent. All of that
// runs on the `lib/dec.ts` BigInt fixed-point helpers — no float touches a
// node amount, because the frozen golden demanded string equality with the
// server's Decimal output.

import type { Category } from "../api/categories";
import type { Trade } from "../api/trades";
import type { Transaction } from "../api/transactions";
import {
  REPORTING,
  countOnOrBefore,
  dabs,
  dadd,
  dcents,
  dec,
  disZero,
  dmul,
  dneg,
  dstr,
  dzero,
  quantizeCents,
  type Dec,
} from "./dec";

// --- report shapes (docs/12 §Endpoints — the server's response_model) --------

export type SankeyColumn = "source" | "total" | "destination";

export interface SankeyNode {
  id: string;
  column: SankeyColumn;
  label: string;
  amount_cad: string;
  group: string | null;
}

export interface SankeyLink {
  source: string;
  target: string;
  amount_cad: string;
}

export interface SankeyTotals {
  income_cad: string;
  expenses_cad: string;
  invested_cad: string;
  saved_cad: string;
  drawn_cad: string;
  money_in_cad: string;
}

export interface SankeyReport {
  from: string;
  to: string;
  columns: SankeyColumn[];
  nodes: SankeyNode[];
  links: SankeyLink[];
  totals: SankeyTotals;
}

export interface SankeyInputs {
  txns: readonly Transaction[];
  trades: readonly Trade[];
  /** account_id → ISO currency code. */
  accounts: ReadonlyMap<number, { currency: string }>;
  /** category_id → category (for the top-level roll-up). */
  categories: ReadonlyMap<number, Category>;
  /** base currency → fx rates for CAD, ascending by date (cache-only). */
  rates: ReadonlyMap<string, ReadonlyArray<{ date: string; rate: string }>>;
}

// --- constants mirroring app/services/reports.py -----------------------------

const UNCATEGORIZED = "Uncategorized";

const INFLOW_LABELS: Readonly<Record<string, string>> = {
  dividend: "Dividends",
  distribution: "Distributions",
  return_of_capital: "Return of capital",
  contribution: "Contributions",
};
const OUTFLOW_LABELS: Readonly<Record<string, string>> = {
  fee: "Fees",
  withdrawal: "Withdrawals",
};
const COMMISSION_LABEL = "Trading commissions";
const INVESTED_ID = "dst:invested";
const INVESTED_LABEL = "Investments";
const SAVED_ID = "dst:saved";
const SAVED_LABEL = "Savings";
const DRAWN_ID = "src:drawn";
const DRAWN_LABEL = "Transfers & savings in";
const TOTAL_ID = "total:in";
const TOTAL_LABEL = "Money in";

const CASH_INFLOW = new Set(["dividend", "distribution", "return_of_capital", "contribution"]);
const CASH_OUTFLOW = new Set(["fee", "withdrawal"]);
const COMMISSION = new Set(["buy", "sell", "drip"]);
const BUY_LIKE = new Set(["buy", "drip"]);

// --- helpers -----------------------------------------------------------------

/** Newest cached base→CAD rate on or before `day`; null when unresolved. */
function rateDec(
  rates: ReadonlyMap<string, ReadonlyArray<{ date: string; rate: string }>>,
  base: string,
  day: string,
): Dec | null {
  if (base === REPORTING) return dec("1"); // base == quote → rate 1
  const rows = rates.get(base);
  if (!rows) return null;
  const i = countOnOrBefore(rows, day);
  return i === 0 ? null : dec(rows[i - 1].rate);
}

/** Python's `str` compares by CODE POINT; JS `<` compares UTF-16 units, so
 *  equal-amount nodes whose labels contain astral characters could order the
 *  two implementations differently. Compare code points explicitly. */
function cmpLabel(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  const n = Math.min(x.length, y.length);
  for (let i = 0; i < n; i += 1) {
    const cx = x[i].codePointAt(0) ?? 0;
    const cy = y[i].codePointAt(0) ?? 0;
    if (cx !== cy) return cx < cy ? -1 : 1;
  }
  return x.length - y.length;
}

/** `_top_ancestor` (docs/12 §2): walk to the root, roll-up the whole way. */
function topAncestor(categories: ReadonlyMap<number, Category>, id: number): number {
  let current = id;
  for (;;) {
    const cat = categories.get(current);
    if (!cat) throw new Error(`unknown category: ${current}`);
    if (cat.parent_id === null) return current;
    current = cat.parent_id;
  }
}

// --- engine ------------------------------------------------------------------

export function cashflowSankey(
  from: string,
  to: string,
  inputs: SankeyInputs,
): SankeyReport {
  const srcAmount = new Map<string, Dec>();
  const srcLabel = new Map<string, string>();
  const srcGroup = new Map<string, string>();
  const dstAmount = new Map<string, Dec>();
  const dstLabel = new Map<string, string>();
  const dstGroup = new Map<string, string>();

  const credit = (
    income: boolean,
    key: string,
    label: string,
    group: string,
    amount: Dec,
  ): void => {
    const amounts = income ? srcAmount : dstAmount;
    if (disZero(amount)) return; // zero-valued rows never become a node
    amounts.set(key, dadd(amounts.get(key) ?? dzero(), amount));
    (income ? srcLabel : dstLabel).set(key, label);
    (income ? srcGroup : dstGroup).set(key, group);
  };

  const toCad = (amount: Dec, currency: string, day: string): Dec | null => {
    const rate = rateDec(inputs.rates, currency, day);
    return rate === null ? null : quantizeCents(dmul(amount, rate));
  };

  // --- transactions (committed rows loaded by the caller, date-bounded) ------
  for (const txn of inputs.txns) {
    if (txn.cad_amount === null || txn.date < from || txn.date > to) continue;
    const cad = dec(txn.cad_amount);
    if (disZero(cad)) continue; // unresolved/zero CAD excluded (docs/12 §3)
    const income = cad.v > 0n;
    let nodeId: string;
    let label: string;
    if (txn.category_id === null) {
      // the server's `"null"` node id (JSON, not Python's None) — the two must
      // match byte-for-byte, so the literal is part of the contract.
      nodeId = "null";
      label = UNCATEGORIZED;
    } else {
      const root = topAncestor(inputs.categories, txn.category_id);
      nodeId = String(root);
      label = inputs.categories.get(root)?.name ?? UNCATEGORIZED;
    }
    credit(
      income,
      `${income ? "src" : "dst"}:cat:${nodeId}`,
      label,
      income ? "income" : "spent",
      income ? cad : dneg(cad),
    );
  }

  // --- trades ---------------------------------------------------------------
  for (const trade of inputs.trades) {
    if (trade.date < from || trade.date > to) continue;
    const account = inputs.accounts.get(trade.account_id);
    if (account === undefined) continue;
    const price = dec(trade.price_native);
    // M132: the row's own currency (docs/02); `null` reads as the account's.
    const rowCurrency = trade.currency ?? account.currency;

    if (CASH_INFLOW.has(trade.action)) {
      const cad = toCad(price, rowCurrency, trade.date);
      if (cad !== null) {
        credit(true, `src:trade:${trade.action}`, INFLOW_LABELS[trade.action] ?? trade.action, "income", cad);
      }
    } else if (CASH_OUTFLOW.has(trade.action)) {
      // M131: the row stores the broker's native sign (negative); the sankey
      // shows the magnitude leaving, so abs() it.
      const cad = toCad(dabs(price), rowCurrency, trade.date);
      if (cad !== null) {
        credit(false, `dst:trade:${trade.action}`, OUTFLOW_LABELS[trade.action] ?? trade.action, "spent", cad);
      }
    }
    if (COMMISSION.has(trade.action)) {
      const fee = toCad(dec(trade.commission || "0"), rowCurrency, trade.date);
      if (fee !== null) {
        credit(false, "dst:trade:commission", COMMISSION_LABEL, "spent", fee);
      }
    }
    const quantity = dec(trade.quantity);
    if (BUY_LIKE.has(trade.action) && !disZero(quantity)) {
      const principal = toCad(dmul(quantity, price), rowCurrency, trade.date);
      if (principal !== null) {
        credit(false, INVESTED_ID, INVESTED_LABEL, "invested", principal);
      }
    }
  }

  // --- totals + the balancing node -----------------------------------------
  let incomeTotal = dzero();
  for (const amount of srcAmount.values()) incomeTotal = dadd(incomeTotal, amount);
  let expensesTotal = dzero();
  for (const [key, amount] of dstAmount) {
    if (dstGroup.get(key) === "spent") expensesTotal = dadd(expensesTotal, amount);
  }
  const investedTotal = dstAmount.get(INVESTED_ID) ?? dzero();
  const surplus = dadd(dadd(incomeTotal, dneg(expensesTotal)), dneg(investedTotal));
  const savedTotal = surplus.v > 0n ? surplus : dzero();
  const drawnTotal = surplus.v < 0n ? dneg(surplus) : dzero();
  if (!disZero(savedTotal)) {
    dstAmount.set(SAVED_ID, savedTotal);
    dstLabel.set(SAVED_ID, SAVED_LABEL);
    dstGroup.set(SAVED_ID, "saved");
  }
  if (!disZero(drawnTotal)) {
    srcAmount.set(DRAWN_ID, drawnTotal);
    srcLabel.set(DRAWN_ID, DRAWN_LABEL);
    srcGroup.set(DRAWN_ID, "drawn");
  }
  const moneyIn = dadd(incomeTotal, drawnTotal);

  const byAmountDesc = (amounts: ReadonlyMap<string, Dec>, labels: ReadonlyMap<string, string>) =>
    (a: string, b: string): number => {
      const av = amounts.get(a) ?? dzero();
      const bv = amounts.get(b) ?? dzero();
      if (av.v !== bv.v || av.s !== bv.s) {
        // magnitude compare (all values share scale 2, but keep it general)
        const scale = Math.max(av.s, bv.s);
        const an = av.v * 10n ** BigInt(scale - av.s);
        const bn = bv.v * 10n ** BigInt(scale - bv.s);
        if (an !== bn) return an > bn ? -1 : 1;
      }
      const byLabel = cmpLabel(labels.get(a) ?? "", labels.get(b) ?? "");
      return byLabel !== 0 ? byLabel : a < b ? -1 : a > b ? 1 : 0;
    };

  const sources = [...srcAmount.keys()].sort(byAmountDesc(srcAmount, srcLabel));
  const destinations = [...dstAmount.keys()].sort(byAmountDesc(dstAmount, dstLabel));

  const nodes: SankeyNode[] = sources.map((key) => ({
    id: key,
    column: "source",
    label: srcLabel.get(key) ?? "",
    amount_cad: dstr(srcAmount.get(key) ?? dzero()),
    group: srcGroup.get(key) ?? null,
  }));
  if (!disZero(moneyIn) || sources.length > 0 || destinations.length > 0) {
    nodes.push({
      id: TOTAL_ID,
      column: "total",
      label: TOTAL_LABEL,
      amount_cad: dstr(moneyIn),
      group: null,
    });
  }
  for (const key of destinations) {
    nodes.push({
      id: key,
      column: "destination",
      label: dstLabel.get(key) ?? "",
      amount_cad: dstr(dstAmount.get(key) ?? dzero()),
      group: dstGroup.get(key) ?? null,
    });
  }

  const links: SankeyLink[] = [
    ...sources.map((key) => ({
      source: key,
      target: TOTAL_ID,
      amount_cad: dstr(srcAmount.get(key) ?? dzero()),
    })),
    ...destinations.map((key) => ({
      source: TOTAL_ID,
      target: key,
      amount_cad: dstr(dstAmount.get(key) ?? dzero()),
    })),
  ];

  return {
    from,
    to,
    columns: ["source", "total", "destination"],
    nodes,
    links,
    totals: {
      income_cad: dcents(incomeTotal),
      expenses_cad: dcents(expensesTotal),
      invested_cad: dcents(investedTotal),
      saved_cad: dcents(savedTotal),
      drawn_cad: dcents(drawnTotal),
      money_in_cad: dcents(moneyIn),
    },
  };
}
