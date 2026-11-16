# 57 — Retirement simulator (Monte Carlo on real holdings, client-side)

## Context

Kilter's headline product (trykilter.com/ca) is a retirement projection on the
user's own numbers: probability of success and a net-worth timeline, driven by
an editable assumptions panel. fintrack already holds every input that feature
needs — current net worth (`GET /api/v1/reports/net-worth`), holdings (same
payload) and historical spending/income (`GET /api/v1/reports/cashflow`) — so
v1 adopts the headline with **no bank sync, no new data sources and no new
server work**: the simulation runs in the browser on data the app already
fetches (docs/45 §context, "retirement planning, which must not ship until the
tax projection model exists" — this card ships the model in stub form).

Two docs bound the design:

- **docs/45 (forecast)** stays deterministic: its scenarios are *inputs*, not
  randomness, and it must keep saying so. The Monte Carlo lives on its own
  page so the two claims never blur.
- **docs/27 / docs/06 (tax projection)** is the *real* Canadian tax math,
  computed server-side behind a `TaxEngine`. This feature must not inline tax
  tables: it calls a seam (`lib/taxEngine.ts`). The seam now runs the real
  engine — **t_52ed8d01 shipped `caTaxEngine()` (docs/58) and t_1ab83b11
  wired it into this page** (§3); the flat-rate stub stays exported as the
  seam's reference implementation.

## Approach

Everything below is client-side. **No new API endpoints, no new tables, no new
backend code, no new network calls** (the two reads are the ones Forecast and
the Reports page already make).

### 1. Data inputs (existing endpoints only)

| Input | Source | Derivation |
|---|---|---|
| `startingPortfolio` | `getNetWorth(today, today, "day")` (docs/12 §7) | `Number(lastBucket.net_cad)` — a single day range returns the only bucket, so "last = only" |
| `monthlyContribution` (default) | `getCashflow(today − 365d, today, "month")` (docs/12 §6) | `max(0, mean(income_cad) − mean(expenses_cad))`, means over the returned buckets, rounded to cents |
| `annualWithdrawal` (default) | same cashflow payload | `mean(expenses_cad) × 12`, rounded to cents |

`expenses_cad` and `income_cad` are positive magnitudes (docs/12 §3; backend
`reports.py` computes `net = income − expenses`). Both endpoints are already
cached by react-query on other pages; the sim itself never fetches.

**Units:** the page requests cashflow with `group=month`, so the buckets are
monthly — `mean(income) − mean(expenses)` lands in CAD/**month** (the engine's
`monthlyContribution`) and `× 12` converts mean expenses to CAD/**year** (the
engine's `annualWithdrawal`). The derivations are unit-consistent with the
engine fields by construction, not by coincidence.

### 2. Engine — `frontend/src/lib/retirementEngine.ts` (pure, no I/O, no clock)

```ts
export interface RetirementInputs {
  startingPortfolio: number;   // >= 0, finite
  currentAge: number;          // integer, 18..90
  retirementAge: number;       // integer, currentAge+1..94 (PLAN_END_AGE − 1)
  monthlyContribution: number; // >= 0 (CAD/month, accumulated phase only)
  annualWithdrawal: number;    // >= 0 (CAD/year of NET spending, retirement phase)
  expectedReturnPct: number;   // -50..50 (annual expected return, %)
  volatilityPct: number;       // 0..100 (annual sigma, %)
  scenarios: number;           // integer, 100..10000 (default 2000)
  seed: number;                // integer, 0..2147483647 (default 42)
  baseYear: number;            // integer, 1900..2200 — the calendar year of the FIRST step
  tax: TaxEngine;              // seam, see §3
  eventFlow?: number[];        // M189 (docs/58 §3): length === years; entry t−1
                               // applies at step t. Absent = no events (M188
                               // behaviour, byte-identical).
}
export interface RetirementOptions { drawNormal?: () => number }  // test seam
export interface RetirementResult {
  scenarios: number; seed: number;
  survived: number;            // paths that never hit 0
  successPct: number;           // 1 dp
  ages: number[];               // years+1 entries: currentAge .. PLAN_END_AGE
  median: number[]; p10: number[]; p90: number[];  // rounded to cents, aligned to ages
}
export const PLAN_END_AGE = 95;
export const DEFAULT_SCENARIOS = 2000;
export const DEFAULT_SEED = 42;
export const SIM_DEBOUNCE_MS = 300;
export class RetirementInputError extends Error {}
export function validateRetirementInputs(inputs: RetirementInputs): void;
export function quantile(sorted: ArrayLike<number>, q: number): number;
export function runRetirementSimulation(inputs, options?): RetirementResult;
```

**Simulation math (exact order — hand-computable tests depend on it):**

- `years = PLAN_END_AGE − currentAge`; one step per age from `currentAge`
  (step `t = 1..years`, the age at step `t` is `currentAge + (t − 1)`).
- Randomness: **mulberry32** (32-bit state, output `[0,1)` = `>>> 0 / 4294967296`)
  seeded with `seed`, feeding **Box–Muller** with `u1 = max(rng(), 1e-12)`
  (no redraw loop — the stream consumption stays fixed) and `u2 = rng()`:
  `z = sqrt(−2·ln u1) · cos(2π·u2)`. `options.drawNormal` replaces `z` in tests
  (the scripted-draw seam). Reproducibility is per JS engine (V8/Node); tests
  pin values on this repo's Node.
- Per step, in order: **grow** `p ← p·(1 + r)` with
  `r = clamp(expectedReturnPct/100 + (volatilityPct/100)·z, −1, +inf)` (a year
  cannot lose more than the portfolio holds); floor at 0; then the **flow**:
  - accumulation (`age < retirementAge`): `p += monthlyContribution × 12`;
  - retirement (`age >= retirementAge`): call the seam once —
    `gross = tax.grossWithdrawalFor(annualWithdrawal, { age, year: baseYear + t })`;
    `p >= gross` → `p -= gross`, else `p = 0` and the path is **ruined**.
- **Events** (M189, docs/58 §3): when `eventFlow` is supplied, `p +=
  eventFlow[t − 1]` then floor at 0 — applied AFTER the phase flow and
  BEFORE the ruin check, so a life event may empty the portfolio (or fund a
  same-year shortfall) but never push it negative. Absent, the step is
  skipped and every output stays byte-identical to M188.
- **Ruin** is sticky: a path whose value is `<= 0` after any step is ruined
  (it can never come back — a later contribution does not un-ruin it), and only
  paths with **no ruin** count toward `successPct = 100 · survived / scenarios`
  (rounded to 1 dp).
- Bands: for each step, collect the `scenarios` values, sort ascending and take
  **p10 / median / p90 by R-7 linear interpolation**
  (`h = (n−1)·q`, `sorted[⌊h⌋] + frac·(sorted[⌊h⌋+1] − sorted[⌊h⌋])`),
  rounded to cents. All three start at `startingPortfolio` (step 0 is
  deterministic) so the fan converges to the user's actual net worth today.
- `validateRetirementInputs` throws `RetirementInputError` (message names the
  offending field) for anything outside the bounds above; the page also clamps
  before calling, but the engine is the contract.

**Determinism:** same inputs + same seed (+ same `drawNormal` when supplied)
⇒ deep-equal results. Different seeds ⇒ different paths (the default draw is
never constant).

**Performance:** a step is a handful of float ops; the default run is
`2000 × 55 ≈ 110k` steps. Measured on this box (§Verification, loaded with
concurrent suite runs): default 2000×55 = **85–365 ms**, 10 000×55 = **248–659
ms** — box-load dependent, so the number is recorded, not pinned by a budget
test (AGENTS §2 latency-pin rule). The sim runs synchronously in a `useMemo`
over the committed assumptions — no worker needed at these sizes.

**Tax cost (t_1ab83b11):** each retirement step now calls
`caTaxEngine().grossWithdrawalFor` — `scenarios × (PLAN_END_AGE −
retirementAge)` = 2000 × 30 = **60 000 gross-ups per run**, measured at
**175 ms** for 60 000 calls in isolation on this box (vitest, varying
age/year). That sits on top of the 85–365 ms range above; still synchronous,
still sub-second at the default size, so no worker. The gross-up is pure and
deterministic, so nothing here is load-shaped beyond the existing box-load
dependence.

### 3. Tax seam — `frontend/src/lib/taxEngine.ts`

```ts
export interface TaxContext { age: number; year: number }
export interface TaxEngine {
  readonly id: string;    // "stub-flat" | "ca-engine" | … (readable badge)
  readonly label: string; // "Flat-rate stub (docs/57)" | "Canadian tax — ON"
  /** Gross withdrawal required so the retiree NETS `netSpend` after tax. */
  grossWithdrawalFor(netSpend: number, ctx: TaxContext): number;
}
export function flatRateTaxEngine(ratePct: number): TaxEngine;
```

- **Shipped (t_1ab83b11): the page constructs `caTaxEngine()`** from
  `frontend/src/lib/canadaTaxEngine.ts` (docs/58) — `id = "ca-engine"`,
  `label = "Canadian tax — ON"` (default province ON, mirroring docs/27
  `DEFAULT_PROVINCE`; no province input, docs/09/57 expose none). The badge
  under the assumptions panel renders that label. The withdrawal field is
  stated as after-tax, so the engine's `grossWithdrawalFor` gross-up is what
  the portfolio loses each retirement step.
- `flatRateTaxEngine(ratePct)` (default **0 %**, bounds 0..95):
  `gross = netSpend / (1 − rate/100)` — stays exported as the seam's
  reference/fallback implementation, still pinned by R5/R6 in
  `tests/retirementEngine.test.ts`. The page no longer passes it, and the
  stub's rate input was removed from the panel with it (a control that fed
  only the stub would be dead UI).
- The interface carries the two facts the real engine needs (`age`, `year`);
  `caTaxEngine` accepts them as `CanadaTaxEngineContext` (`year` required,
  `age`/`province`/`otherIncome` optional) — structurally the same seam, no
  cross-branch import. This feature never grows a Canadian tax table of its
  own: the tables live only in `canadaTaxTables.ts` (docs/58).
- **Two error classes, one boundary (t_daede909).** The engine throws
  `CanadaTaxInputError` (docs/58) — a *different* class from
  `RetirementInputError`, not a subclass — so `instanceof
  RetirementInputError` alone does not catch it. The page's `run()` catches
  **both** and returns `null` (the no-plan path, §4); any other error still
  rethrows, so a genuine defect stays loud. Before this, an engine refusal
  escaped `useMemo` → render, was caught by `RouteLoadError`
  (`App.tsx route()`), and the valid route showed its misleading *"the page
  could not be loaded, reload to fetch it fresh"* panel — security F1 on
  `178a4638` (crash reproduced at annual withdrawal ≥ $25,000,001 with the
  real engine; see §4 for the clamp that keeps it unreachable from the UI).

### 4. Page — `frontend/src/pages/Retirement.tsx`, route `/retirement`

Layout mirrors Forecast: page header, then two columns — assumptions panel
(`w-72`) and results. Uses only semantic tokens (docs/09 §tokens), `SkeletonRows`
while loading, `ErrorPanel` on query failure, `EmptyState` (icon + title +
hint + `preview={{ none: "<reason>" }}` + action — the tenth documented
none-preview exception, docs/09 §Empty states) when net worth is `<= 0` (there
is nothing to simulate, and inventing a positive portfolio would violate
docs/34 §1).

**Assumptions panel — fields, defaults, bounds** (all money inputs are
`type="text" inputMode="decimal"`, clamped on blur; percentages/ratios are
readable numbers):

| Field | Default | Bounds | Mask class |
|---|---|---|---|
| Current age | 40 | integer 18..90 | readable |
| Retirement age | 65 (knob: `<input type="range">`, max = `PLAN_END_AGE − 1` = 94) | integer `currentAge+1`..94 | readable |
| Monthly contribution (CAD) | from cashflow (§1) | >= 0 | **money → masked** |
| Annual withdrawal (CAD, 0–10,000,000) | from cashflow (§1) | 0..10,000,000 — clamped at parse time (engine-safe max, see below) | **money → masked** |
| Expected annual return % | 5.0 | -50..50 | readable |
| Volatility % (annual) | 15.0 | 0..100 | readable |
| Scenarios | 2000 | integer 100..10000 | readable |
| Seed | 42 | integer 0..2147483647 | readable |

Money fields seed from the fetched defaults the first time data arrives; the
user's edits always win afterwards. **The verdict is withheld until the
committed assumptions carry the seeded money defaults** (`seedCaughtUp`: the
skeleton stays up until `committed.contribution/withdrawal` equal the seeded
values, unless the user has already edited that field) — without that gate the
first paint would run the sim on the pre-seed zeros and flash a fake 100 % for
a beat. The panel renders the engine's own id/label badge (`Tax engine:
Canadian tax — ON` — the label is `caTaxEngine().label`, spec §3) so the seam
is visible. There is no tax-rate input: the stub's rate field was removed with
the swap (t_1ab83b11), because a control feeding only the stub would be dead
UI — tax comes from the tables in docs/58, not from a dial.

**Withdrawal clamp (t_daede909).** `MAX_ANNUAL_WITHDRAWAL = 10,000,000`
(exported from `Retirement.tsx` for the field label and R14): `parseForm`
bounds the typed withdrawal to `0..10,000,000`, and the label states the bound
(`Annual withdrawal (CAD, 0–10,000,000)`) so the control advertises it the way
the age and percentage fields do. The reason is the ca-engine's gross-up: it
doubles the gross candidate until it covers the net spend and refuses
(`CanadaTaxInputError`, code `input_too_large`) once that candidate would pass
the engine's $100,000,000 input ceiling — with the current tables that trips
at a net spend above **$25,000,000** (measured on this box: 25,000,000 OK,
25,000,001 throws, for every projection year 2026–2090). $10,000,000 is 40 %
of that reachable limit, so no value the field can hold reaches the refusal,
and whenever the typed text exceeds the clamp the field's `Using …` hint
echoes the effective figure back underneath it. The second line of defence is
§3's boundary: if the engine still refuses, `run()` maps the error to `null`
and the results column renders the no-plan copy below — never the route error
panel.

**Debounce:** every assumption edit updates draft state immediately (the input
is controlled and never lags typing) and commits to the simulation state
**300 ms** after the last keystroke (`SIM_DEBOUNCE_MS`) — "updates as you type"
with a stable render in between. Until the first human edit the delay is 0, so
the seeded defaults commit with the data instead of racing it; `userEdited`
flips on any field's `onChange` (range included) and arms the 300 ms delay for
everything after.

**Result column:**

- **No-plan message (t_daede909)** — when the committed inputs are refused
  (§3), the column renders copy 6 below as one `role="alert"` line
  (`data-testid="retirement-input-issue"`) instead of a verdict; the shimmer
  skeleton stays reserved for the seed gate (`seedCaughtUp`) above. A refusal
  must never reach `RouteLoadError`.
- **Probability of success** — big number, 1 dp (`data-testid="retirement-success"`),
  plus the readable count line: `X of N simulated paths never ran out of money
  before age 95.`
- **Net-worth timeline** — `ChartFrame` + `chartAriaLabel` (role=img, docs/12
  §7) around a recharts area band: `p10 → p90` as one `Area`
  (`type="monotone"`, `fillOpacity=0.18`, `strokeWidth={0}`) + `median` as a
  second `Area` (`fillOpacity=0.34`) + a `ReferenceLine` at
  `x = retirementAge`. X axis = age (ticks every 5 years), Y axis through
  **`chrome.moneyAxis`** (mask-aware, repaints on a mask flip — it becomes the
  tenth F-7 member). Tooltip uses `ChartTooltip` with `fmtCad` (reads the
  ambient mask).
- **Readouts** — median / p10 / p90 at `retirementAge` and at `PLAN_END_AGE`
  (money → masked).

**Copy (verbatim, load-bearing — asserted by tests):**

1. `Simulation, not a prediction.`
2. `Runs in your browser on the data you already track — no bank sync, no new data sources.`
3. `Tax on withdrawals is estimated with Canadian tax tables (federal + Ontario) — a simplification, not a tax filing.`
4. `Not financial advice.`
5. Panel seed note: `Same seed and assumptions always give the same result.`
6. No-plan message (refused assumptions, `role="alert"`):
   `These assumptions are outside the simulator's supported range — adjust them in the Assumptions panel and the plan will re-run.`

