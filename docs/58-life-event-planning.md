# 58 — Life-event planning (events reshape the retirement plan)

## Context

Kilter's plan asks "what if?" as EVENTS on a timeline — buy a home, have a
kid, take a sabbatical, retire early — and shows each event's year-by-year
impact next to the headline the user already has: the retirement success rate
(docs/57). fintrack has two what-if surfaces today and neither answers this:

- **docs/45 (forecast)** ships five typed events (`house_purchase`,
  `career_break`, `salary_change`, `one_off_cash_event`,
  `recurring_cost_delta`) but it is deliberately DETERMINISTIC: one path, no
  ruin probability, server-persisted scenarios.
- **docs/57 (retirement simulator)** ships the Monte Carlo success rate but
  takes only scalar assumptions — no per-year events.

This doc adds the event layer on top of the simulator: an extensible,
client-side registry of life events, each carrying cash-flow assumptions that
reshape the plan year by year, with the success rate shown **with and without**
the event set. Card `t_6cad38b0`, gated on `t_5534e54f` (M188) as its
substrate.

**Everything is client-side — no new endpoints, no new tables, no new network
calls, no backend code.** The page keeps making exactly the two reads M188
already makes (docs/57 §1); events are page state, like the assumptions
themselves. docs/45's event set is untouched and stays the deterministic
decision surface.

## Approach

### 1. Year ↔ step mapping (exact — the tests and the table depend on it)

The engine's step convention (docs/57 §2): step `t = 1..years`, where
`years = PLAN_END_AGE − currentAge`; the loop's age at step `t` is
`currentAge + (t − 1)`; the tax seam receives `year = baseYear + t`;
`baseYear` is the page's current calendar year. The result's `ages[]` array
labels step `t` as `currentAge + t` (that is what the chart draws).

An event's `year` therefore maps to step **`i = year − baseYear`** and is
valid iff `1 ≤ i ≤ years` (i.e. `year ∈ [baseYear+1, baseYear+years]` — plan
flows start next year; step 0 is today's deterministic start and carries no
flow). Displayed timeline rows pair the two engine conventions:

```
row i (i = 1..years):  year = baseYear + i    age = currentAge + i   (== ages[] label)
                       phase = (currentAge + i − 1) < effectiveRetirementAge
                               ? "Accumulation" : "Retirement"
```

so the row's year is exactly the year the tax seam receives for that step,
the age column matches the fan chart's x labels, and the phase flips on the
step where withdrawals actually start (docs/57 named test R5 pins that step).
The one-year offset between the loop's internal age and the `ages[]` label is
the simulator's pre-existing convention; this feature adopts it, does not
change it.

### 2. Registry — `frontend/src/lib/lifeEventEngine.ts` (pure, no I/O, no clock)

```ts
export type LifeEventType = "home_purchase" | "new_child" | "sabbatical" | "early_retirement";

export interface HomePurchaseEvent {
  id: string; type: "home_purchase"; year: number;
  downPayment: number;          // CAD, one-time outflow in `year`, 0..5,000,000
  annualHousingDelta: number;   // CAD/year vs the spending already in the plan,
                                // SIGNED, -500,000..500,000 (negative = cheaper)
}
export interface NewChildEvent {
  id: string; type: "new_child"; year: number;
  annualCost: number;           // CAD/year extra spending from `year` on, 0..500,000
}
export interface SabbaticalEvent {
  id: string; type: "sabbatical"; year: number;
  durationYears: number;        // integer 1..10, starting at `year`
  extraAnnualSpending: number;  // CAD/year on top of normal spending, 0..500,000
}
export interface EarlyRetirementEvent {
  id: string; type: "early_retirement"; year: number;   // no other fields
}
export type LifeEvent = HomePurchaseEvent | NewChildEvent | SabbaticalEvent | EarlyRetirementEvent;

export interface LifeEventFieldSpec {
  key: "downPayment" | "annualHousingDelta" | "annualCost" | "durationYears" | "extraAnnualSpending";
  label: string;                // rendered with the bounds: "<label> (<min>–<max>)"
  kind: "money" | "int";
  min: number; max: number;
  defaultValue: number;         // prefilled in the add/edit form
}
export interface LifeEventTypeSpec { type: LifeEventType; label: string; fields: LifeEventFieldSpec[]; }
/** Ordered — the add-form's type dropdown renders from it, in this order. */
export const LIFE_EVENT_TYPES: readonly LifeEventTypeSpec[];
export function lifeEventType(type: LifeEventType): LifeEventTypeSpec; // throws on unknown

export interface EventPlanContext {
  baseYear: number;             // page's current calendar year
  currentAge: number;
  years: number;                // PLAN_END_AGE − currentAge
  retirementAge: number;        // the assumptions knob, PRE-event
  monthlyContribution: number;  // CAD/month (accumulation surplus)
  annualWithdrawal: number;     // CAD/year net spending (== the plan's expense level)
}
export class LifeEventInputError extends Error {}   // name = "LifeEventInputError"
export function validateLifeEvents(events: LifeEvent[], ctx: EventPlanContext): void;
export function effectiveRetirementAge(events: LifeEvent[], ctx: EventPlanContext): number;
export function buildEventFlow(events: LifeEvent[], ctx: EventPlanContext): number[];
export interface PlanYearRow {
  year: number; age: number;
  phase: "Accumulation" | "Retirement";
  startsHere: LifeEvent[];      // events whose year === row.year
  eventImpact: number;          // sum of every event's CAD effect in this row
  netFlow: number;              // base flow + eventImpact (display convention, net of tax)
}
export function planTimeline(events: LifeEvent[], ctx: EventPlanContext): PlanYearRow[];
```

