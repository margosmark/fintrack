// SharedHousehold — the partner's `/shared` page (M90, docs/43 §13).
//
// Read-only BY CONSTRUCTION: this page imports no mutation from
// api/sharing — there is no write endpoint it could call (the partner's only
// writes in the system are accepting the invite and leaving the link, both
// on /sharing, never here).
//
// PRIVACY MODE (docs/43 §9, M90 D4/A): when the OWNER has privacy mode on,
// every money field in the payloads is JSON `null` — balances, transaction
// amounts, a budget's target. This page never renders a blank: it labels the
// gap ("Amounts hidden"), shows 🔒 where a figure would be, and drops the
// budget bars it has no honest numbers to draw. Names, dates, payees and
// categories are owner-set or structural, and still show.
//
// E2EE (docs/43 §8): every resolve helper is called with `dek = null`,
// UNCONDITIONALLY — never the default argument, which would read THIS
// browser's vault. The partner renders the owner's rows in the locked shape
// whether or not either side is unlocked; their own DEK must never be passed.
//
// `401 sharing_not_linked` (domain state — client.ts keeps it out of the
// refresh dance) renders "No shared household — access was removed" IN PLACE.
// It now reaches us two ways (M92): from the /partner DATA routes, and from
// `loadSharedView`'s own guard on `200 {linked: false}` — same envelope, same
// rendering. The partner is not bounced to /login: their own session is fine.
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Eye, Lock, Users, Wallet } from "lucide-react";
import { ApiError } from "../api/client";
import { loadSharedView } from "../api/sharing";
import type { PartnerTransaction, SharedView } from "../api/sharing";
import { resolveAccountName, resolveAmount, resolvePayee, resolveRawDescription } from "../lib/e2ee";
import { SplitGroupRow, groupSplitRows } from "../components/transactions/SplitGroupRow";
import { formatMoney } from "../lib/format";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { HelpHint } from "../components/ui/HelpHint";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { budgetView, hasCarry, CARRY_HINT, LIMIT_HINT } from "../lib/budgetCarry";
import { useMasked } from "../lib/mask";

/** One feed row after display resolution — `🔒` where ciphertext sits. */
interface DisplayRow {
  id: number;
  date: string;
  currency: string;
  payee: string;
  description: string;
  amount: string;
}

/** A split header after display resolution — same locked rules as a row. */
interface DisplayParent {
  payee: string;
  locked: boolean;
}

function messageOf(err: unknown, fallback: string): string {
  if (err instanceof ApiError || err instanceof Error) return err.message;
  return fallback;
}

/** Resolve every display field with `dek = null` — the docs/43 §8 pin. */
function useLockedDisplay(view: SharedView | undefined) {
  const [names, setNames] = useState<Record<number, string>>({});
  const [rows, setRows] = useState<DisplayRow[]>([]);
  const [parents, setParents] = useState<Record<number, DisplayParent>>({});
  useEffect(() => {
    if (view == null) return;
    let cancelled = false;
    void (async () => {
      const [resolvedNames, resolvedRows, resolvedParents] = await Promise.all([
        Promise.all(
          view.accounts.map(
            async (account) =>
              [account.id, (await resolveAccountName(account, null)).payee] as const,
          ),
        ),
        Promise.all(
          view.transactions.map(async (tx) => ({
            id: tx.id,
            date: tx.date,
            currency: tx.currency,
            payee: (await resolvePayee(tx, null)).payee,
            description: (await resolveRawDescription(tx, null)).payee,
            amount: (await resolveAmount(tx, null)).amount,
          })),
        ),
        // M107 (docs/44 §9): the group header resolves exactly like a row —
        // `dek = null` UNCONDITIONALLY (docs/43 §8), so a masked or ciphered
        // figure is 🔒 in the header too, never a leak.
        Promise.all(
          // Fail open: a payload without the field still renders its rows.
          (view.split_parents ?? []).map(async (parent) => {
            const [payee, amount] = await Promise.all([
              resolvePayee(parent, null),
              resolveAmount(parent, null),
            ]);
            return [parent.id, { payee: payee.payee, locked: payee.locked || amount.locked }] as const;
          }),
        ),
      ]);
      if (cancelled) return;
      setNames(Object.fromEntries(resolvedNames));
      setRows(resolvedRows);
      setParents(Object.fromEntries(resolvedParents));
    })();
    return () => {
      cancelled = true;
    };
  }, [view]);
  return { names, rows, parents };
}