### 5. E2EE / Rule-8 disposition (docs/33)

The simulator **computes client-side, and that is the entire point of the
feature** (§Compute moves client-side is exactly the slot this occupies):
`lib/retirementEngine.ts` and `lib/taxEngine.ts` are rule-8 inventory rows
with **no server twin — lock-vacuous by design**, covered by
`tests/retirementEngine.test.ts`, and documented here. No key material is read,
written or needed. Nothing in the feature may be gated on a key being present
(deterministic guard pattern, docs/33 §Rule 8): it reads its two public data
queries and runs.

## Touch points

| Path | Role |
|---|---|
| `frontend/src/lib/retirementEngine.ts` | **new** — pure Monte Carlo engine (§2) |
| `frontend/src/lib/taxEngine.ts` | **new** — `TaxEngine` seam + flat-rate stub (§3; stub now the fallback, page runs `caTaxEngine`) |
| `frontend/src/lib/canadaTaxEngine.ts` | consumed, not modified — `caTaxEngine()` fills the seam (docs/58, t_52ed8d01) |
| `frontend/src/pages/Retirement.tsx` | **new** — page (§4) |
| `frontend/src/App.tsx` | route `path="retirement"` + lazy import (M76 pattern) |
| `frontend/src/components/shell/navGroups.ts` | nav row `Retirement` / `/retirement` / `PiggyBank`, in the Forecast group, after Forecast |
| `frontend/tests/m68Navigation.test.tsx` | `"/retirement"` in the PRIMARY_GROUP hrefs array (no count assertion exists — the array is `toEqual`-ed) |
| `frontend/tests/m100AccountsNav.test.tsx` | `{ label: "Retirement", route: "/retirement" }` in `PRIMARY_LINKS` (iterated, not length-pinned) |
| `frontend/tests/retirementEngine.test.ts` | **new** — math pins (R1–R6) |
| `frontend/tests/retirementPage.test.tsx` | **new** — defaults/render/debounce/mask pins (R7–R11), ca-engine path pin (R13) + route-boundary pins (R14 clamp, R15 engine error class) |
| `frontend/tests/f7MoneyAxisMaskRepaint.test.tsx` | tenth axis member + data routes |
| `frontend/tests/routeCodeSplit.test.tsx` | no edit needed (regex already matches `Retirement`) |
| `frontend/tests/m95EmptyStates.test.tsx` | `Retirement` becomes the tenth documented `preview={{none}}` exception (the pin is an exact set) |
| `frontend/tests/m140MaskSweep.test.ts` | no edit needed — the sweep picks the new page up; `Retirement()` calls `useMasked()` so its own money strings repaint (FanTooltip already subscribed for the tooltip) |
| `docs/33-end-to-end-encryption.md` | §Compute moves client-side bullet + 2 inventory rows |
| `docs/09-ui-ux.md` | page table row + routes sentence — and a pre-existing staleness fix: the count said 29 while `src/pages/*.tsx` already held 30 (`/forecast` was missing from the routes list too); now 31 with `/forecast` + `/retirement` listed |
| `docs/45-forecasting.md` | scope lines: Monte Carlo now lives at `/retirement` (docs/57) |
| `docs/46-display-masking.md` | §D3 "NINE call sites" → TEN (the f7 list grows) |
| `docs/10-roadmap.md` | new `## M188` section |
| `docs/57-retirement-simulator.md` | this spec |