**Registry defaults** (what the form prefills; bounds are enforced by
`validateLifeEvents`, which reads them from the registry — one source):

| type | fields (label · kind · bounds · default) |
|---|---|
| `home_purchase` ("Home purchase") | `downPayment` (money, 0–5,000,000, **100,000**) · `annualHousingDelta` (money, −500,000–500,000, **0**) |
| `new_child` ("New child") | `annualCost` (money, 0–500,000, **12,000**) |
| `sabbatical` ("Sabbatical") | `durationYears` (int, 1–10, **1**) · `extraAnnualSpending` (money, 0–500,000, **10,000**) |
| `early_retirement` ("Early retirement") | — (the year is the whole event) |

Adding a fifth event type is ONE registry entry plus its arm in
`buildEventFlow` — the form, the bounds, the list labels and the validation
all render from the registry.

### 3. Cash-flow semantics (exact)

`buildEventFlow(events, ctx)` returns an array of length `ctx.years`;
entry `i−1` is the net CAD delta applied **at step `i`, after the phase flow,
before the ruin check** (docs/57 §2's order becomes grow → flow → events →
ruin; an event delta may not be silently dropped). It validates first (same
rules as `validateLifeEvents`), then folds every event in — deltas are
additive, so event ORDER NEVER MATTERS (pinned):

- **home_purchase** at `i`: `flow[i−1] -= downPayment`; then for every
  `t ≥ i`: `flow[t−1] -= annualHousingDelta` (a negative delta ADDS cash —
  the new housing costs less than the spending already in the plan).
- **new_child** at `i`: for every `t ≥ i`: `flow[t−1] -= annualCost`.
- **sabbatical** at `i` for `durationYears` (clamped to the plan horizon):
  - while the loop age `currentAge + t − 1 < effectiveRetirementAge` there is
    no income, so the portfolio funds the plan's own expense level plus the
    event's extra spending **on top of** the base `+contribution×12` the engine
    would add:
    `flow[t-1] -= (monthlyContribution × 12) + annualWithdrawal + extraAnnualSpending`
  - once the loop age reaches retirement the base flow already draws the
    expense level, so only `flow[t-1] -= extraAnnualSpending` applies.
- **early_retirement** carries no flow of its own — it moves the phase switch
  (§4).

The engine applies `eventFlow[t−1]` by `p += eventFlow[t-1]`, floors at 0, and
runs its STICKY ruin check unchanged: a one-time outflow that empties the
portfolio ruins the path exactly like any other zero year (no borrowing is
modelled — outspending the portfolio is a failed plan, and the UI never
pretends otherwise).

### 4. Early retirement = the phase switch, not a flow