export function SharedHousehold() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const viewQ = useQuery({
    queryKey: ["partner", "shared"],
    queryFn: loadSharedView,
    retry: false,
  });
  const { names, rows, parents } = useLockedDisplay(viewQ.data);
  /** M107 (docs/44 §6.3): the one group whose parts panel is open. */
  const [expandedSplit, setExpandedSplit] = useState<number | null>(null);

  if (viewQ.isLoading) {
    return <p className="text-sm text-muted">Loading shared household…</p>;
  }
  if (viewQ.isError) {
    const err = viewQ.error;
    if (err instanceof ApiError && err.status === 401 && err.code === "sharing_not_linked") {
      return (
        <EmptyState
          icon={<Users className="size-6" aria-hidden />}
          title="No shared household — access was removed"
          hint="Nothing is shared with this account right now. Your own session and data are untouched."
          preview={{
            none: "the partner's populated view needs a second account, which the demo household cannot have",
          }}
          action={{ label: "Go to your dashboard", to: "/dashboard" }}
        />
      );
    }
    return <ErrorPanel message={messageOf(err, "Could not load the shared household")} />;
  }

  const view = viewQ.data;
  if (view == null) return null;
  const { state } = view;
  const sharedCount = state.account_ids.length;

  // M107 (docs/44 §6.3): the feed groups the same way the owner's register
  // does — header where the group first appears, children in `id ASC` inside
  // a labelled region. The feed is read-only BY CONSTRUCTION (no write route
  // exists for a partner), so the panel never fetches and offers no merge.
  const feedUnits = groupSplitRows(view.transactions, view.split_parents ?? []);
  const rowById = new Map(rows.map((row) => [row.id, row]));

  function renderFeedRow(tx: PartnerTransaction): ReactNode {
    const display = rowById.get(tx.id);
    // Still resolving (or already gone) — the locked pass paints nothing new.
    if (display === undefined) return null;
    return (
      <div key={tx.id} className="flex items-center gap-3 px-4 py-2.5">
        <span className="w-24 shrink-0 text-xs text-muted">{display.date}</span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm">{display.payee}</p>
          {display.description !== display.payee && (
            <p className="truncate text-xs text-muted">{display.description}</p>
          )}
        </div>
        <span className="shrink-0 text-sm tabular-nums">
          {/* `🔒` is already the display value — never route it through Number(). */}
          {display.amount === "🔒" ? "🔒" : formatMoney(display.amount, display.currency)}
        </span>
      </div>
    );
  }

  return (
    <motion.section
      variants={staggerContainer(0.06)}
      initial="hidden"
      animate="show"
      className="space-y-6"
    >
      <motion.div variants={fadeUp} className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Shared household</h1>
          <p className="text-sm text-muted">
            Shared with you by{" "}
            <span className="font-medium text-text">{state.owner.name ?? state.owner.email}</span>
            {state.accepted_at != null && (
              <> · since {state.accepted_at.slice(0, 10)}</>
            )}
          </p>
        </div>
        <Users className="size-5 text-muted" aria-hidden />
      </motion.div>

      {/* Scope banner (docs/43 §13): the projection boundary, stated. */}
      <motion.div
        variants={fadeUp}
        className="flex items-center gap-2 rounded-xl border border-border bg-veil/[0.03] px-4 py-3 text-sm"
      >
        <Eye className="size-4 shrink-0 text-muted" aria-hidden />
        <p>
          <span className="font-medium">Shared accounts: {sharedCount}</span> — figures exclude
          everything else
        </p>
      </motion.div>

      {/* Privacy banner (docs/43 §9 / docs/09): the owner's posture, stated
          where the numbers would be — never a silently blank cell. */}
      {state.privacy_mode && (
        <motion.div
          variants={fadeUp}
          className="flex items-center gap-2 rounded-xl border border-border bg-veil/[0.03] px-4 py-3 text-sm"
          role="status"
        >
          <Lock className="size-4 shrink-0 text-muted" aria-hidden />
          <p>
            <span className="font-medium">Amounts hidden</span> — the owner has privacy mode on,
            so balances and amounts are not shared with you. Names, dates and categories still
            show.
          </p>
        </motion.div>
      )}

      {/* Account cards */}
      <motion.div variants={fadeUp} className="space-y-2">
        <h2 className="text-sm font-semibold text-muted">Accounts</h2>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {view.accounts.map((account) => (
            <div key={account.id} className="rounded-xl border border-border p-4">
              <p className="truncate text-sm font-medium">{names[account.id] ?? account.name}</p>
              <p
                className="mt-1 text-lg font-semibold tabular-nums"
                title={
                  account.total_value == null ? "Hidden by the owner’s privacy mode" : undefined
                }
              >
                {/* Never a blank: privacy mode sends `null`, so 🔒 says why. */}
                {account.total_value == null ? "🔒" : formatMoney(account.total_value, account.currency)}
              </p>
            </div>
          ))}
          {view.accounts.length === 0 && (
            <p className="text-sm text-muted">No accounts are shared right now.</p>
          )}
        </div>
      </motion.div>

      {/* Transaction feed — paged to `total` in loadSharedView (docs/43 §13). */}
      <motion.div variants={fadeUp} className="space-y-2">
        <h2 className="text-sm font-semibold text-muted">Transactions</h2>
        <div className="divide-y divide-border rounded-xl border border-border">
          {feedUnits.map((unit) =>
            unit.kind === "group" ? (
              <SplitGroupRow
                key={unit.key}
                group={unit.group}
                variant="div"
                expanded={expandedSplit === unit.group.parent.id}
                onToggleExpand={() =>
                  setExpandedSplit((current) =>
                    current === unit.group.parent.id ? null : unit.group.parent.id,
                  )
                }
                renderRow={renderFeedRow}
                // No partner split route exists (docs/43 §6, docs/44 D13): the
                // panel shows THIS page's children instead of drilling down.
                expandMode="local"
                resolved={parents[unit.group.parent.id]}
              />
            ) : (
              renderFeedRow(unit.tx)
            ),
          )}
          {rows.length === 0 && (
            <p className="px-4 py-3 text-sm text-muted">No transactions in shared accounts.</p>
          )}
        </div>
      </motion.div>

      {/* Budget definitions — progress computed from THESE rows only (D3).
          Under privacy mode there is no figure to derive from (the server
          nulls a budget's target too), so the card says so rather than
          drawing an empty bar. */}
      <motion.div variants={fadeUp} className="space-y-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-muted">Budgets</h2>
          <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">
            shared accounts only
          </span>
        </div>
        {state.privacy_mode && (
          <p className="text-sm text-muted">
            Budget amounts are hidden while the owner has privacy mode on.
          </p>
        )}
        {!state.privacy_mode && (
        <div className="grid gap-3 sm:grid-cols-2">
          {view.budgetProgress.map((budget) => {
            // M110 (docs/43 §9): `carried_in` crosses the seam as its own
            // field, so the partner's card shows the same layers the owner's
            // does — assigned / carried-in / spendable, one at a time.
            const carry = budgetView(budget);
            const showCarry = hasCarry(carry);
            const pct = budget.pct_used;
            return (
            <div key={budget.id} className="rounded-xl border border-border p-4">
              <div className="flex items-baseline justify-between gap-2">
                <p className="truncate text-sm font-medium">{budget.category_name}</p>
                <span className="text-xs text-muted">{budget.period}</span>
              </div>
              <p className="mt-1 text-sm tabular-nums">
                {formatMoney(budget.spent, "CAD")} of {formatMoney(carry.pool.toFixed(2), "CAD")}
                {showCarry && <span className="text-xs text-muted"> spendable</span>}
              </p>
              {showCarry && carry.carried !== null && (
                <p className={`text-xs ${carry.debt ? "text-danger" : "text-gain"}`}>
                  {carry.debt ? "−" : "+"}{formatMoney(Math.abs(carry.carried).toFixed(2), "CAD")} carried in
                </p>
              )}
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-veil">
                <div
                  className={`h-full rounded-full ${pct === null ? "bg-danger" : "bg-accent"}`}
                  style={{
                    // Only the BAR clamps (M65); the label reports the truth.
                    width: `${Math.min(pct ?? 100, 100)}%`,
                  }}
                />
              </div>
              <p className="mt-1 text-xs text-muted">
                {pct === null ? (
                  <span className="text-danger">over budget · </span>
                ) : (
                  <>
                    {Math.round(pct)}% used
                    {/* M65 honest-%: true ratio, only the bar clamps. */}
                    <HelpHint text={showCarry ? CARRY_HINT : LIMIT_HINT} />
                    {" · "}
                  </>
                )}
                {formatMoney(budget.remaining, "CAD")} left
              </p>
            </div>
            );
          })}
          {view.budgetProgress.length === 0 && (
            <p className="text-sm text-muted">No budgets cover the shared accounts.</p>
          )}
        </div>
        )}
      </motion.div>

      <motion.p variants={fadeUp} className="flex items-center gap-2 text-xs text-muted">
        <Wallet className="size-3.5" aria-hidden />
        Read-only view — nothing here can change the shared data.
      </motion.p>
    </motion.section>
  );
}