No backend files, no `docs/01`/`02`/`08` edits (no endpoints, no tables).

## Named tests

All in `frontend/tests/`: R1–R6 in `retirementEngine.test.ts` (pure math),
R7–R11 and R13–R15 in `retirementPage.test.tsx` (the page over its two reads).

- **R1 determinism** — same inputs + seed ⇒ `successPct` and all three bands
  deep-equal; different seed ⇒ at least one band value differs.
- **R2 ruin on a fixed series** — with `drawNormal` scripted to `0` (so
  `r = mu` every year) and a withdrawal that outruns growth, `successPct === 0`
  and the first retirement step is `0`; with a contribution schedule that
  outruns the withdrawal, `successPct === 100`.
- **R3 scripted-draw probability** — a `drawNormal` alternating `+1 / −1` over a
  sigma that makes one branch ruin and the other survive pins an exact
  `successPct` (e.g. 50.0 for N = 100) — the "ruin probability on a fixed
  series" AC.
- **R4 percentile math** — `quantile` on `[0..9]`: q=0.1 → 0.9, median → 4.5,
  q=0.9 → 8.1, endpoints inclusive.
- **R5 tax seam** — a recording `TaxEngine` stub receives exactly
  `scenarios × (PLAN_END_AGE − retirementAge)` calls with the right
  `{age, year}`; the portfolio drain uses `grossWithdrawalFor(...)`; raising
  the stub rate lowers `successPct` on the same seed.