`effectiveRetirementAge(events, ctx)` = `min(retirementAge knob, e)` where
`e = currentAge + (year − baseYear) − 1` for the single `early_retirement`
event (the loop age at the event's step — so withdrawals begin ON the event's
year, matching R5's switch arithmetic). Rules enforced by
`validateLifeEvents`:

- **at most one** `early_retirement` event (`only one early_retirement event
  is allowed`);
- `year ∈ [baseYear+2, baseYear+years]` — the switch needs loop age ≥
  `currentAge + 1`, which is also the engine's own lower bound
  (`early_retirement year must be an integer in <min>..<max> (got <year>)`);
- the event must be EARLY: `e < retirementAge` knob, else
  (`early_retirement year must fall before retirement age <knob> (got <year>)`);
- a sabbatical must START before the effective retirement age
  (`sabbatical must start before retirement (year <year>)`); its duration may
  run past it (the post-retirement rule in §3 takes over).

Other validation (all thrown as `LifeEventInputError`, message names the
offending field, no doc references in any message — `tests/jargonLeaks`):

- `event id must be a non-empty string` · `duplicate event id <id>` ·
  unknown type → `unknown event type <type>` (via `lifeEventType`);
- `year must be an integer in <baseYear+1>..<baseYear+years> (got <year>)`;
- per field: `must be an integer in <min>..<max> (got <v>)` (int) or
  `must be a number in <min>..<max> (got <v>)` (money — fractional cents
  allowed; the page's own assumptions clamp the same way).

The page validates BEFORE an event enters the list (add/save), so the list is
always valid; it re-validates against the COMMITTED assumptions on every
render (`tests` cover the stale case: raising `currentAge` shrinks the
horizon and can push an existing event's year out of range → the error shows,
the plan falls back to the baseline, nothing crashes).

### 5. Page — `frontend/src/pages/Retirement.tsx` (same route, two new cards)

No new route, no nav change. Inside the existing results branch (after the
fan chart):

- **Life events card** — the type dropdown (registry order), `Event year`
  (starts EMPTY with the valid `<min>–<max>` range in its label — never a
  stale date literal; blank on submit falls back to the type's minimum,
  `baseYear+1`, or `baseYear+2` for `early_retirement`), the type's fields
  prefilled from the registry defaults in CANONICAL text (money `toFixed(2)`,
  so a pristine field shows no `Using X` hint; a blank field falls back to its
  default on submit), `Add event` (`Save event`/`Cancel` while editing), the
  event list (`<li data-testid="life-event-item">` with per-field summaries —
  money through `formatCad`, so it masks — plus `Edit <type> <year>` /
  `Remove <type> <year>` buttons), the validation error
  (`data-testid="life-event-error"`, `role="alert"`), and — while events
  exist — the delta line:

  `Success with events: <X>% · without events: <Y>% · delta <±D> pp`
  (`data-testid="life-event-success-delta"`; D = with − without, 1 dp, signed;
  percentages stay readable under a mask, docs/46 §D2).

  `retirement-success` (the headline) shows the WITH-events verdict — it is
  the plan the user is looking at; the baseline lives only in the delta line.
- **Year-by-year cash flow card** — `data-testid="life-event-timeline"`, one
  row per plan year (`i = 1..years`) in a scroll box:
  `Year | Age | Phase | Starts this year | Event impact | Net cash flow`.
  Money cells are the only `td.tabular` (mask census rule — R10's
  `tbody td.tabular` sweep treats every one of them as money) and render
  through `formatCad`; `Starts this year` lists the labels of events whose
  year is that row, else `—`. `netFlow = phase base (+contribution×12, or
  −annualWithdrawal net) + eventImpact`.

**Two runs, not one:** with events valid and non-empty the page runs the sim
TWICE on the same seed — baseline (no `eventFlow`, knob retirement age) and
plan (with `eventFlow`, `effectiveRetirementAge`) — so the delta isolates the
events (same RNG stream both sides). With no events it runs ONCE (the
existing behaviour, no cost added). An invalid event set (stale against edited
assumptions) renders the baseline as the verdict, shows the error, and keeps
the timeline on the baseline rows — never a crash, never a silent ignore.

The chart's `ReferenceLine` and the readouts row use the **effective**
retirement age while events are valid and present, so the marker agrees with
the step where withdrawals actually start (same one-year chart-label
convention as the knob, §1).

Events commit IMMEDIATELY on add/save/remove (discrete actions — no
debounce; the 300 ms `SIM_DEBOUNCE_MS` stays what it was: assumptions only).

### 6. E2EE / Rule-8 disposition (docs/33)

`lib/lifeEventEngine.ts` is a rule-8 inventory module (the `*Engine*.ts`
clause): pure math over caller-supplied numbers, no endpoint, no key material,
no `../api/` import — lock-vacuous by construction, same disposition family
as `retirementEngine.ts`. A §"Compute moves client-side" bullet and inventory
rows ship with the feature (the gate in `tests/lockedVaultDegradation.test.ts`
reads the table back and fails either direction).

## Touch points

| Path | Role |
|---|---|
| `frontend/src/lib/lifeEventEngine.ts` | **new** — registry, validation, event flow, timeline (§2–§4) |
| `frontend/src/lib/retirementEngine.ts` | **edit** — optional `eventFlow?: number[]` on `RetirementInputs`, its length/finite validation, and the apply step between flow and ruin (§3); no behaviour change when absent |
| `frontend/src/pages/Retirement.tsx` | **edit** — events card, timeline card, dual run + delta, effective-retirement marker (§5) |
| `frontend/tests/lifeEventEngine.test.ts` | **new** — L1–L8 (per event type + registry + timeline) |
| `frontend/tests/lifeEventPage.test.tsx` | **new** — E1–E6 (add/remove/edit → timeline + delta, error, mask, no new fetches) |
| `frontend/tests/retirementEngine.test.ts` | **edit** — R12 (`eventFlow` validation + application) |
| `docs/57-retirement-simulator.md` | §2 interface + step order + named test R12 (the substrate changed shape) |
| `docs/33-end-to-end-encryption.md` | §Compute moves client-side bullet + rule-8 inventory rows (`lifeEventEngine.ts`, amended `retirementEngine.ts` rows) |
| `docs/09-ui-ux.md` | Retirement module-row: events card + timeline + effective marker |
| `docs/45-forecasting.md` | one scope line: the Monte Carlo's life events live at `/retirement` (docs/58); the five deterministic events stay here |
| `docs/10-roadmap.md` | new `## M189` section (appended) |
| `docs/58-life-event-planning.md` | this spec |

No backend files, no nav/route edits, no `docs/01`/`02`/`08` edits, no
`docs/46` edit (no new axis call site — the timeline is a table; the page
already subscribes to the mask), no new `preview={{none}}` empty-state
exception (the events card's empty hint is plain copy).

## Named tests

All in `frontend/tests/`.

**`lifeEventEngine.test.ts` (L1–L8, pure):**

- **L1 registry** — exactly the four types in order; every field's
  `defaultValue` inside its bounds; `early_retirement` has zero fields;
  `lifeEventType` throws on an unknown type.
- **L2 home purchase** — flow at the event year = `−downPayment −
  annualHousingDelta`, later years = `−annualHousingDelta`, earlier years
  `0`; a negative delta adds cash; `year` out of range and a negative
  `downPayment` each throw naming the field.
- **L3 new child** — `0` before the year, `−annualCost` from it to the plan
  end; `annualCost` above the registry bound throws naming the field.
- **L4 sabbatical** — accumulation years = `−(12×contribution + withdrawal +
  extra)`, years past the effective retirement age = `−extra` only; a
  duration running past retirement clamps at the horizon; `durationYears` 0
  and a sabbatical starting at/after retirement each throw.
- **L5 early retirement** — `effectiveRetirementAge = currentAge + (year −
  baseYear) − 1`; `year = baseYear+1` throws (before the plan's first
  switch); an event not before the knob throws; a second early-retirement
  event throws; with events present the timeline phase flips on the event's
  row.
- **L6 timeline** — `years` rows; first row `{baseYear+1, currentAge+1}`;
  last row `{baseYear+years, currentAge+years}`; phase flips exactly at the
  effective retirement row; `netFlow = base + eventImpact`; overlapping
  events sum in `eventImpact`; `startsHere` carries the right events.
- **L7 order independence** — the same events shuffled produce byte-equal
  `buildEventFlow` arrays.
- **L8 empty set** — zero flow, knob-driven phases, effective age = knob,
  baseline rows — the no-event plan is exactly the M188 plan.

**`lifeEventPage.test.tsx` (E1–E6, over the same two mocked reads as
R7–R11/R13):**

- **E1 no events** — timeline renders (`years` rows, first/last year
  re-derived from the clock, never a date literal), every impact `$0.00`, no
  delta line, no list items, and every fetch is still one of the two report
  endpoints.
- **E2 add** — one click on `Add event` (registry defaults) appends the
  list item, flips `retirement-success` to the with-events verdict, shows the
  delta line with `without events: 8.0%` (the same fixture R9 pins — 8.0 %
  under the wired `caTaxEngine`, 13.9 % before t_1ab83b11) and
  `with < without`, and writes the down payment into that year's timeline
  impact row.
- **E3 edit + remove** — editing the down payment changes both the item
  summary and the delta; removing restores `$0.00` impact, drops the delta
  line and empties the list.
- **E4 invalid** — an out-of-range year surfaces `life-event-error` with the
  message, adds nothing, and leaves the verdict on the baseline.
- **E5 mask** — `setMasked(true)`: every timeline money cell (the
  `td.tabular` census) is `MASK_TEXT`, the delta percentages stay readable,
  the form inputs stay raw (docs/46 §D2).
- **E6 stale horizon** — after an event exists, raising current age so the
  event's year falls outside the plan shows the error, falls back to the
  baseline verdict and keeps rendering (no throw).

**R12 (`retirementEngine.test.ts`)** — `eventFlow` of the wrong length and a
non-finite entry each throw `RetirementInputError` naming `eventFlow`; a
scripted-draw run with a ruinous `eventFlow` goes 100 → 0 success while the
identical run without it survives (the apply step is live, not decorative).

## Verification

Frontend (from `frontend/`, worktree with `node_modules` linked):

```bash
npx tsc --noEmit                 # exit 0 (recorded 2026-11-10)
npx eslint . --max-warnings 0    # exit 0 (recorded 2026-11-10)
npx vitest run                   # 1608 passed / 5 skipped / 1 failed —
                                 # the red is splitTransactions >
                                 # splitGrouping (17.0s, untouched file,
                                 # green 5/5 solo at 11.4s; same load-
                                 # shaped family as docs/10 M188's run)
npm run build                    # exit 0, Retirement-*.js chunk
```

Targeted first (before the full run): `npx vitest run tests/lifeEventEngine.test.ts
tests/lifeEventPage.test.tsx tests/retirementEngine.test.ts` → **39 passed /
0 failed** (L1–L8 + E1–E6 + R1–R12).

Backend (tree untouched):

```bash
cd backend && uv run ruff format --check && uv run ruff check   # clean
cd backend && uv run pytest -q     # 2893 passed / 7 skipped / 0 failed
```

Pitfall recorded: a `.env` copied into the worktree for the smoke changes
Settings under pytest — with it present, four M148/M149 live-server probes
go red (`401` expected, `200` served); A/B on one of them (present → red,
absent → green) pins the file as the cause. Keep `.env` OUT of the test
tree and run the smoke against a separately booted process.

Running stack (no docker on this box): `uv run uvicorn app.main:app --port
8010` from `backend/` (with `.env` present for the boot only), plus
`smoke_proxy_6cad.py dist 8090` (static `/` + reverse-proxy `/api` — the
nginx role), then real Chrome at `http://127.0.0.1:8090/retirement` with the
demo tenant (`POST /api/v1/auth/demo`, no password): **add** a $100k home
purchase → headline 98.9% → 98.2%, delta line `-0.7 pp`, timeline row
`2027 · 41 · Accumulation · Home purchase · -$100,000.00 · -$54,798.52`;
**edit** to $250k → delta `-96.7 pp`, row `-$250,000.00`; **remove** →
headline 98.9%, delta line gone, row `— · $0.00`; mask toggles
(`••••` ↔ `$250,000.00`); `window.__errs` empty through a full add/remove
cycle; resource-timing census = `reports/net-worth` + `reports/cashflow`
once each (life events add ZERO API calls). Screenshot recorded on the
card: `life_event_smoke_6cad.png`.

## Out of scope (v1)

- Persistence (events live in page state; a follow-up may persist through a
  `scopedKey` storage key, exactly as docs/57 defers its assumptions).
- Mortgages/loans, home appreciation, renovation, moving costs — the home
  purchase is a cash outflow plus an annual cost CHANGE, nothing else
  (docs/45's out-of-scope list stands for its own house event).
- Income-side event fields (parental-leave pay, severance), CPP/OAS timing,
  overlapping-spouse events, per-event currency.
- New endpoints / server storage / partner-visible plans.
- Touching docs/45's five deterministic event types or the forecast engine.
