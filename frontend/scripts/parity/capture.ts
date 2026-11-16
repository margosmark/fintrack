/**
 * Golden-vector capture for the M48 E2EE parity ports (docs/33 Phase 3: S3, S4).
 *
 * Writes `<fixture>-vectors.json` (inputs + server JSON) to frontend/tests/fixtures/.
 * Run from anywhere; paths resolve relative to this file.
 *
 *   node frontend/scripts/parity/capture.ts --slice s3        # rules/subscriptions/budgets
 *   node frontend/scripts/parity/capture.ts --slice s4        # capital-gains/acb
 *   node frontend/scripts/parity/capture.ts --slice s5        # fuzzy transaction search
 *   node frontend/scripts/parity/capture.ts --slice all
 *   node frontend/scripts/parity/capture.ts --slice s3 --base-url http://localhost:8000/api/v1
 *
 * Rules of the road (fin-tracker skill + AGENTS.md):
 *  - tokens are never printed or written to disk;
 *  - every capture asserts the status it expects, so a broken response can
 *    never be recorded as a golden;
 *  - S3's POST /rules/preview is captured from a THROWAWAY tenant registered
 *    and deleted in the same run, because the demo tenant is method-blocked
 *    (403 demo_account_restricted) on every non-GET route even when the route
 *    never writes. Read-only GET goldens come from the demo tenant.
 *  - S5's `GET /transactions?q=` goldens must be captured BEFORE M48-S5
 *    removes the parameter: the capture asserts its no-match sentinel still
 *    ranks to `total: 0` and stops with an explicit "param was removed" error
 *    once the server ignores `q` (frozen fixture, same rule as S3's routes).
 */

import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Api, demoToken, request, requireAccessToken } from "./http.ts";
import type { Dataset, Fixture, FixtureMeta, Json, ParityCase } from "./types.ts";

const DEFAULT_BASE_URL = "https://fintrac.princessandmark.com/api/v1";
const FIXTURE_DIR = fileURLToPath(new URL("../../tests/fixtures/", import.meta.url));
const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url));

/** `today_et()` (America/Toronto) as YYYY-MM-DD — the server's "today". */
function etDate(date: Date): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Toronto",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function headCommit(): string {
  try {
    return execFileSync("git", ["rev-parse", "HEAD"], { cwd: REPO_ROOT, encoding: "utf8" }).trim();
  } catch {
    return "unknown";
  }
}

function say(message: string): void {
  console.log(message);
}

// --------------------------------------------------------------------------
// seeds for the throwaway tenant (deterministic rows -> deterministic goldens)
// --------------------------------------------------------------------------

interface SeedTransaction {
  date: string;
  raw_description: string;
  normalized_payee: string;
  /** Decimal as string; the column is Numeric(18,2) so the API echoes 2dp. */
  amount_native: string;
  /** true -> give the row a default category (pins only_unassigned filtering). */
  assign?: boolean;
}

/**
 * Fixed 2025 dates, signed amounts (outflow negative — docs/02), and edges the
 * rules port must reproduce exactly: inclusive amount_between bounds, sign
 * handling for amount_lt/amount_gt, a 0.00 row, and assigned vs unassigned rows
 * so `assigned_skipped` is non-zero on both sides of the filter.
 */
const SEED_TRANSACTIONS: SeedTransaction[] = [
  { date: "2025-01-03", raw_description: "NETFLIX.COM", normalized_payee: "NETFLIX.COM", amount_native: "-15.99" },
  { date: "2025-02-03", raw_description: "NETFLIX.COM", normalized_payee: "NETFLIX.COM", amount_native: "-15.99" },
  { date: "2025-03-03", raw_description: "NETFLIX.COM", normalized_payee: "NETFLIX.COM", amount_native: "-15.99", assign: true },
  { date: "2025-01-05", raw_description: "GROCERY MART #12", normalized_payee: "GROCERY MART", amount_native: "-45.20" },
  { date: "2025-01-18", raw_description: "grocery mart west", normalized_payee: "GROCERY MART WEST", amount_native: "-0.01" },
  { date: "2025-01-20", raw_description: "TIM HORTONS", normalized_payee: "TIM HORTONS", amount_native: "0.00" },
  { date: "2025-02-01", raw_description: "PAYROLL DEPOSIT", normalized_payee: "PAYROLL", amount_native: "2500.00", assign: true },
  { date: "2025-02-10", raw_description: "STORE REFUND", normalized_payee: "STORE REFUND", amount_native: "10.00" },
  { date: "2025-02-15", raw_description: "ODEON CINEMAS", normalized_payee: "ODEON CINEMAS", amount_native: "-12.50" },
  { date: "2025-03-01", raw_description: "SHELL STATION", normalized_payee: "SHELL STATION", amount_native: "-50.00" },
  { date: "2025-03-05", raw_description: "CORNER CAFE", normalized_payee: "CORNER CAFE", amount_native: "-10.00" },
  { date: "2025-03-08", raw_description: "CORNER DINER", normalized_payee: "CORNER DINER", amount_native: "-10.01" },
  { date: "2025-03-09", raw_description: "MONTHLY GYM", normalized_payee: "MONTHLY GYM", amount_native: "-100.00" },
  { date: "2025-03-10", raw_description: "HOTEL STAY", normalized_payee: "HOTEL STAY", amount_native: "-100.01" },
  { date: "2025-03-11", raw_description: "UTILITY BILL", normalized_payee: "UTILITY BILL", amount_native: "-99.99" },
  { date: "2025-03-12", raw_description: "MISC SHOP", normalized_payee: "MISC SHOP", amount_native: "-30.00" },
  { date: "2025-03-13", raw_description: "BONUS PAY", normalized_payee: "BONUS PAY", amount_native: "40.00", assign: true },
  { date: "2025-03-14", raw_description: "MID SHOP", normalized_payee: "MID SHOP", amount_native: "30.00" },
  { date: "2025-03-15", raw_description: "HI SHOP", normalized_payee: "HI SHOP", amount_native: "50.00" },
  { date: "2025-03-16", raw_description: "JUSTOVER SHOP", normalized_payee: "JUSTOVER SHOP", amount_native: "50.01" },
  { date: "2025-03-17", raw_description: "JUSTUNDER SHOP", normalized_payee: "JUSTUNDER SHOP", amount_native: "9.99" },
];

interface PreviewSpec {
  id: string;
  body: Record<string, Json>;
  expectStatus: number;
}

/**
 * One preview golden per matcher/scope branch the S3 port must reproduce.
 * Bodies mirror what RuleDryRun.tsx sends (only_unassigned always present
 * there) plus one omitted-field case to pin the schema default, and two error
 * cases so the client engine must return the SAME {detail, code} envelope.
 */