- **R6 validation** — each bound violation throws `RetirementInputError`
  naming the field; `retirementAge <= currentAge` rejected.
- **R7 defaults align with data** — a fixed payload (net worth 50 000; three
  monthly buckets, income 7000, expenses 4000/6000/8000) through the live
  page pins `contribution = 1000.00` and `withdrawal = 72000.00`, plus that
  only the two report endpoints were called.
- **R8 render** — with mocked `getNetWorth`/`getCashflow`, the page shows
  `retirement-success` (`/\d+\.\d%/`), the count line, the four honesty
  strings, and one `svg.recharts-surface`.
- **R9 debounce** — the seeded verdict is pinned first (that fixture, seed 42,
  ca-engine gross-up: 160 of 2000 paths survive → **8.0 %** — the pre-wiring
  stub run was 278 → 13.9 %, t_1ab83b11; the pin fails if the seeding gate,
  the defaulting or the engine wiring ever drifts), then typing a new
  contribution does not change the rendered result before `SIM_DEBOUNCE_MS`
  and does after (fake timers scoped to this test; no `waitFor` while they are
  armed).
- **R10 mask** — `initMaskForSession()` on: money readouts show the mask text,
  `retirement-success` stays readable, and the assumption inputs keep their
  RAW editable values (docs/46 §D2: masking renders in output, never in inputs
  — ProjectionsPanel's "Start value" pattern).
- **R11 empty** — net worth `<= 0` renders the `EmptyState` (no fabricated
  positive portfolio, docs/34 §1).
- **R12 eventFlow** (M189, docs/58 §3) — wrong length and non-finite entries
  throw `RetirementInputError` naming `eventFlow`; a ruinous entry flips a
  deterministic surviving run to `successPct === 0` while the identical run
  without it stays 100; and a zero `eventFlow` under R3's scripted draws
  reproduces the exact 50/50 with the same draw count — the apply step is
  live, validated, and RNG-transparent.
- **R13 ca-engine path** (t_1ab83b11) — the rendered verdict equals a
  reference `runRetirementSimulation(..., tax: caTaxEngine())` on the same
  fixture (baseYear re-read from the clock at assertion time, never a date
  literal) **and differs from** the `flatRateTaxEngine(0)` run on that
  fixture, so a silent revert to the stub fails the first assertion while the
  second proves the two engines genuinely diverge here.

- **R14 withdrawal clamp** (t_daede909) — under the route's own
  `RouteLoadError` boundary (the same one `App.tsx route()` mounts), typing
  `78000000` into the withdrawal field after the debounce must NOT show
  `route-load-error`, must show the field's `Using 10000000.00` clamp hint,
  and must render a verdict equal to a reference run at `10,000,000` (0.0 % —
  every path ruins at 10M/yr out of the 50k fixture, versus 8.0 % for the
  seeded 72k run), so the clamped figure, not the typed text, reached the
  engine. On `178a4638` this input crashed the route (F1).
- **R15 engine error class** (t_daede909) — the page's own seam instance
  (`TAX_ENGINE`, exported for this) is made to throw a real
  `CanadaTaxInputError` for a withdrawal the field allows; the route must
  answer with the no-plan copy (§4 item 6) and no verdict, never
  `route-load-error`. This pins the `run()` boundary itself: removing the
  `CanadaTaxInputError` arm turns this test red with the route error panel.

Plus the existing suite: `f7MoneyAxisMaskRepaint` (tenth member), `m68`, `m100`,
`routeCodeSplit`, `lockedVaultDegradation` (Rule-8 inventory rows), `vaultGate`.

## Verification

Frontend (from `frontend/`):

```bash
npx tsc --noEmit
npx eslint . --max-warnings 0
npx vitest run
npm run build
```

Backend (unchanged tree, from `backend/`): `uv run ruff format --check && uv run ruff check`
and `uv run pytest -q` (0 failed — the docs-only backend impact is nil, but the
AC requires the number).

Running stack: `docker compose up -d --build web`, then load
`http://localhost:8080/retirement` in headless Chrome
(`--headless=old`): success % + fan chart render, an assumption edit changes
the result after the debounce, no console errors, no network calls beyond the
two report reads (verify in the network log).

Record here after the run: engine timing for the default (2000 × 55), gate
counts, and the browser screenshot path.

## Out of scope (v1)

- Canadian tax tables (t_52ed8d01 fills the seam); CPP/OAS/RRSP-vs-TFSA
  account treatment; contribution-room rules.
- Bank sync, new data sources, any new endpoint.
- Persisted assumptions / saved scenarios (page state only; a follow-up may
  add a scoped storage key through `scopedKey`).
- Inflation, sequence-of-returns guards, decumulation strategies beyond a flat
  annual withdrawal.
- Help guide for the page (docs/32 unchanged).