const PREVIEW_SPECS: PreviewSpec[] = [
  { id: "rules.preview.contains-unassigned", body: { match_type: "contains", pattern: "NETFLIX", only_unassigned: true }, expectStatus: 200 },
  { id: "rules.preview.contains-all", body: { match_type: "contains", pattern: "NETFLIX", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.contains-default-scope", body: { match_type: "contains", pattern: "NETFLIX" }, expectStatus: 200 },
  { id: "rules.preview.exact-payee", body: { match_type: "exact_payee", pattern: "NETFLIX.COM", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.regex-prefix", body: { match_type: "regex", pattern: "^grocery", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.amount-equals", body: { match_type: "amount_equals", pattern: "10.00", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.amount-gt", body: { match_type: "amount_gt", pattern: "100", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.amount-lt-signed", body: { match_type: "amount_lt", pattern: "0", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.amount-between-inclusive", body: { match_type: "amount_between", pattern: "10|50", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.range-january", body: { match_type: "contains", pattern: "GROCERY MART", from: "2025-01-01", to: "2025-01-31", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.limit-cap", body: { match_type: "amount_lt", pattern: "0", only_unassigned: false, limit: 3 }, expectStatus: 200 },
  { id: "rules.preview.no-match", body: { match_type: "contains", pattern: "NONEXISTENT PAYEE", only_unassigned: false }, expectStatus: 200 },
  { id: "rules.preview.err-amount-between", body: { match_type: "amount_between", pattern: "10-50" }, expectStatus: 422 },
  { id: "rules.preview.err-regex-invalid", body: { match_type: "regex", pattern: "(" }, expectStatus: 422 },
];

// --------------------------------------------------------------------------
// shared helpers
// --------------------------------------------------------------------------

function expect(res: { status: number }, what: string, want = 200): void {
  if (res.status !== want) {
    throw new Error(`${what} returned ${res.status} (expected ${want})`);
  }
}

/** Non-CAD currencies any collection touches (account.currency, security.listing_currency). */
function nonCadCurrencies(collections: Json[][]): string[] {
  const out = new Set<string>();
  for (const rows of collections) {
    for (const row of rows) {
      if (row === null || typeof row !== "object" || Array.isArray(row)) continue;
      const rec = row as Record<string, Json>;
      const value = rec.currency ?? rec.listing_currency;
      if (typeof value === "string" && value !== "CAD") out.add(value);
    }
  }
  return [...out].sort();
}

/** Cache-only fx range per base (docs/12: a report render never fetches). */
async function fetchFx(api: Api, bases: string[], end: string): Promise<Record<string, Json>> {
  const out: Record<string, Json> = {};
  for (const base of bases) {
    const res = await api.get(
      `/fx?base=${base}&quote=CAD&start_date=2000-01-01&end_date=${end}&fetch=false`,
    );
    expect(res, `GET /fx base=${base}`);
    const items = (res.body as { items?: Json } | null)?.items;
    if (!Array.isArray(items)) throw new Error(`GET /fx base=${base} returned no items[]`);
    out[base] = items;
  }
  return out;
}

/** Budget definitions = the golden minus the keys the server computes. */
function budgetDefinitions(body: Json): Json[] {
  if (!Array.isArray(body)) throw new Error("GET /budgets did not return an array");
  return body.map((row) => {
    const rec = row as Record<string, Json>;
    return {
      id: rec.id,
      user_id: rec.user_id,
      category_id: rec.category_id,
      amount: rec.amount,
      period: rec.period,
      // M110: `carry` is a DEFINITION column (docs/08) — it rides with the
      // definition; `carried_in` is computed and stays out of this input.
      carry: rec.carry,
    };
  });
}

// --------------------------------------------------------------------------
// S3: demo GET goldens + throwaway-tenant rule-preview goldens
// --------------------------------------------------------------------------

async function capturePreviewTenant(baseUrl: string): Promise<{ dataset: Dataset; cases: ParityCase[] }> {
  const email = `parity-capture-${Date.now()}-${randomBytes(4).toString("hex")}@example.com`;
  const password = `parity-${randomBytes(18).toString("hex")}`;
  say(`  register throwaway tenant ${email}`);
  const register = await request("POST", `${baseUrl}/auth/register`, { body: { email, password } });
  const token = requireAccessToken(register, "POST /auth/register");
  const api = new Api(baseUrl, token);

  try {
    const categories = await api.paged("/categories");
    const first = categories[0];
    const assignedCategory =
      first !== null && typeof first === "object" && !Array.isArray(first)
        ? (first as Record<string, Json>).id
        : null;
    if (typeof assignedCategory !== "number") {
      throw new Error("throwaway tenant got no default categories");
    }

    const account = await api.post("/accounts", {
      name: "parity-seed",
      type: "chequing",
      currency: "CAD",
      starting_balance: "0",
    });
    if (account.status !== 201 && account.status !== 200) {
      throw new Error(`POST /accounts returned ${account.status}`);
    }
    const accountId = (account.body as Record<string, Json> | null)?.id;
    if (typeof accountId !== "number") throw new Error("POST /accounts returned no id");

    for (const seed of SEED_TRANSACTIONS) {
      const body: Record<string, Json> = {
        account_id: accountId,
        date: seed.date,
        raw_description: seed.raw_description,
        normalized_payee: seed.normalized_payee,
        amount_native: seed.amount_native,
        currency: "CAD",
      };
      if (seed.assign === true) body.category_id = assignedCategory;
      const res = await api.post("/transactions", body);
      if (res.status !== 201 && res.status !== 200) {
        throw new Error(`POST /transactions ${seed.date} ${seed.normalized_payee} returned ${res.status}`);
      }
    }
    say(`  seeded ${SEED_TRANSACTIONS.length} transactions`);

    const transactions = await api.paged("/transactions");
    const cats = await api.paged("/categories");
    if (transactions.length !== SEED_TRANSACTIONS.length) {
      throw new Error(`seeded ${SEED_TRANSACTIONS.length} rows but GET /transactions returned ${transactions.length}`);
    }

    const cases: ParityCase[] = [];
    for (const spec of PREVIEW_SPECS) {
      const res = await api.post("/rules/preview", spec.body);
      if (res.status === 405) {
        // M48-S3 deleted the route: fail HERE with the reason instead of a
        // bare "expected 200" — the S3 fixture is frozen at meta.base_commit
        // and must be re-captured against a pre-M48-S3 stack.
        throw new Error(
          `${spec.id}: POST /rules/preview returned 405 — the route was removed in ` +
            "M48-S3 (preview computes client-side now), so the S3 rule-preview " +
            "goldens are frozen at meta.base_commit and cannot be re-captured.",
        );
      }
      if (res.status !== spec.expectStatus) {
        throw new Error(
          `${spec.id}: POST /rules/preview returned ${res.status} (expected ${spec.expectStatus})`,
        );
      }
      cases.push({
        id: spec.id,
        dataset: "scratch",
        request: { method: "POST", path: "/rules/preview", body: spec.body },
        expected: { status: res.status, body: res.body },
      });
    }
    say(`  captured ${cases.length} rule-preview goldens`);

    return { dataset: { transactions, categories: cats }, cases };
  } finally {
    const del = await request("DELETE", `${baseUrl}/users/me`, {
      token,
      body: { current_password: password },
    });
    if (del.status === 204) {
      say(`  throwaway tenant deleted (204): ${email}`);
    } else {
      // Loud: leftover prod rows are a real problem, never a footnote.
      say(`  WARNING: throwaway tenant deletion returned ${del.status} — remove manually: ${email}`);
    }
  }
}

async function captureS3(baseUrl: string, today: string): Promise<Fixture> {
  const demoTokenValue = await demoToken(baseUrl);
  const api = new Api(baseUrl, demoTokenValue);

  say("  demo tenant: fetching inputs");
  const transactions = await api.paged("/transactions");
  const categories = await api.paged("/categories");
  const rules = await api.paged("/rules");
  const accounts = await api.paged("/accounts");
  const securities = await api.paged("/securities");
  const fx = await fetchFx(api, nonCadCurrencies([accounts, securities]), today);

  say("  demo tenant: fetching goldens");
  const budgetsRes = await api.get("/budgets");
  expect(budgetsRes, "GET /budgets");
  const subsRes = await api.get("/subscriptions");
  if (subsRes.status === 404) {
    // M48-S3 unmounted the router — same reason as the preview 405 above.
    throw new Error(
      "GET /subscriptions returned 404 — the route was removed in M48-S3 " +
        "(detection computes client-side now), so the S3 subscription golden " +
        "is frozen at meta.base_commit and cannot be re-captured.",
    );
  }
  expect(subsRes, "GET /subscriptions");

  const cases: ParityCase[] = [
    {
      id: "budgets.progress",
      dataset: "demo",
      request: { method: "GET", path: "/budgets" },
      expected: { status: budgetsRes.status, body: budgetsRes.body },
    },
    {
      id: "subscriptions.detect",
      dataset: "demo",
      request: { method: "GET", path: "/subscriptions" },
      expected: { status: subsRes.status, body: subsRes.body },
    },
  ];

  say("  throwaway tenant: rule previews (demo POSTs are method-blocked)");
  const scratch = await capturePreviewTenant(baseUrl);
  cases.push(...scratch.cases);

  const budgets = budgetsRes.body;
  const notes = [
    "Time-dependent goldens: GET /budgets and GET /subscriptions derive their window from today_et() (America/Toronto). Inject meta.captured_at_et, not the machine's today, or the numbers move.",
    "datasets.demo.* are the demo tenant (read-only GETs). datasets.scratch.* are a throwaway tenant registered + deleted during capture: POST /rules/preview is method-blocked for the demo tenant (403 demo_account_restricted) although the route never writes (docs/31 preview is dry-run only).",
    "datasets.demo.budgets = budget definitions extracted from the GET /budgets golden (spent/remaining/pct_used/category_name stripped); the server returns definitions and computed progress in one payload.",
    "reporting_currency is meta.reporting_currency (get_settings().reporting_currency, not exposed by any endpoint); lib/dec.ts pins REPORTING='CAD' the same way.",
    "docs/08 §subscriptions pins `occurrence_count`; the live response returns `occurrences` plus `last_date`, which docs/08 does not list. Reported, not reconciled (AGENTS §3).",
    "docs/08 §rules/preview response omits `assigned_skipped`, which docs/31 pins and the server returns.",
    "docs/08 §rules/preview states limit default 25 but not the schema cap (le=100 -> 422 above 100).",
    "GET /rules is captured as an INPUT (datasets.demo.rules), not as a case: rule definitions are fetched data, not compute — S3 moves the MATCHING to the client.",
  ];

  const meta: FixtureMeta = {
    slice: "s3",
    base_url: baseUrl,
    tenant: "demo (GET goldens) + throwaway (rule previews)",
    base_commit: headCommit(),
    captured_at_utc: new Date().toISOString(),
    captured_at_et: today,
    reporting_currency: "CAD",
    capture_command: "node frontend/scripts/parity/capture.ts --slice s3",
    diff_command: "node frontend/scripts/parity/capture.ts --slice s3 && node frontend/scripts/parity/parity.ts --slice s3",
    notes,
  };

  return {
    meta,
    datasets: {
      demo: {
        transactions,
        categories,
        rules,
        budgets: budgetDefinitions(budgets),
        fx,
      },
      scratch: scratch.dataset,
    },
    cases,
  };
}

// --------------------------------------------------------------------------
// S4: demo capital-gains + ACB goldens
// --------------------------------------------------------------------------

/** Mirror Taxes.tsx: current year down to 2014. */
function taxYears(now: Date): number[] {
  const current = Number(
    new Intl.DateTimeFormat("en-US", { timeZone: "America/Toronto", year: "numeric" }).format(now),
  );
  const years: number[] = [];
  for (let y = current; y >= 2014; y -= 1) years.push(y);
  return years;
}

async function captureS4(baseUrl: string, today: string): Promise<Fixture> {
  const token = await demoToken(baseUrl);
  const api = new Api(baseUrl, token);

  say("  demo tenant: fetching inputs");
  const trades = await api.paged("/trades?committed_only=true");
  const accounts = await api.paged("/accounts");
  const securities = await api.paged("/securities");
  const prices = await api.paged("/prices");
  const fx = await fetchFx(api, nonCadCurrencies([accounts, securities]), today);

  say("  demo tenant: fetching goldens");
  const cases: ParityCase[] = [];
  const years = taxYears(new Date());
  let yearsWithItems = 0;
  let yearsWithDenials = 0;
  for (const year of years) {
    const res = await api.get(`/reports/capital-gains?year=${year}`);
    expect(res, `GET /reports/capital-gains?year=${year}`);
    const body = res.body as Record<string, Json> | null;
    const items = Array.isArray(body?.items) ? body.items.length : 0;
    const denials = Array.isArray(body?.denials) ? body.denials.length : 0;
    if (items > 0) yearsWithItems += 1;
    if (denials > 0) yearsWithDenials += 1;
    cases.push({
      id: `capital-gains.${year}`,
      dataset: "demo",
      request: { method: "GET", path: `/reports/capital-gains?year=${year}` },
      expected: { status: res.status, body: res.body },
    });
  }

  // Taxes.tsx calls listAcbPools({ limit: 200 }) -> "/acb?limit=200".
  // ONE fetch serves both roles (PM decision A, 2026-09-27): the case golden
  // AND `datasets.demo.acbPools`. The engine computes the derivable fields and
  // takes only the two database-assigned columns (`id`, `updated_at`) from
  // that frozen fetch — same precedent as S3's `datasets.demo.rules`.
  const acbRes = await api.get("/acb?limit=200");
  expect(acbRes, "GET /acb?limit=200");
  cases.push({
    id: "acb.pools",
    dataset: "demo",
    request: { method: "GET", path: "/acb?limit=200" },
    expected: { status: acbRes.status, body: acbRes.body },
  });

  const notes = [
    `Coverage on this tenant: ${yearsWithItems}/${years.length} tax years have disposal rows, ${yearsWithDenials}/${years.length} have superficial-loss denials. If S4 needs denial/ROC coverage the demo does not carry, extend the capture with a synthetic tenant (same pattern as the S3 previews) rather than weakening the comparison.`,
    "fx captured cache-only (fetch=false), 2000-01-01 .. meta.captured_at_et — reports never fetch (docs/07/docs/12).",
    "prices/trades/accounts/securities are the S2b loaders' populations: GET /trades?committed_only=true, 200/page, merged.",
    "reporting_currency is meta.reporting_currency (get_settings().reporting_currency, not exposed by any endpoint).",
    "GET /acb is captured exactly as Taxes.tsx calls it (`?limit=200`, no offset).",
    "`datasets.demo.acbPools` is that same GET /acb response kept as an INPUT: `acb_pools.id` (serial) and `updated_at` (last rebuild_user_pools write) are assigned by Postgres and re-issued by any demo-exempt POST /acb/rebuild, so they cannot be derived from trades. The S4 engine computes the other six fields per row plus order/total/limit/offset itself and takes ONLY those two columns from the frozen fetch (PM scope decision A, 2026-09-27); the acb.pools case still gates every computed field.",
  ];

  const meta: FixtureMeta = {
    slice: "s4",
    base_url: baseUrl,
    tenant: "demo",
    base_commit: headCommit(),
    captured_at_utc: new Date().toISOString(),
    captured_at_et: today,
    reporting_currency: "CAD",
    capture_command: "node frontend/scripts/parity/capture.ts --slice s4",
    diff_command: "node frontend/scripts/parity/capture.ts --slice s4 && node frontend/scripts/parity/parity.ts --slice s4",
    notes,
  };

  return {
    meta,
    datasets: {
      demo: {
        trades,
        accounts,
        securities,
        prices,
        fx,
        // The frozen GET /acb response — server-owned metadata only is read
        // from it by the engine (id, updated_at); see the note above.
        acbPools: acbRes.body,
      },
    },
    cases,
  };
}

// --------------------------------------------------------------------------
// S5: fuzzy `GET /transactions?q=` goldens (M48-S5 removes the parameter)
// --------------------------------------------------------------------------

/** A query the demo data cannot score above the threshold — the frozen check. */
const S5_SENTINEL_QUERY = "zzzzqqq";

interface S5SearchSpec {
  id: string;
  q: string;
  limit?: number;
  offset?: number;
  direction?: "paid" | "received";
}

/**
 * One golden per behavior the browser must reproduce on its own after M48-S5:
 * relevance ordering (score desc, id desc) over a 49-row payee, a mid-list page
 * window, the default limit/offset envelope, threshold misses, a punctuation
 * edge (`&` in "CITY POWER & WATER"), and composition with the one structured
 * filter the search view composes with (`direction` — the substrate fetch still
 * runs server-side, only the scoring moves).
 */
const S5_SEARCH_SPECS: S5SearchSpec[] = [
  { id: "transactions.search.rank", q: "grocery", limit: 50, offset: 0 },
  { id: "transactions.search.page-window", q: "grocery", limit: 10, offset: 40 },
  { id: "transactions.search.multi-word", q: "cafe luna", limit: 50, offset: 0 },
  { id: "transactions.search.default-envelope", q: "netflix" },
  { id: "transactions.search.punctuation", q: "city power water", limit: 50, offset: 0 },
  { id: "transactions.search.threshold-edge", q: "gro", limit: 50, offset: 0 },
  { id: "transactions.search.threshold-miss", q: S5_SENTINEL_QUERY, limit: 50, offset: 0 },
  { id: "transactions.search.cross-payee", q: "luna sushi", limit: 50, offset: 0 },
  { id: "transactions.search.direction-composes", q: "payroll", direction: "received", limit: 50, offset: 0 },
  { id: "transactions.search.direction-excludes", q: "payroll", direction: "paid", limit: 50, offset: 0 },
];

async function captureS5(baseUrl: string, today: string): Promise<Fixture> {
  const token = await demoToken(baseUrl);
  const api = new Api(baseUrl, token);

  say("  demo tenant: fetching inputs");
  const transactions = await api.paged("/transactions");
  if (transactions.length === 0) throw new Error("GET /transactions returned no demo rows");

  say("  demo tenant: fetching goldens");
  const cases: ParityCase[] = [];
  for (const spec of S5_SEARCH_SPECS) {
    const params = new URLSearchParams();
    params.set("q", spec.q);
    if (spec.direction !== undefined) params.set("direction", spec.direction);
    if (spec.limit !== undefined) params.set("limit", String(spec.limit));
    if (spec.offset !== undefined) params.set("offset", String(spec.offset));
    const path = `/transactions?${params.toString()}`;
    const res = await api.get(path);
    expect(res, `GET ${path}`);
    const body = res.body as { total?: Json; items?: Json } | null;
    if (!Array.isArray(body?.items) || typeof body?.total !== "number") {
      throw new Error(`GET ${path} did not return {items, total}`);
    }
    if (spec.q === S5_SENTINEL_QUERY && body.total !== 0) {
      // M48-S5 deleted the parameter: an unranked list answers with every row.
      throw new Error(
        `GET ${path} returned total=${body.total} for a no-match query — the ` +
          "q parameter was removed in M48-S5 (the search view computes client-" +
          "side now), so the S5 goldens are frozen at meta.base_commit and " +
          "cannot be re-captured.",
      );
    }
    cases.push({
      id: spec.id,
      dataset: "demo",
      request: { method: "GET", path },
      expected: { status: res.status, body: res.body },
    });
  }
  say(`  captured ${cases.length} search goldens over ${transactions.length} rows`);

  const notes = [
    `datasets.demo.transactions is EVERY committed demo row (${transactions.length}, 200/page merged) — the exact population the server scored, so the engine ranks from the same inputs.`,
    "The engine reproduces the removed endpoint: threshold (0.3), score desc / id desc ordering, and the {items,total,limit,offset} envelope. `direction` cases pin that the S5 client still takes its substrate from the server (filters unchanged) and only the scoring moved; the port classifies the Decimal sign without floats.",
    "No demo row carries ciphertext (payee_cipher is null on all rows), so resolved payee === normalized_payee and the goldens pin ordering, not vault resolution — locked-vault degradation is covered by tests/searchMatch.test.ts instead.",
    "Frozen after M48-S5: `capture.ts --slice s5` re-runs only while the server still reads `q`; afterwards it stops at the threshold-miss sentinel with an explicit error rather than recording an unranked page as a golden.",
    "reporting_currency is meta.reporting_currency (get_settings().reporting_currency, not exposed by any endpoint).",
  ];

  const meta: FixtureMeta = {
    slice: "s5",
    base_url: baseUrl,
    tenant: "demo",
    base_commit: headCommit(),
    captured_at_utc: new Date().toISOString(),
    captured_at_et: today,
    reporting_currency: "CAD",
    capture_command: "node frontend/scripts/parity/capture.ts --slice s5",
    diff_command: "node frontend/scripts/parity/parity.ts --slice s5",
    notes,
  };

  return {
    meta,
    datasets: { demo: { transactions } },
    cases,
  };
}

// --------------------------------------------------------------------------
// S2a: the three report routes the browser used to compute for itself
// (docs/33 §Server-first K-1/K-2/K-4; M135 §4.4-1 "capture first, then swap")
// --------------------------------------------------------------------------
//
// The demo tenant exercises the DEFAULT shapes only — every row is CAD,
// categorised, untagged and unlinked — so the tag dimension, the two drill
// filters, the multi-tag double count and the fx conversion would all be
// captured as empty answers. M135 §4.4-3's rule applies verbatim: extend the
// capture with a synthetic tenant (this file's S3-preview pattern) rather
// than letting a vacuous golden stand in for a shape.

/** Ranges are FIXED calendar windows — never today-relative — so a re-capture
 *  only moves when the data moves. The demo ledger spans 2025-10-01..2026-09-28. */
const S2A_DEMO_RANGES = {
  full: { from: "2025-10-01", to: "2026-09-30" },
  september: { from: "2026-09-01", to: "2026-09-30" },
  septemberWeek: { from: "2026-09-01", to: "2026-09-07" },
  year: { from: "2026-01-01", to: "2026-12-31" },
  empty: { from: "2024-01-01", to: "2024-12-31" },
} as const;

/** The synthetic ledger lives entirely inside 2026-01-01..2026-09-30. */
const S2A_SYNTH_RANGES = {
  full: { from: "2026-01-01", to: "2026-09-30" },
  window: { from: "2026-03-02", to: "2026-03-08" },
  week: { from: "2026-03-02", to: "2026-03-15" },
  march: { from: "2026-03-01", to: "2026-03-31" },
  may: { from: "2026-05-01", to: "2026-05-31" },
  year: { from: "2026-01-01", to: "2026-12-31" },
  empty: { from: "2024-01-01", to: "2024-12-31" },
} as const;

const SYNTHETIC_TAG_NAMES = [
  "tag-a",
  "tag-b",
  "tag-c",
  "tag-d",
  "tag-e",
  "tag-f",
  "tag-g",
  "tag-h",
  "tag-i",
  "tag-j",
  "tag-k",
  "tag-l",
];

interface SyntheticTxn {
  date: string;
  payee: string;
  amount: string;
  category?: string;
  tags?: string[];
  currency?: "USD";
}

/**
 * Every branch the two spending engines, the breakdown and the matrix read,
 * on purpose:
 *
 *  - `parity-child` is a CHILD of `parity-root` -> the drill widens to the
 *    subtree while the breakdown rolls the child back up to its root;
 *  - `PARITY MULTI` carries two tags -> the tag dimension counts it twice and
 *    `spend_total_cad` / `total_cad` (matrix) stay once-counted;
 *  - two rows carry no category and one carries no tag -> the `uncategorized`
 *    drill and the `Untagged` column have real money behind them;
 *  - one row is USD -> its `cad_amount` is the server's own fx conversion;
 *  - one income row and one 0.00 row -> both are excluded from spending and
 *    only the income one reaches cash flow's income side.
 */
const SYNTHETIC_TRANSACTIONS: SyntheticTxn[] = [
  { date: "2026-01-15", payee: "PARITY JANUARY", amount: "-5.00", category: "parity-solo" },
  { date: "2026-03-02", payee: "PARITY GROCERY", amount: "-120.50", category: "parity-child", tags: ["tag-a"] },
  { date: "2026-03-03", payee: "PARITY ROOT SPEND", amount: "-80.25", category: "parity-root", tags: ["tag-b"] },
  { date: "2026-03-04", payee: "PARITY SOLO", amount: "-40.00", category: "parity-solo" },
  { date: "2026-03-05", payee: "PARITY UNTAGGED", amount: "-10.10" },
  { date: "2026-03-06", payee: "PARITY MULTI", amount: "-60.00", category: "parity-child", tags: ["tag-a", "tag-b"] },
  { date: "2026-03-07", payee: "PARITY PAYROLL", amount: "2500.00" },
  { date: "2026-03-08", payee: "PARITY ZERO", amount: "0.00", category: "parity-solo" },
  { date: "2026-03-09", payee: "PARITY USD ROW", amount: "-25.00", category: "parity-solo", tags: ["tag-c"], currency: "USD" },
  { date: "2026-04-12", payee: "PARITY APRIL ONE", amount: "-33.33", category: "parity-root", tags: ["tag-c"] },
  { date: "2026-04-13", payee: "PARITY APRIL TWO", amount: "-77.77", category: "parity-child", tags: ["tag-d"] },
  { date: "2026-09-30", payee: "PARITY SEPTEMBER", amount: "-9.99", tags: ["tag-a"] },
  // One row per remaining tag, descending amounts, so the matrix's top-8 cut
  // has a deterministic ranking AND genuinely spills tags into "Other tags".
  { date: "2026-05-11", payee: "PARITY TAG E", amount: "-17.00", category: "parity-solo", tags: ["tag-e"] },
  { date: "2026-05-12", payee: "PARITY TAG F", amount: "-16.00", category: "parity-solo", tags: ["tag-f"] },
  { date: "2026-05-13", payee: "PARITY TAG G", amount: "-15.00", category: "parity-solo", tags: ["tag-g"] },
  { date: "2026-05-14", payee: "PARITY TAG H", amount: "-14.00", category: "parity-solo", tags: ["tag-h"] },
  { date: "2026-05-15", payee: "PARITY TAG I", amount: "-13.00", category: "parity-solo", tags: ["tag-i"] },
  { date: "2026-05-16", payee: "PARITY TAG J", amount: "-12.00", category: "parity-solo", tags: ["tag-j"] },
  { date: "2026-05-17", payee: "PARITY TAG K", amount: "-11.00", category: "parity-solo", tags: ["tag-k"] },
  { date: "2026-05-18", payee: "PARITY TAG L", amount: "-10.00", category: "parity-solo", tags: ["tag-l"] },
];

interface SyntheticTenant {
  email: string;
  password: string;
  token: string;
  dataset: Dataset;
  /** Synthetic category name -> id (the specs name categories by handle). */
  categoryIds: Record<string, number>;
  tagIds: Record<string, number>;
}

async function registerTenant(baseUrl: string): Promise<{ email: string; password: string; token: string }> {
  const email = `parity-capture-${Date.now()}-${randomBytes(4).toString("hex")}@example.com`;
  const password = `parity-${randomBytes(18).toString("hex")}`;
  const register = await request("POST", `${baseUrl}/auth/register`, { body: { email, password } });
  if (register.status !== 201 && register.status !== 200) {
    throw new Error(`POST /auth/register returned ${register.status}`);
  }
  // M87 (docs/29 F15) made registration token-free on BOTH branches so it
  // cannot be used as a "is this address registered" oracle, so the session
  // comes from the very next login — the credentials were just chosen.
  const login = await request("POST", `${baseUrl}/auth/login`, { body: { email, password } });
  return { email, password, token: requireAccessToken(login, "POST /auth/login") };
}

async function deleteTenant(baseUrl: string, tenant: { email: string; password: string; token: string }): Promise<void> {
  const del = await request("DELETE", `${baseUrl}/users/me`, {
    token: tenant.token,
    body: { current_password: tenant.password },
  });
  if (del.status === 204) {
    say(`  throwaway tenant deleted (204): ${tenant.email}`);
  } else {
    // Loud: leftover prod rows are a real problem, never a footnote.
    say(`  WARNING: throwaway tenant deletion returned ${del.status} — remove manually: ${tenant.email}`);
  }
}

/** Register + seed the deterministic ledger both new slices diff against.
 *
 *  The seeding runs inside a `try` that DELETES the tenant on any failure:
 *  a throw halfway through (a 422 on one shape, a missing fx row) used to
 *  leave a registered account plus its partial ledger behind on the
 *  production database, and a leftover prod row is a real problem, never a
 *  footnote. Only a failure BEFORE the account exists skips the cleanup.
 */
async function seedSyntheticTenant(baseUrl: string): Promise<SyntheticTenant> {
  const tenant = await registerTenant(baseUrl);
  const api = new Api(baseUrl, tenant.token);
  say(`  register throwaway tenant ${tenant.email}`);
  try {

    const categoryIds: Record<string, number> = {};
    const rootRes = await api.post("/categories", { name: "parity-root" });
    if (rootRes.status !== 201 && rootRes.status !== 200) throw new Error(`POST /categories parity-root returned ${rootRes.status}`);
    const rootId = (rootRes.body as Record<string, Json> | null)?.id;
    if (typeof rootId !== "number") throw new Error("POST /categories parity-root returned no id");
    categoryIds["parity-root"] = rootId;

    for (const name of ["parity-child", "parity-solo"]) {
      const res = await api.post("/categories", name === "parity-child" ? { name, parent_id: rootId } : { name });
      if (res.status !== 201 && res.status !== 200) throw new Error(`POST /categories ${name} returned ${res.status}`);
      const id = (res.body as Record<string, Json> | null)?.id;
      if (typeof id !== "number") throw new Error(`POST /categories ${name} returned no id`);
      categoryIds[name] = id;
    }

    const tagIds: Record<string, number> = {};
    for (const name of SYNTHETIC_TAG_NAMES) {
      const res = await api.post("/tags", { name });
      if (res.status !== 201 && res.status !== 200) throw new Error(`POST /tags ${name} returned ${res.status}`);
      const id = (res.body as Record<string, Json> | null)?.id;
      if (typeof id !== "number") throw new Error(`POST /tags ${name} returned no id`);
      tagIds[name] = id;
    }

    const accountIds: Record<string, number> = {};
    for (const [handle, currency] of [
      ["cad", "CAD"],
      ["usd", "USD"],
    ] as const) {
      const res = await api.post("/accounts", {
        name: `parity-${handle}`,
        type: "chequing",
        currency,
        starting_balance: "0",
      });
      if (res.status !== 201 && res.status !== 200) throw new Error(`POST /accounts parity-${handle} returned ${res.status}`);
      const id = (res.body as Record<string, Json> | null)?.id;
      if (typeof id !== "number") throw new Error(`POST /accounts parity-${handle} returned no id`);
      accountIds[handle] = id;
    }

    // The USD row's date is a CACHED rate's own date, so the server resolves a
    // cad_amount at write time and the golden can never record a silently
    // unresolved (null) conversion.
    const usdRates = await api.get(
      `/fx?base=USD&quote=CAD&start_date=2000-01-01&end_date=${S2A_SYNTH_RANGES.full.to}&fetch=false`,
    );
    expect(usdRates, "GET /fx base=USD");
    const rateItems = (usdRates.body as { items?: Json } | null)?.items;
    if (!Array.isArray(rateItems) || rateItems.length === 0) {
      throw new Error("the shared fx cache carries no USD->CAD rows — the synthetic USD row cannot be captured");
    }
    const rateDates = rateItems
      .map((row) => (row as Record<string, Json>).date)
      .filter((d): d is string => typeof d === "string" && d <= S2A_SYNTH_RANGES.full.to);
    const usdDate = rateDates[rateDates.length - 1];
    if (usdDate === undefined) throw new Error("no cached USD->CAD rate on or before 2026-09-30");

    for (const seed of SYNTHETIC_TRANSACTIONS) {
      const date = seed.currency === "USD" ? usdDate : seed.date;
      const res = await api.post("/transactions", {
        account_id: seed.currency === "USD" ? accountIds.usd : accountIds.cad,
        date,
        raw_description: seed.payee,
        normalized_payee: seed.payee,
        amount_native: seed.amount,
        currency: seed.currency ?? "CAD",
        ...(seed.category === undefined ? {} : { category_id: categoryIds[seed.category] }),
      });
      if (res.status !== 201 && res.status !== 200) {
        throw new Error(`POST /transactions ${seed.payee} returned ${res.status}`);
      }
      const id = (res.body as Record<string, Json> | null)?.id;
      if (typeof id !== "number") throw new Error(`POST /transactions ${seed.payee} returned no id`);
      if (seed.currency === "USD" && (res.body as Record<string, Json>).cad_amount === null) {
        throw new Error(`POST /transactions ${seed.payee} (USD) resolved no cad_amount at ${date}`);
      }
      if (seed.tags !== undefined && seed.tags.length > 0) {
        const tagged = await api.put(`/transactions/${id}/tags`, {
          tag_ids: seed.tags.map((name) => tagIds[name]),
        });
        if (tagged.status !== 200) throw new Error(`PUT /transactions/${id}/tags returned ${tagged.status}`);
      }
    }
    say(`  seeded ${SYNTHETIC_TRANSACTIONS.length} transactions, ${Object.keys(categoryIds).length} categories, ${Object.keys(tagIds).length} tags`);

    const transactions = await api.paged("/transactions");
    if (transactions.length !== SYNTHETIC_TRANSACTIONS.length) {
      throw new Error(`seeded ${SYNTHETIC_TRANSACTIONS.length} rows but GET /transactions returned ${transactions.length}`);
    }
    const categories = await api.paged("/categories");
    const accounts = await api.paged("/accounts");
    const trades = await api.paged("/trades?committed_only=true");
    const fx = await fetchFx(api, nonCadCurrencies([accounts]), S2A_SYNTH_RANGES.full.to);
    return {
      email: tenant.email,
      password: tenant.password,
      token: tenant.token,
      dataset: { transactions, categories, accounts, trades, fx },
      categoryIds,
      tagIds,
    };
  } catch (error) {
    await deleteTenant(baseUrl, tenant);
    throw error;
  }
}


/** `?from=&to=&granularity=` (+ optional group_by / category_id) for a case. */
function reportPath(
  route: string,
  range: { from: string; to: string },
  extra: { granularity?: string; groupBy?: string; categoryId?: string | number } = {},
): string {
  const params = new URLSearchParams({ from: range.from, to: range.to });
  if (extra.granularity !== undefined) params.set("granularity", extra.granularity);
  if (extra.groupBy !== undefined) params.set("group_by", extra.groupBy);
  if (extra.categoryId !== undefined) params.set("category_id", String(extra.categoryId));
  return `${route}?${params.toString()}`;
}

async function goldenCase(api: Api, id: string, dataset: string, path: string): Promise<ParityCase> {
  const res = await api.get(path);
  expect(res, `GET ${path}`);
  return { id, dataset, request: { method: "GET", path }, expected: { status: res.status, body: res.body } };
}

async function captureS2a(baseUrl: string, today: string): Promise<Fixture> {
  const token = await demoToken(baseUrl);
  const demo = new Api(baseUrl, token);

  say("  demo tenant: fetching inputs");
  const transactions = await demo.paged("/transactions");
  const categories = await demo.paged("/categories");
  const accounts = await demo.paged("/accounts");
  const securities = await demo.paged("/securities");
  const trades = await demo.paged("/trades?committed_only=true");
  const fx = await fetchFx(demo, nonCadCurrencies([accounts, securities]), today);

  say("  demo tenant: fetching goldens");
  // The drill target is the demo's LARGEST category, so the case keeps money
  // in it whatever the seed looks like — an id picked by hand would go stale.
  const breakdown = await demo.get(reportPath("/reports/spending/categories", S2A_DEMO_RANGES.full));
  expect(breakdown, "GET /reports/spending/categories (drill target)");
  const topCategory = ((breakdown.body as { items?: Json } | null)?.items?.[0] as
    | Record<string, Json>
    | undefined
    | null)?.category_id;
  if (typeof topCategory !== "number") throw new Error("GET /reports/spending/categories returned no category item");

  const demoSpecs: Array<[string, string]> = [
    ["spending.by-category.month.full", reportPath("/reports/spending", S2A_DEMO_RANGES.full, { granularity: "month" })],
    ["spending.by-category.week.september", reportPath("/reports/spending", S2A_DEMO_RANGES.september, { granularity: "week" })],
    ["spending.by-category.year.2026", reportPath("/reports/spending", S2A_DEMO_RANGES.year, { granularity: "year" })],
    ["spending.by-category.empty.2024", reportPath("/reports/spending", S2A_DEMO_RANGES.empty, { granularity: "month" })],
    ["spending.by-tag.month.full", reportPath("/reports/spending", S2A_DEMO_RANGES.full, { granularity: "month", groupBy: "tag" })],
    ["spending.drill.top-category", reportPath("/reports/spending", S2A_DEMO_RANGES.full, { granularity: "month", categoryId: topCategory })],
    ["spending.uncategorized-drill", reportPath("/reports/spending", S2A_DEMO_RANGES.full, { granularity: "month", categoryId: "uncategorized" })],
    ["spending.tag-by-top-category-drill", reportPath("/reports/spending", S2A_DEMO_RANGES.full, { granularity: "month", groupBy: "tag", categoryId: topCategory })],
    ["breakdown.by-category.full", reportPath("/reports/spending/categories", S2A_DEMO_RANGES.full)],
    ["breakdown.by-tag.full", reportPath("/reports/spending/categories", S2A_DEMO_RANGES.full, { groupBy: "tag" })],
    ["cashflow.month.full", reportPath("/reports/cashflow", S2A_DEMO_RANGES.full, { granularity: "month" })],
    ["cashflow.day.september-week", reportPath("/reports/cashflow", S2A_DEMO_RANGES.septemberWeek, { granularity: "day" })],
    ["cashflow.year.2026", reportPath("/reports/cashflow", S2A_DEMO_RANGES.year, { granularity: "year" })],
    ["cashflow.empty.2024", reportPath("/reports/cashflow", S2A_DEMO_RANGES.empty, { granularity: "month" })],
  ];

  const cases: ParityCase[] = [];
  for (const [id, path] of demoSpecs) cases.push(await goldenCase(demo, id, "demo", path));
  say(`  captured ${demoSpecs.length} demo goldens over ${transactions.length} rows`);

  say("  throwaway tenant: the shapes the demo cannot carry");
  const synth = await seedSyntheticTenant(baseUrl);
  const scratch = new Api(baseUrl, synth.token);
  try {
    const root = synth.categoryIds["parity-root"] as number;
    const child = synth.categoryIds["parity-child"] as number;
    const synthSpecs: Array<[string, string]> = [
      ["spending.by-category.month.full", reportPath("/reports/spending", S2A_SYNTH_RANGES.full, { granularity: "month" })],
      ["spending.by-category.day.window", reportPath("/reports/spending", S2A_SYNTH_RANGES.window, { granularity: "day" })],
      ["spending.by-category.week.window", reportPath("/reports/spending", S2A_SYNTH_RANGES.week, { granularity: "week" })],
      ["spending.by-category.year.2026", reportPath("/reports/spending", S2A_SYNTH_RANGES.year, { granularity: "year" })],
      ["spending.by-category.empty.2024", reportPath("/reports/spending", S2A_SYNTH_RANGES.empty, { granularity: "month" })],
      ["spending.by-tag.month.full", reportPath("/reports/spending", S2A_SYNTH_RANGES.full, { granularity: "month", groupBy: "tag" })],
      ["spending.by-tag.multi-tag.window", reportPath("/reports/spending", S2A_SYNTH_RANGES.window, { granularity: "day", groupBy: "tag" })],
      ["spending.drill.parent-subtree", reportPath("/reports/spending", S2A_SYNTH_RANGES.full, { granularity: "month", categoryId: root })],
      ["spending.drill.child-exact", reportPath("/reports/spending", S2A_SYNTH_RANGES.full, { granularity: "month", categoryId: child })],
      ["spending.uncategorized-drill", reportPath("/reports/spending", S2A_SYNTH_RANGES.full, { granularity: "month", categoryId: "uncategorized" })],
      ["spending.tag-by-parent-drill", reportPath("/reports/spending", S2A_SYNTH_RANGES.full, { granularity: "month", groupBy: "tag", categoryId: root })],
      ["breakdown.by-category.full", reportPath("/reports/spending/categories", S2A_SYNTH_RANGES.full)],
      ["breakdown.by-tag.full", reportPath("/reports/spending/categories", S2A_SYNTH_RANGES.full, { groupBy: "tag" })],
      ["cashflow.month.full", reportPath("/reports/cashflow", S2A_SYNTH_RANGES.full, { granularity: "month" })],
      ["cashflow.day.window", reportPath("/reports/cashflow", S2A_SYNTH_RANGES.window, { granularity: "day" })],
    ];
    for (const [id, path] of synthSpecs) cases.push(await goldenCase(scratch, `synth.${id}`, "scratch", path));
    say(`  captured ${synthSpecs.length} synthetic goldens`);
  } finally {
    await deleteTenant(baseUrl, synth);
  }

  const notes = [
    "S2a (docs/33 §Server-first K-1/K-2/K-4): the goldens the browser engine must reproduce before any of the three routes may be re-pointed. M135 §4.4-1 — capture first, then swap.",
    "Ranges are FIXED calendar windows, never today-relative: the engines read from/to out of the request path, so nothing here moves with the clock.",
    `datasets.demo is the demo tenant (read-only GETs over ${transactions.length} committed rows, 2025-10-01..2026-09-28). datasets.scratch is a throwaway tenant registered, seeded and deleted during this capture — the demo carries NO tags, NO uncategorized rows, NO linked transfers and NO non-CAD transactions, so the tag dimension, both drill filters, the multi-tag double count and the fx path would otherwise be captured as empty answers (M135 §4.4-3: extend the capture, never relax the comparison).`,
    "The synthetic ledger is one deterministic row set (20 rows): a parent/child category pair, a two-tag row, two uncategorized rows, one USD row, one income row and one 0.00 row. The USD row is dated on a CACHED fx rate's own date so the server resolves cad_amount at write time.",
    "fx is captured cache-only (fetch=false, 2000-01-01 .. the range end) — reports never fetch (docs/07/docs/12).",
    "`datasets.*.trades` is `GET /trades?committed_only=true`: cash flow counts trade cash actions + commissions, never principal (docs/12).",
    "The demo tenant has NO linked transfers, so M143's `_is_transfer` exclusion (spending + breakdown skip them, cash flow keeps them) is NOT exercised by any demo golden here — the synthetic tenant does not seed one either. That exclusion is still unpinned by this fixture; noted for the M156 sweep rather than papered over.",
    "reporting_currency is meta.reporting_currency (get_settings().reporting_currency, not exposed by any endpoint); lib/dec.ts pins REPORTING='CAD' the same way.",
  ];

  const meta: FixtureMeta = {
    slice: "s2a",
    base_url: baseUrl,
    tenant: "demo (GET goldens) + throwaway (tag/drill/fx shapes)",
    base_commit: headCommit(),
    captured_at_utc: new Date().toISOString(),
    captured_at_et: today,
    reporting_currency: "CAD",
    capture_command: "node frontend/scripts/parity/capture.ts --slice s2a",
    diff_command: "node frontend/scripts/parity/capture.ts --slice s2a && node frontend/scripts/parity/parity.ts --slice s2a",
    notes,
  };

  return {
    meta,
    datasets: {
      // `securities` is deliberately NOT an input: no engine here reads it.
      // It still feeds `nonCadCurrencies` above, which decides WHICH fx bases
      // get captured — the rows are what matters, not the security list.
      demo: { transactions, categories, accounts, trades, fx },
      scratch: synth.dataset,
    },
    cases,
  };
}

// --------------------------------------------------------------------------
// M124 R3: the Category x tag matrix (docs/12 §1, docs/08 §category-tag-matrix)
// --------------------------------------------------------------------------

async function captureMatrix(baseUrl: string, today: string): Promise<Fixture> {
  const token = await demoToken(baseUrl);
  const demo = new Api(baseUrl, token);

  say("  demo tenant: fetching inputs");
  const transactions = await demo.paged("/transactions");
  const categories = await demo.paged("/categories");

  say("  demo tenant: fetching goldens");
  const demoSpecs: Array<[string, string]> = [
    ["matrix.full", reportPath("/reports/category-tag-matrix", S2A_DEMO_RANGES.full)],
    ["matrix.september", reportPath("/reports/category-tag-matrix", S2A_DEMO_RANGES.september)],
  ];
  const cases: ParityCase[] = [];
  for (const [id, path] of demoSpecs) cases.push(await goldenCase(demo, id, "demo", path));
  say(`  captured ${demoSpecs.length} demo goldens over ${transactions.length} rows`);

  say("  throwaway tenant: the tag columns the demo cannot carry");
  const synth = await seedSyntheticTenant(baseUrl);
  const scratch = new Api(baseUrl, synth.token);
  try {
    const synthSpecs: Array<[string, string]> = [
      ["matrix.full", reportPath("/reports/category-tag-matrix", S2A_SYNTH_RANGES.full)],
      ["matrix.march", reportPath("/reports/category-tag-matrix", S2A_SYNTH_RANGES.march)],
      ["matrix.may-tags-only", reportPath("/reports/category-tag-matrix", S2A_SYNTH_RANGES.may)],
      ["matrix.empty.2024", reportPath("/reports/category-tag-matrix", S2A_SYNTH_RANGES.empty)],
    ];
    for (const [id, path] of synthSpecs) cases.push(await goldenCase(scratch, `synth.${id}`, "scratch", path));
    say(`  captured ${synthSpecs.length} synthetic goldens`);
  } finally {
    await deleteTenant(baseUrl, synth);
  }

  const notes = [
    "M124 R3 (docs/12 §1, docs/08 §category-tag-matrix): the cross-tab the Categories tab renders, captured because no fixture existed for it (M135 §4.3 — 'capture required'). Its own slice + engine adapter + fixture, exactly like s2a.",
    "datasets.demo is the demo tenant — every row is untagged, so its columns collapse to a single `Untagged` and its value is the zero/one-column shape only.",
    "datasets.scratch is a throwaway tenant with 12 tags: `matrix.full` exercises the top-8 cut, the `Other tags` spill column and the always-last `Untagged`, `matrix.may-tags-only` exercises the same cut over a window whose rows are all tagged, and `matrix.empty.2024` pins the no-spend shape (no rows, `Untagged` only). M135 §4.4-3: extend the capture, never relax the comparison.",
    "Cells count a transaction once PER TAG, so `items` can sum past `total_cad` — the once-counted figure the caption points at (docs/09 §Hidden data). The synthetic multi-tag row is what makes that difference non-zero here.",
    "Rows are RAW categories, not root-rolled: `parity-child` stays its own row even though `parity-root` is its parent (docs/12 §1).",
    "No demo or synthetic row carries a linked transfer, so M143's `_is_transfer` exclusion is NOT exercised — the server's matrix deliberately does not apply it, unlike the two spending readers.",
    "reporting_currency is meta.reporting_currency (get_settings().reporting_currency, not exposed by any endpoint).",
  ];

  const meta: FixtureMeta = {
    slice: "matrix",
    base_url: baseUrl,
    tenant: "demo (GET goldens) + throwaway (tag columns)",
    base_commit: headCommit(),
    captured_at_utc: new Date().toISOString(),
    captured_at_et: today,
    reporting_currency: "CAD",
    capture_command: "node frontend/scripts/parity/capture.ts --slice matrix",
    diff_command: "node frontend/scripts/parity/capture.ts --slice matrix && node frontend/scripts/parity/parity.ts --slice matrix",
    notes,
  };

  return {
    meta,
    datasets: {
      demo: { transactions, categories },
      scratch: synth.dataset,
    },
    cases,
  };
}

// --------------------------------------------------------------------------
// CLI
// --------------------------------------------------------------------------

// --- M159: the household scope (docs/12 §Owner dimension, docs/08) ----------
//
// K-13's fresh slice: the Owner lens' server twin. A household needs TWO
// tenants and a live `sharing_links` row, so this capture registers an OWNER
// and a PARTNER, seeds both ledgers, links them on ONE account, and takes its
// goldens as the PARTNER (the seat the lens actually runs for) — plus the two
// degenerate populations (the owner seat, and a privacy-mode owner) that the
// scope's tenancy promises must hold. Both tenants are deleted at the end;
// any throw mid-seed deletes them on the way out, exactly like the S3
// previews (a leftover prod tenant is a real problem, never a footnote).

const HOUSEHOLD_RANGE = { from: "2026-05-01", to: "2026-05-31" } as const;

interface HouseholdTenant {
  email: string;
  password: string;
  token: string;
  categories: Record<string, number>;
  accounts: Record<string, number>;
}

async function seedHouseholdTenant(
  baseUrl: string,
  handle: "owner" | "partner",
): Promise<HouseholdTenant> {
  const tenant = await registerTenant(baseUrl);
  const api = new Api(baseUrl, tenant.token);
  say(`  register throwaway ${handle} ${tenant.email}`);
  const categories: Record<string, number> = {};
  const accounts: Record<string, number> = {};

  const root = await api.post("/categories", { name: `m159-${handle}-root` });
  if (root.status !== 201 && root.status !== 200) {
    throw new Error(`POST /categories m159-${handle}-root returned ${root.status}`);
  }
  const rootId = (root.body as Record<string, Json> | null)?.id;
  if (typeof rootId !== "number") throw new Error("m159 root category got no id");
  categories.root = rootId;

  const child = await api.post("/categories", {
    name: `m159-${handle}-child`,
    parent_id: rootId,
  });
  if (child.status !== 201 && child.status !== 200) {
    throw new Error(`POST /categories m159-${handle}-child returned ${child.status}`);
  }
  const childId = (child.body as Record<string, Json> | null)?.id;
  if (typeof childId !== "number") throw new Error("m159 child category got no id");
  categories.child = childId;

  const solo = await api.post("/categories", { name: `m159-${handle}-solo` });
  if (solo.status !== 201 && solo.status !== 200) {
    throw new Error(`POST /categories m159-${handle}-solo returned ${solo.status}`);
  }
  const soloId = (solo.body as Record<string, Json> | null)?.id;
  if (typeof soloId !== "number") throw new Error("m159 solo category got no id");
  categories.solo = soloId;

  for (const name of handle === "owner" ? ["shared", "hidden"] : ["own"]) {
    const res = await api.post("/accounts", {
      name: `m159-${handle}-${name}`,
      type: "chequing",
      currency: "CAD",
      starting_balance: "0",
    });
    if (res.status !== 201 && res.status !== 200) {
      throw new Error(`POST /accounts m159-${handle}-${name} returned ${res.status}`);
    }
    const id = (res.body as Record<string, Json> | null)?.id;
    if (typeof id !== "number") throw new Error(`m159 ${handle} ${name} account got no id`);
    accounts[name] = id;
  }

  return { ...tenant, categories, accounts };
}

/** `[accountHandle, date, amount, description, categoryHandle|null]` — CAD only
 *  (no fx dataset: the money columns are the server's own, cache-free). */
type HouseholdSeed = [string, string, string, string, string | null];

const OWNER_SEED: HouseholdSeed[] = [
  ["shared", "2026-05-03", "-120.00", "M159-OWNER-ROOT", "root"],
  ["shared", "2026-05-09", "-45.50", "M159-OWNER-CHILD", "child"],
  ["shared", "2026-05-14", "-12.25", "M159-OWNER-UNCAT", null],
  // The HIDDEN account never reaches a partner's household (docs/43 §2).
  ["hidden", "2026-05-06", "-88.00", "M159-OWNER-HIDDEN", "solo"],
  ["hidden", "2026-05-20", "-9.99", "M159-OWNER-HIDDEN-2", null],
];

const PARTNER_SEED: HouseholdSeed[] = [
  ["own", "2026-05-05", "-60.00", "M159-PARTNER-SOLO", "solo"],
  ["own", "2026-05-11", "-33.33", "M159-PARTNER-ROOT", "root"],
  ["own", "2026-05-22", "-7.77", "M159-PARTNER-UNCAT", null],
];

async function seedHouseholdRows(api: Api, tenant: HouseholdTenant, rows: HouseholdSeed[]): Promise<void> {
  for (const [account, date, amount, description, category] of rows) {
    const res = await api.post("/transactions", {
      account_id: tenant.accounts[account],
      date,
      raw_description: description,
      normalized_payee: description,
      amount_native: amount,
      currency: "CAD",
      ...(category === null ? {} : { category_id: tenant.categories[category] }),
    });
    if (res.status !== 201 && res.status !== 200) {
      throw new Error(`POST /transactions ${description} returned ${res.status}`);
    }
  }
}

async function captureHousehold(baseUrl: string, _today: string): Promise<Fixture> {
  const owner = await seedHouseholdTenant(baseUrl, "owner");
  let partner: HouseholdTenant | null = null;
  try {
    partner = await seedHouseholdTenant(baseUrl, "partner");
    await seedHouseholdRows(new Api(baseUrl, owner.token), owner, OWNER_SEED);
    await seedHouseholdRows(new Api(baseUrl, partner.token), partner, PARTNER_SEED);

    // The link: ONE shared account, deny-by-default on the rest (docs/43 §2).
    const invite = await new Api(baseUrl, owner.token).post("/sharing/invites", {
      email: partner.email,
      account_ids: [owner.accounts.shared],
    });
    if (invite.status !== 201) throw new Error(`POST /sharing/invites returned ${invite.status}`);
    const token = (invite.body as Record<string, Json> | null)?.token;
    if (typeof token !== "string") throw new Error("POST /sharing/invites returned no token");
    const accept = await new Api(baseUrl, partner.token).post("/sharing/invites/accept", { token });
    if (accept.status !== 200) throw new Error(`POST /sharing/invites/accept returned ${accept.status}`);
    say("  linked owner -> partner on ONE shared account");

    const asPartner = new Api(baseUrl, partner.token);
    say("  partner seat: fetching inputs");
    const ownTransactions = await asPartner.paged("/transactions");
    const ownCategories = await asPartner.paged("/categories");
    // The M93 projection itself — the exact rows the pre-M159 browser engine
    // was handed, frozen as the ADAPTER's input (not the golden).
    const projectionTransactions = await asPartner.paged("/partner/transactions");
    const ownerCategories = await asPartner.paged("/partner/categories");
    if (projectionTransactions.length !== OWNER_SEED.filter((r) => r[0] === "shared").length) {
      throw new Error(
        `GET /partner/transactions returned ${projectionTransactions.length} rows, expected the shared account's own`,
      );
    }

    say("  partner seat: fetching household goldens");
    const cases: ParityCase[] = [];
    const householdPath = (route: string, extra: Record<string, string> = {}): string => {
      const params = new URLSearchParams({ from: HOUSEHOLD_RANGE.from, to: HOUSEHOLD_RANGE.to, scope: "household" });
      for (const [key, value] of Object.entries(extra)) params.set(key, value);
      return `${route}?${params.toString()}`;
    };
    const partnerDataset = "partner";
    for (const [id, path] of [
      ["household.spending.month.full", householdPath("/reports/spending", { granularity: "month" })],
      ["household.spending.week.full", householdPath("/reports/spending", { granularity: "week" })],
      ["household.spending.day.full", householdPath("/reports/spending", { granularity: "day" })],
      [
        "household.spending.drill.owner-child",
        householdPath("/reports/spending", {
          granularity: "month",
          category_id: String(owner.categories.child),
        }),
      ],
      [
        "household.spending.drill.partner-solo",
        householdPath("/reports/spending", {
          granularity: "month",
          category_id: String(partner.categories.solo),
        }),
      ],
      ["household.spending.uncategorized-drill", householdPath("/reports/spending", { granularity: "month", category_id: "uncategorized" })],
      ["household.breakdown.month.full", householdPath("/reports/spending/categories")],
      // The narrow scope over the SAME range — proof the param is what moves
      // the population, and a regression pin for every existing caller.
      [
        "own.spending.month.full",
        `/reports/spending?from=${HOUSEHOLD_RANGE.from}&to=${HOUSEHOLD_RANGE.to}&granularity=month`,
      ],
      [
        "own.breakdown.month.full",
        `/reports/spending/categories?from=${HOUSEHOLD_RANGE.from}&to=${HOUSEHOLD_RANGE.to}`,
      ],
    ] as const) {
      cases.push(await goldenCase(asPartner, id, partnerDataset, path));
    }

    // The OWNER seat: M90's grant is one-directional, so `scope=household` is
    // the owner's own rows (docs/43 §2) — a degenerate population the tenancy
    // tests pin, captured here so the harness proves the SAME server path.
    const asOwner = new Api(baseUrl, owner.token);
    say("  owner seat: fetching the degenerate household golden");
    const ownerDataset = "owner";
    const ownerTransactions = await asOwner.paged("/transactions");
    const ownerCategoriesSelf = await asOwner.paged("/categories");
    cases.push(
      await goldenCase(
        asOwner,
        "owner-seat.spending.month.household",
        ownerDataset,
        householdPath("/reports/spending", { granularity: "month" }),
      ),
    );
    cases.push(
      await goldenCase(
        asOwner,
        "owner-seat.spending.month.own",
        ownerDataset,
        `/reports/spending?from=${HOUSEHOLD_RANGE.from}&to=${HOUSEHOLD_RANGE.to}&granularity=month`,
      ),
    );

    // A privacy-mode owner projects NOTHING (docs/43 §9, D4): the partner's
    // household collapses to their own rows. Flipped on, captured, flipped
    // back off — the link itself survives, only the mask moves.
    say("  privacy mode: masking the owner from the partner, then restoring");
    const flipOn = await request("PATCH", `${baseUrl}/users/me`, {
      token: owner.token,
      body: { privacy_mode: true },
    });
    if (flipOn.status !== 200) throw new Error(`PATCH /users/me privacy_mode=true returned ${flipOn.status}`);
    // Frozen UNDER the mask: the projection still delivers the owner's rows,
    // with every monetary field nulled (docs/43 §9). The adapter skips a
    // null-`cad_amount` row exactly as the pre-M159 engine did, so reproducing
    // this golden proves the client's own null-skip and the server's
    // population-level mask are the SAME guarantee.
    const maskedProjection = await asPartner.paged("/partner/transactions");
    cases.push(
      await goldenCase(
        asPartner,
        "privacy.spending.month.household",
        "partner-privacy",
        householdPath("/reports/spending", { granularity: "month" }),
      ),
    );
    const flipOff = await request("PATCH", `${baseUrl}/users/me`, {
      token: owner.token,
      body: { privacy_mode: false },
    });
    if (flipOff.status !== 200) throw new Error(`PATCH /users/me privacy_mode=false returned ${flipOff.status}`);

    say(`  captured ${cases.length} household goldens`);
    return {
      meta: {
        slice: "household",
        base_url: baseUrl,
        tenant: "throwaway (owner + partner, one shared account)",
        base_commit: headCommit(),
        captured_at_utc: new Date().toISOString(),
        captured_at_et: _today,
        reporting_currency: "CAD",
        capture_command: "node frontend/scripts/parity/capture.ts --slice household",
        diff_command:
          "node frontend/scripts/parity/capture.ts --slice household && node frontend/scripts/parity/parity.ts --slice household",
        notes: [
          "M159 (docs/12 §Owner dimension, docs/08 §Report routes): the goldens the Owner lens must reproduce before `scope=household` stops being computed in the browser (M135 §4.3 'capture first, then swap'). Both seats of a real partnership are registered, seeded and linked during this capture, then deleted.",
          "datasets.partner is the PARTNER seat — the only seat the Owner lens runs for. ownTransactions/ownCategories are the partner's own ledger (GET /transactions, GET /categories); projectionTransactions/ownerCategories are the M93 projection itself (GET /partner/transactions, GET /partner/categories), i.e. the exact row set the pre-M159 browser engine was handed. datasets.owner is the OWNER seat's own ledger, for the degenerate owner-seat case.",
          "The adapter merges the two category trees the way the server contract does (docs/08, docs/43 §6): a projected row carries the OWNER's ids, so the owner's tree is layered over the partner's and wins a bare-id conflict. The seeded categories deliberately COLLIDE numerically (both tenants create m159-*-root/child/solo), so a wrong merge order cannot pass by luck.",
          "The tag dimension is NOT captured here: the Owner lens groups by category only, and the M93 projection does not serve the owner's tags to the partner — a tag-mode household golden would compare a population the client engine cannot even be handed. Tag-mode household is pinned by backend/tests/test_reports_household.py instead.",
          "privacy.spending.month.household pins the D4 mask at population level: with the owner's privacy_mode on, the partner's household answer is their OWN rows (an aggregate has no field to null, docs/43 §9).",
          "No fx dataset: every row is CAD, so the money columns are the server's own conversion-free figures and the adapter never re-derives money.",
        ],
      },
      datasets: {
        partner: {
          ownTransactions,
          ownCategories,
          projectionTransactions,
          ownerCategories,
        },
        // Same partner seat, owner privacy_mode ON: the projection rows carry
        // NULL money, which is what makes the mask a population-level fact.
        "partner-privacy": {
          ownTransactions,
          ownCategories,
          projectionTransactions: maskedProjection,
          ownerCategories,
        },
        owner: { ownTransactions: ownerTransactions, ownCategories: ownerCategoriesSelf },
      },
      cases,
    };
  } finally {
    await deleteTenant(baseUrl, owner);
    if (partner !== null) await deleteTenant(baseUrl, partner);
  }
}

function usage(): void {
  console.error(
    [
      "usage: node frontend/scripts/parity/capture.ts --slice s2a|matrix|s3|s4|s5|household|all [--base-url <api-root>]",
      "",
      "  --slice     which golden set to capture (default: required)",
      "  --base-url  API root (default: " + DEFAULT_BASE_URL + ")",
    ].join("\n"),
  );
}

function writeFixture(fixture: Fixture): string {
  mkdirSync(FIXTURE_DIR, { recursive: true });
  const file = path.join(FIXTURE_DIR, `${fixture.meta.slice}-vectors.json`);
  writeFileSync(file, `${JSON.stringify(fixture, null, 1)}\n`, "utf8");
  return file;
}

function report(fixture: Fixture, file: string): void {
  const withData = Object.keys(fixture.datasets).length;
  say(`wrote ${path.relative(REPO_ROOT, file)}  (${fixture.cases.length} cases, ${withData} datasets)`);
  say(`  base_commit=${fixture.meta.base_commit}  captured_at_et=${fixture.meta.captured_at_et}`);
  for (const c of fixture.cases) {
    const shape = c.expected.status === 200 ? "200" : `${c.expected.status}`;
    say(`  ${shape} ${c.request.method} ${c.request.path}  -> ${c.id}`);
  }
}

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  let slice = "";
  let baseUrl = DEFAULT_BASE_URL;
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--slice") {
      slice = argv[i + 1] ?? "";
      i += 1;
    } else if (arg === "--base-url") {
      baseUrl = (argv[i + 1] ?? "").replace(/\/+$/, "");
      i += 1;
    } else if (arg === "--help" || arg === "-h") {
      usage();
      process.exit(0);
    } else {
      console.error(`unknown argument: ${arg}`);
      usage();
      process.exit(4);
    }
  }
  if (
    slice !== "s2a" &&
    slice !== "matrix" &&
    slice !== "s3" &&
    slice !== "s4" &&
    slice !== "s5" &&
    slice !== "household" &&
    slice !== "all"
  ) {
    usage();
    process.exit(4);
  }

  const now = new Date();
  const today = etDate(now);
  // `household` is deliberately NOT in `all`: it registers two throwaway
  // tenants against whatever stack `--base-url` points at, and it is the one
  // slice whose route may not exist on that stack yet (M135 §4.3 captures it
  // BEFORE the flip ships). Run it explicitly.
  const slices =
    slice === "all"
      ? (["s2a", "matrix", "s3", "s4", "s5"] as const)
      : ([slice] as const);
  say(`base_url=${baseUrl}  git_head=${headCommit()}  today_et=${today}`);

  for (const which of slices) {
    say(`capturing ${which.toUpperCase()} goldens...`);
    const fixture =
      which === "s2a"
        ? await captureS2a(baseUrl, today)
        : which === "matrix"
          ? await captureMatrix(baseUrl, today)
          : which === "s3"
            ? await captureS3(baseUrl, today)
            : which === "s4"
              ? await captureS4(baseUrl, today)
              : which === "household"
                ? await captureHousehold(baseUrl, today)
                : await captureS5(baseUrl, today);
    report(fixture, writeFixture(fixture));
  }
  say("CAPTURE_DONE");
}

main().catch((error: unknown) => {
  console.error(`capture failed: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
