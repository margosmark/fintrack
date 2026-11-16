# 36 — Cash-flow calendar (M52)

One month grid that puts the three forward-looking cash-flow sources the app
already computes — **bill reminders** (M40, docs/25), **dividend projections**
(M29, docs/24 / `services/dividend_calendar.py`) and **confirmed subscriptions**
(M170, `services/confirmed_subscriptions.py`) — on a single calendar.

**M170 — this surface states facts, so it only reads confirmed state.** Until
M170 the `subscription` series was the M27 DETECTOR's output verbatim
(`services/subscriptions.py::detect_subscriptions`), which is how a guessed
`Google*Youtubeprmium −$25.98` came to render directly under Mark's real
`Google *Youtubeprmium −$25.98` BILL — the same subscription in two colours,
from two sources — alongside a bare `Electronic Funds Transfer −$359.08`. A
detector is a PROMPT (M133's film.ca verdict, enforced on the dashboard cards
in M138); a calendar reading is a fact. See **§M170** below.

**M174 — the confirmed subscription set gains a THIRD source: a tag the user
names.** Mark: *"for recurring stuff can we make that based on tags since there
can be multiple"*. A group every one of whose rows carries one of the user's
nominated "recurring" tags is confirmed state, exactly like an accepted group —
same series, same kind, same dedup — and an EMPTY tag set means the source does
not exist. See **§M174** below.

**M170 follow-up — an amount is stated in the currency its rows were recorded in.**
The event's money field was `amount_cad`, and a confirmed group's
`amount_native` went straight into it: a USD subscription rendered as a CAD
figure nobody had computed. The wire now carries `amount` **plus `currency`**
as an all-or-nothing pair — CAD for a bill, the shared row/account currency for
a dividend, the group's own code for a subscription, and `null`/`null` when the
contributing dividend rows or group rows are mixed or unknown. Nothing converts
and nothing is relabelled; there is still no fx read on this path. See
**decision 15** below.

## Context

- `NEXT_WAVE_PLAN.md` §Wave 3 (M52). Lands after M49, so `/calendar` ships as
  one more lazy route (`docs/09` "Route loading & code-splitting").
- Every number shown is already computed somewhere else in the app. M52 adds
  **composition and presentation only**: no new detection, no new persistence,
  no migration, no network (`docs/07`: reports never fetch; `docs/12`:
  cache-only reads).
- The three sources are read through their own canonical functions so the
  calendar can never disagree with the surfaces that own them:
  - bills — `BillReminder` rows (`app/api/bill_reminders.py::list_bills`
    already filters `enabled = true`);
  - dividends — `app/services/dividend_calendar.py::compute_dividend_calendar`
    (the function `GET /reports/dividend-calendar` serves);
  - subscriptions — `app/services/confirmed_subscriptions.py::confirmed_subscriptions`
    (M170): the user's confirmed set — active `bill_reminders` rows plus the
    recurring groups `POST /transactions/recurring/accept` tagged, plus (M174)
    the groups whose every row carries one of the tags the user nominated in
    Settings. NOT the M27 detector: `detect_subscriptions` is a candidate engine
    (docs/24 §M27), and candidates belong on the prompt surfaces, not here.

## Decisions

1. **One endpoint, three reuse-only sources.** `GET /reports/cashflow-calendar`
   composes the functions above; it contains no pattern detection of its own.
   Dividends contribute exactly what M29 already projects — **one
   `next_expected_date` per security** — not a re-derived series. Subscriptions
   contribute exactly `next_expected` per **confirmed** subscription (M170:
   an active bill or a group the user accepted; M174 adds a group the user
   tag-marked), not per detected one — see §M170 and §M174.

2. **`month` is optional; when present it must be a real calendar month.**
   - absent → the server's own current month (`today_et()` → `YYYY-MM`), the
     same defaulting style `GET /reports/dividends?year=` uses;
   - `2026-13`, `2026-00`, `2026-1`, `abc`, `2026-09-31`, or a year outside
     **1900–2200** → **422 `invalid_month`** (hand-validated in the handler, so
     the envelope carries a pinned, endpoint-specific code — never a bare 500
     and never the generic code);
   - the parameter omitted entirely is *not* an error (see above).
   The 1900–2200 band matches `GET /reports/capital-gains?year=` and bounds the
   bill-occurrence loop.

3. **Zero-filled days.** `days` has exactly `days_in_month(month)` entries in
   ascending date order, every one present with `"events": []` when nothing is
   scheduled. An empty month is a valid 200 with an all-empty grid — the client
   never renders an error for it.

4. **One "today", server-side (docs/29 F1d).** `today_et()`
   (`app/services/txn_filters.py`) is the only clock: it decides the default
   month, each event's `status`, and the `today` echo in the response. The
   browser does not derive "today" from `new Date()` for status or highlighting
   — a client in another timezone at 23:00 ET must not disagree with the API.

5. **Bills project forward from `next_due_date` by their stored cadence.** Since
   **M180** the whole rule is `services/bill_schedule.py` — the ONE bill
   recurrence rule (docs/25 §M180) — which this surface reads through the
   row-level `bill_occurrences(bill, start, end)`, exactly as `mark-paid` reads
   `next_due` and the confirmed-subscription query reads `bill_next_on_or_after`.
   The grid therefore can never clamp a schedule differently from the write that
   advances it; this module's private `_advance` and its local `_STEP_MONTHS` /
   `_MAX_STEPS` copies are gone, and so is
   `services/confirmed_subscriptions.py::advance_months` — two clamps is how the
   two surfaces drifted apart in the first place.
   - `monthly` → +1 month, `quarterly` → +3, `yearly` → +12, `one_time` → no
     repetition;
   - the day-of-month **clamps to the target month's length** (`2026-01-31`
     monthly → `2026-02-28`), because a calendar must land on a real date. The
     day itself is the schedule's **anchor** (`bill_reminders.anchor_day`, M180),
     so the clamp is transient: the grid steps `2026-02-28` → **`2026-03-31`** →
     `2026-04-30`, and a Feb-29 yearly bill returns to Feb 29 in the next leap
     year, instead of staying pinned to the 28th February clamped it to;
   - only occurrences `>= month_start` are emitted — **no backward
     reconstruction**: docs/25 decision 4 pins that bills do not track payment
     history, so months before a bill's `next_due_date` are not replayed from
     its cadence;
   - the fast-forward is a bounded loop (hard cap 10 000 steps,
     `bill_schedule.MAX_STEPS`, then the bill's remaining events are dropped —
     unreachable inside the 1900–2200 band, but pinned so a corrupt row can never
     hang a request);
   - an unparseable `next_due_date` skips that bill instead of raising
     (`bill_reminders.next_due_date` is a `VARCHAR(10)`). The write path that
     could store `2026-02-31` for a day-31 bill is **CLOSED by M180**:
     `mark-paid` answers **422 `invalid_due_date`** and writes nothing, and no
     advance can land on a day the target month does not have (docs/25 §M180
     decision 4).

6. **Signed, and stated in its own currency.** `amount` carries direction
   exactly the way `services/reports.py` splits cash flow (income when
   `cad_amount > 0`, expense otherwise): dividends are **positive**, bills and
   subscriptions are **negative**. Values are `Decimal` end-to-end and
   serialize as strings (`AGENTS.md` §2). Per source:
   | kind | amount | currency |
   | --- | --- | --- |
   | `bill` | `-bill.amount`, or **`null`** when the bill has no amount (variable bills) | `"CAD"` — bills are reporting-currency by schema (docs/25) |
   | `dividend` | `avg_amount`, unsigned — direction is positive by kind (M29's projected average of native cash rows; unchanged) | the one normalized `[A-Z]{3}` code shared by every contributing row, using `trade.currency or account.currency`; mixed/unknown rows withhold both `amount` and `currency` |
   | `subscription` | `-avg_amount` for a confirmed group = `round_half_even(Σ abs(amount_native)/n, 2)` over the group's rows (M170 — the same rounding M27's detector uses, so a figure the user has already seen on the prompt card does not move when they accept it); `null` never happens here (a group is only confirmed when it is money OUT, §M170) | **the group's own currency** — the one ISO code all its rows agree on (M170 follow-up, decision 15) |

   **No fx read is required at all**: bills are reporting-currency by schema,
   dividends retain their native average and state the one row/account currency
   shared by their contributing trades (or neither field when mixed/unknown),
   and a subscription's native amount is
   passed through untouched **together with the currency it was recorded in**
   — so this endpoint calls neither `services/fx.py` nor `GET /fx` (with or
   without `fetch=false`), not even the cache-only readers. There is no
   network path in this endpoint, and no conversion anywhere on it.

7. **`status` is day-relative, not source-relative.** `past` (event date <
   `today`), `today` (== `today`), `upcoming` (> `today`) — one enum for all
   three kinds, computed from `today_et()`. Bills have no paid/unpaid state to
   report (docs/25 decision 4), so a per-kind status vocabulary would be empty
   for two of the three sources.

8. **`source_id` identifies the row the event came from, and `kind` says how to
   read it** (it is a string because the three id spaces are not numbers):
   | kind | `source_id` | click-through |
   | --- | --- | --- |
   | `bill` | `str(BillReminder.id)` | `/bills` |
   | `dividend` | `str(Security.id)` | `/securities/{source_id}` |
   | `subscription` | the confirmed group's `payee` | `/bills?autofill=<payee>&amount=<abs amount>&due=<event date>` — the M55 draft hand-off (docs/25), written with only the params the calendar holds: `readBillAutofill` requires just `autofill` and degrades the rest (unknown/absent `frequency` → `monthly`), and the server stays the authority on submit. **`amount` is written only when `currency == "CAD"`** (M170 follow-up): a bill is CAD by schema (docs/25), so handing a USD figure to that form would create a wrong bill — the draft opens with an empty amount instead |
   Subscription identity is the payee within *this* user's own confirmed set
   (the same tenancy-by-construction argument as docs/25 decision 8). The
   autofill target is kept since M170 even though a confirmed subscription is
   usually a bill already — the click is also "go and manage this", and
   `POST /bills` remains the authority on what it creates.

9. **Ordering is deterministic.** Days ascending; within a day, events sort by
   kind rank (`bill`, `dividend`, `subscription`), then `label`, then
   `source_id`. Two sources can put an event on the same date; nothing here
   depends on dict/set iteration order.

10. **Tenancy and staging are inherited, not re-implemented.**
    `BillReminder` is queried with `user_id == current_user.id`; dividends go
    through `compute_dividend_calendar` (user-scoped `Security` select, docs/29
    F1c) and subscriptions through `confirmed_subscriptions` (this user's
    enabled bills plus this user's ACCEPTED rows, via the shared
    committed-only + IDENTITY predicates — docs/03, docs/44 §2). Staged import
    rows can never appear on the calendar; bills are manual rows and are never
    staged.

11. **Privacy mode is unchanged.** The endpoint is a read of the caller's own
    rows: no LLM, no external call, no `fx_rates` fetch, nothing gated by
    `privacy_mode`. `GET` stays open to the demo tenant (only mutations are
    blocked, `docs/20`).

12. **MCP is out of scope.** `docs/23`'s tool inventory is a manual list and
    recent feature milestones (M40 bills, M51 inbox) did not extend it; M52
    does not either. Reported as a known doc claim that stays approximate.

13. **M170 — the subscription series is CONFIRMED state, never a detector guess.**
    Confirmed means **the user asserted it**: an active `bill_reminders` row
    (`enabled = true`) or a recurring group `POST /transactions/recurring/accept`
    tagged (`transactions.is_recurring`). Both sources are read through the ONE
    query, `services/confirmed_subscriptions.py::confirmed_subscriptions`, so
    the calendar and the count can never drift apart. Everything else — the M27
    detector's read of the same ledger — is a PROMPT, not a fact: M133's
    film.ca verdict for the dashboard, extended to this surface here, because a
    guessed `Google*Youtubeprmium −$25.98` rendered directly under Mark's real
    `Google *Youtubeprmium −$25.98` BILL (one subscription, two colours, two
    sources) alongside a bare `Electronic Funds Transfer −$359.08`.

    **Dedup: the bill wins.** A merchant an ENABLED bill's match rule covers is
    answered by the BILL alone — the accepted group is dropped, so the same
    money renders once, under Bills. The comparison is **payee-only** through
    the shared `services/bill_match.py::payee_matches` rule: amount and category
    scope which PAYMENTS a bill counts (docs/25 §M116 D2), not whether a bill
    exists for the payee — exactly the line M165 draws on the prompt cards. M165's
    `normalize_payee_key` collapses case, whitespace and trailing branch
    numbers and **nothing else**, so `Google*Youtubeprmium` stays its own key on
    purpose: a normalize that guesses is two normalizes again.

    **A recurring CREDIT is not a subscription.** A group whose rows are all
    `amount_native >= 0` (a payroll deposit clears the recurring rule as
    happily as Netflix does) is dropped from this series — the signed
    money-OUT contract of decision 6 — while the prompt cards still show it.

    **The detector keeps its candidates, and now refuses statement-generic
    descriptors.** `services/subscriptions.py::detect_subscriptions` still
    feeds its two prompt-shaped callers (`POST /bills`' autofill resolution and
    `GET /subscriptions`) and skips any row whose normalized payee key starts
    with one of `STATEMENT_GENERIC_PREFIXES` — `ELECTRONICFUNDSTRANSFER`,
    `EFT`, `INTERACE-TRANSFER`, `INTERACTRANSFER`, `E-TRANSFER`, `TAXPAYMENT`,
    `PAYROLL` — before any amount or cadence fit is considered. Scope is this
    detector only: `services/recurring.py` (the M96/M138 prompt engine) and the
    dashboard's "Bills you probably have" card are **OUT OF SCOPE** and keep
    showing such candidates — they are prompts, and a candidate that is neither
    a bill nor accepted appears there and nowhere else.

    **An empty subscription series is reported, never silent.** The payload
    carries `confirmed_subscription_count` — the size of the user's WHOLE
    confirmed set, a fact about the **user**, not about this month — so the
    client tells "nothing confirmed yet" from "nothing this month" and renders
    the sentence itself (§Frontend): `No confirmed subscriptions yet — recurring
    candidates ask on /transactions before they land here.` when the count is
    0, otherwise `No subscription payments in <Month> <year> — a subscription
    appears here once it is confirmed: a bill, or a candidate you accepted on
    /transactions.`

14. **M174 — a TAG the user names is the third confirmed source.**
    `confirmed_subscriptions` UNIONS (active bills) + (accepted groups) +
    (tag-marked groups), and the query stays the ONE answer both readers call,
    so this surface and `confirmed_subscription_count` cannot drift.
    A tag-marked group is a group from `services/recurring.py::detect_recurring`
    — the same M96 fit, over the same trailing 400-day window and the same
    committed-only + IDENTITY population — **every one of whose rows carries at
    least one of the tags in `users.recurring_intent_tag_ids`**. The design, in
    the order the pieces matter:

    - **No magic tag names.** The set is an explicit list the user curates in
      Settings (docs/09 §Tags that mark recurring); nothing anywhere
      pattern-matches a tag's NAME. A `/sub|recurring|monthly|bill/i` heuristic
      would be a second, invisible normalizer whose verdicts the user could only
      discover by seeing a wrong calendar — the PM's ruling, enforced by there
      being no such code.
    - **An EMPTY set means the source does not exist** — not "matches nothing".
      `_tag_intent_groups` returns early, so no tag scan runs at all; `[]` is
      the feature's OFF state and docs/09's Settings card says so in words.
    - **ALL of the group's rows, not any** (the M96 consistency discipline). One
      untagged row in the run leaves it unconfirmed: a partly-marked run is
      ambiguity, and ambiguity is what turns a fact surface back into a guess.
    - **The math gate is untouched.** A tag is intent, never arithmetic: the
      ±15% band, the ≥3-occurrence floor and "every gap fits one cadence" still
      have to hold, so a tagged PAIR of charges is not a subscription. The
      group's next occurrence uses the same `_group_next_date` projection as
      accepted groups, including the shared whole-month rule and derived charge
      day anchor.
    - **Money OUT only** — decision 13's rule, re-used through `_all_credited`,
      so a tagged `PAYROLL` group cannot render an income line in a bills
      colour.
    - **One merchant answers once.** The tag source is skipped when its merchant
      is already answered by an active bill (`payee_matches`) or by an accepted
      group (the shared `normalize_payee_key`), so a group that is BOTH accepted
      and tagged keeps the `recurring` source — the user's explicit accept is
      the stronger assertion. A row carrying TWO nominated tags is still one
      row, its group still one group: multi-tag is one dimension, never many.
    - **A suppression still wins.** `recurring_suppressions` is read through the
      same `rekey_suppression` re-read the candidates route performs, so a group
      the user dismissed never returns as confirmed because a tag also matches
      it — "a decision cannot be re-asked" survives the new source.
    - **`SOURCE_TAG = "tag"` is a third `source` value on the shared dataclass,
      and the calendar renders it into the SAME `subscription` kind** this
      decision's series already uses, with the same `amount` convention
      (`-round_half_even(Σ abs(amount_native)/n, 2)`) and the same
      `source_id = payee`. The wire's `kind` vocabulary is therefore still
      `bill | dividend | subscription` — a client needs no third word.
    - **Nothing is persisted by tagging.** No `transactions.is_recurring` write
      happens, so removing the tag (from the setting or from the rows) is
      immediately reversible: this source is a read-time JOIN, not an
      assertion.

    **Where a tag-marked group does NOT appear:** the dashboard's "Bills you
    probably have" card (`GET /transactions/recurring/candidates?source=subscriptions`,
    docs/09 §Bills you probably have). That card is a PROMPT surface reading
    candidates; tagging a run says "this is recurring", not "make me a bill
    reminder", and the card keeps offering that.

15. **M170 follow-up — `amount` + `currency` are an all-or-nothing pair.**
    The calendar used to name its money field `amount_cad` and pass a
    confirmed group's `amount_native` straight into it: for a USD account the
    wire carried `amount_cad: "-11.99"` for a figure that was never CAD, and
    the page rendered it with a `$`. The amount is now `amount` and travels
    with the `currency` it is STATED in — `null` on both sides when there is
    no honest figure — so no surface has to guess a denomination. The rule, in
    one place (`services/confirmed_subscriptions.py`, which both readers
    call):

    - **A bill with an amount is CAD** — `bill_reminders` has no currency
      column; bills are reporting-currency by schema (docs/25). A bill with
      **no** amount (variable) states neither: a currency with no figure
      denominate nothing.
    - **A dividend states the rows' currency** — the normalized code shared by
      every contributing trade, using `trade.currency or account.currency`.
      Mixed or unknown rows yield `amount: null, currency: null`; the native
      mean is never converted or relabelled.
    - **A subscription is stated in its group's own currency** — the ONE ISO
      code (`[A-Z]{3}`, upper-cased) that **every** row of the group agrees
      on. Mixed-currency (a USD account and a CAD account under one payee) and
      unknown (a row whose `currency` is blank or not a code) both yield
      `amount: null, currency: null`: the group's figure is the mean of ALL
      its rows, so one row the query cannot denominate makes that mean
      undemonstrable. **The number is never relabelled and never converted** —
      a conversion needs the fx read this endpoint refuses (decision 6).
    - **All three sources obey it** — bills, accepted groups and M174's
      tag-marked groups (the rule is `_group_currency`, called by both group
      branches).
    - **The client renders the pair**: `formatMoney(amount, currency)`, so a
      USD figure paints as `-US$11.99` in en-CA and a CAD one as `-$11.99`;
      `null`/`null` paints the app's "no honest figure" dash (`—`), the same
      convention `formatMoney` documents for a null money field (docs/09
      §money displays). The click-through drops its `amount` param for
      anything that is not CAD (decision 8).
    - **The rule is per-row, not per-account and not per-user**: the group is
      what is being stated, and the rows are the evidence.

    *What this deliberately does not do:* it does not convert (no fx read on
    this path), it does not hide the event (a commitment with a date is still
    a fact worth showing — only the figure is withheld), and it does not
    invent a per-user "reporting currency" setting. Those would each be a
    different feature with its own spec.

## API

### `GET /api/v1/reports/cashflow-calendar?month=YYYY-MM`

Auth: `Authorization: Bearer …` (`get_current_user`); missing/invalid token →
**401** with the standard envelope. Read-only, so the demo tenant may call it.

Query:

| param | type | required | notes |
| --- | --- | --- | --- |
| `month` | `YYYY-MM` | no | defaults to the current `today_et()` month; otherwise 1900-01 … 2200-12 or **422 `invalid_month`** |

Response **200** (`application/json`):

```json
{
  "month": "2026-09",
  "today": "2026-09-28",
  "confirmed_subscription_count": 2,
  "days": [
    {
      "date": "2026-09-01",
      "events": [
        {"kind": "bill", "label": "Mapleview Rent", "amount": "-1400.00",
         "currency": "CAD", "source_id": "3", "status": "past"},
        {"kind": "subscription", "label": "SPOTIFY", "amount": "-11.99",
         "currency": "USD", "source_id": "SPOTIFY", "status": "upcoming"},
        {"kind": "subscription", "label": "DROPBOX", "amount": null,
         "currency": null, "source_id": "DROPBOX", "status": "upcoming"}
      ]
    },
    {"date": "2026-09-02", "events": []}
  ]
}
```

Field contract:

| field | type | meaning |
| --- | --- | --- |
| `month` | string, 7 chars | echoed `YYYY-MM` (the default when the param is absent) |
| `today` | string, 10 chars | `today_et()` as `YYYY-MM-DD` — the single day boundary (docs/29 F1d). Additive beyond `NEXT_WAVE_PLAN.md`'s indicative shape: without it the client would have to invent "today" in browser-local time to highlight the current cell. |
| `confirmed_subscription_count` | int | **M170 / M174:** the size of the user's WHOLE confirmed set (active bills + accepted recurring groups + tag-marked groups, deduped by the shared `normalize_payee_key`) — a fact about the **user**, not about `month`. The grid says which confirmed subscriptions fall in THIS month; this number says how many exist at all, which is what lets the client tell "no confirmed subscriptions yet" from "no subscription payments this month" and render the honest empty state instead of silence (decision 13). A tag-marked group counts here exactly as an accepted one does (decision 14). |
| `days[]` | array | exactly one entry per calendar day of `month`, ascending, zero-filled |
| `days[].date` | `YYYY-MM-DD` | never duplicated, never missing |
| `days[].events[]` | array | `[]` when nothing is scheduled that day |
| `events[].kind` | `"bill" \| "dividend" \| "subscription"` | StrEnum-style literal |
| `events[].label` | string | `bill.name` / `"{symbol} dividend"` / confirmed `payee` |
| `events[].amount` | decimal string \| `null` | signed per decision 6, **in `currency`**; `null` with `currency: null` when the source carries no honest figure — a variable bill, or dividend/subscription rows without one shared currency |
| `events[].currency` | ISO code \| `null` | **M170 follow-up:** the denomination of `amount` — `"CAD"` for a bill, the shared normalized trade/account currency for a dividend, and the group's own code for a subscription. Mixed/unknown dividend or subscription denominations yield `null` for both fields. All-or-nothing with `amount` (decision 15). |
| `events[].source_id` | string | per decision 8 |
| `events[].status` | `"past" \| "today" \| "upcoming"` | per decision 7 |

### Error envelope

| code | HTTP | when |
| --- | --- | --- |
| `invalid_month` | 422 | `month` present but not a real `YYYY-MM` month in 1900–2200. Message: `Invalid month: '<value>' — expected YYYY-MM, year 1900-2200`. |
| *(standard 401)* | 401 | no/invalid access token |

There is no other failure mode: the endpoint cannot 500 on a malformed stored
row (decision 5 skips it), never fetches, and never reads another tenant's data.

## Frontend (`/calendar`)

**Route.** `frontend/src/pages/Calendar.tsx`, wired exactly like the other 22
(`docs/09`): `pages.calendar = () => import("./pages/Calendar").then(...)`
plus `{ path: "calendar", element: route(pages.calendar) }` in
`frontend/src/App.tsx` — lazy, one chunk, `RouteFallback` while loading,
`RouteLoadError` on a failed chunk. `tests/routeCodeSplit.test.tsx` enforces
both directions (no eager page import, no unwired page).

**API wrapper.** `frontend/src/api/cashflowCalendar.ts`:

```ts
export type CalendarEventKind = "bill" | "dividend" | "subscription";
export type CalendarEventStatus = "past" | "today" | "upcoming";
export interface CalendarEvent { kind: CalendarEventKind; label: string;
  amount: string | null; currency: string | null;
  source_id: string; status: CalendarEventStatus; }
export interface CalendarDay { date: string; events: CalendarEvent[]; }
export interface CashflowCalendar { month: string; today: string; days: CalendarDay[];
  confirmed_subscription_count: number; }
export function getCashflowCalendar(month?: string): Promise<CashflowCalendar>;
```

**Query key.** One key = one `queryFn` (`docs/09` "Data fetching & query
keys"): `["cashflow-calendar", month]` with `month` normalized to the string
`"current"` when absent, so the default-month fetch and a `2026-09` fetch are
never confused for each other.

**Month state.** `month: string | undefined` in component state, starting
`undefined` (server decides the current month). The response's `month` is
authoritative for rendering; Prev/Next set an explicit `YYYY-MM`; **Today**
resets to `undefined` and refetches. The client never computes the current
month from `new Date()`.

**Grid.** 7 columns, **weeks start Monday** (ISO, matching docs/12's week
bucketing), weekday header `Mon … Sun`, leading blanks for the first weekday.
Each cell is a `<button>` labelled e.g. `September 3, 2026 — 2 events`, showing
the day number, up to three kind-colored dots and a `+N` overflow marker. The
cell for `today` (from the response, not the browser) carries
`aria-current="date"` and an accent ring. Dates are parsed from the ISO string
by splitting on `-` — never `new Date("YYYY-MM-DD")`, whose UTC midnight shifts
the weekday in negative offsets.

**Day popover.** Clicking a cell opens a portaled, fixed-position panel
(`createPortal(…, document.body)`, `role="dialog"`, `aria-label` = the date):
header with the full date, one row per event (kind dot + label + amount +
status), a legend of the three kinds, and an empty line `No cash-flow events`
for an empty day. `Escape`, a backdrop click and the close button all dismiss
it; a second click on the same cell toggles it. **The amount is rendered by
`formatAmount(amount, currency)`** (M170 follow-up): the pair is passed to
`formatMoney`, so a USD figure paints as `-US$11.99` and a CAD one as
`-$11.99`; a `null`/`null` pair paints `—` — the app's "no honest figure"
convention, never a fabricated `$0.00` (decision 15).

**Click-through** is a link per decision 8's table: `/bills` for a bill,
`/securities/{id}` for a dividend, and
`/bills?autofill=<encoded payee>&amount=<abs amount>&due=<date>` for a
subscription (only the params the calendar holds; `readBillAutofill` fills the
blanks, docs/25). The `amount` param is written **only when
`currency === "CAD"`** — the draft is a CAD bill, so a USD or unstated figure
is left out entirely rather than mislabelled.

**Tokens & motion.** Only semantic tokens (`bg-surface`, `border-border`,
`text-text`, `text-muted`, `text-accent`, `bg-warn`, `bg-gain`,
`focus-visible:ring-accent`) — no literal colors, so both themes hold the
documented contrast pairs; amounts render in default ink (the sign is part of
the string), the *kind* dot carries the color (`bill` → `warn`, `dividend` →
`gain`, `subscription` → `accent`). Animations use the shared
`staggerContainer`/`fadeUp` variants only (transform/opacity), and
`MotionConfig reducedMotion="user"` already honors `prefers-reduced-motion`.

**States.** loading → `Skeleton` grid cells (never spinner text); error →
`ErrorPanel` with the API message; a month with no events → the grid still
renders in full with the line `No cash-flow events in <Month> <year>` beneath
it (never an `EmptyState` replacing the grid). A month with **zero subscription
dots** → one muted line under the legend (`data-testid="calendar-subscriptions-empty"`),
worded from `confirmed_subscription_count` and never left to silence (decision
13): when it is 0, `No confirmed subscriptions yet — recurring candidates ask
on /transactions before they land here.`; otherwise `No subscription payments
in <Month> <year> — a subscription appears here once it is confirmed: a bill,
or a candidate you accepted on /transactions.` (e.g. `No subscription payments
in February 2026 — a subscription appears here once it is confirmed: a bill, or
a candidate you accepted on /transactions.`). The line is absent as soon as the
month shows one.

## Touch points

| path | change |
| --- | --- |
| `docs/36-cash-flow-calendar.md` | this spec, pinned before any code |
| `backend/app/services/cashflow_calendar.py` | new — composition + bill occurrence projection; since M180 the bill series calls `services/bill_schedule.py::bill_occurrences` DIRECTLY (this module's own clamp and the one-line `_bill_occurrences` pass-through are both gone — one recurrence rule, one call site) |
| `backend/app/services/bill_schedule.py` | new (M180) — the ONE bill-recurrence rule: `next_due` (the write), `occurrences` / `first_on_or_after` and the row-level `bill_occurrences` / `bill_next_on_or_after` (this grid and the confirmed set) |
| `backend/app/services/confirmed_subscriptions.py` | new (M170) — THE one confirmed-subscription query (active bills + accepted groups, dedup, sorted); since M180 its bill rows project through `bill_schedule.bill_next_on_or_after` DIRECTLY (the former shared `advance_months` clamp and the private `_next_bill_occurrence` replica are gone, not duplicated) |
| `backend/tests/test_bill_schedule.py` | new (M180) — the rule as arithmetic, as the API's writes (anchor pinning, leap years, the 422) and as the row-level contract these two readers call |
| `backend/tests/test_m170_confirmed_subscriptions.py` | new (M170) — confirmed-set composition, bill-wins dedup, credits, stale anchors, the statement-generic stoplist, tenancy |
| `backend/app/api/reports.py` | new `GET /cashflow-calendar` handler + `month` validation |
| `backend/app/schemas/reports.py` | `CashflowCalendarEvent` / `CashflowCalendarDay` / `CashflowCalendarReport` |
| `backend/tests/test_cashflow_calendar.py` | new — contract tests below |
| `backend/tests/test_cashflow_calendar_demo.py` | new — the demo-month acceptance below, in a hermetic `<suite-db>_demo` database (the `tests/test_demo_seed.py` pattern) |
| `frontend/src/api/cashflowCalendar.ts` | new — typed wrapper |
| `frontend/src/pages/Calendar.tsx` | new — month grid + popover |
| `frontend/src/App.tsx` | `pages.calendar` + `route(pages.calendar)` (M49 convention) |
| `frontend/tests/calendarPage.test.tsx` | new — contract tests below |
| `docs/08` | new row under *Reports — additional endpoints* + `invalid_month` in the error-code list |
| `docs/09` | `Calendar` row in the Pages table, `/calendar` in the route list, route count |
| `docs/10` | `M52 — Cash-flow calendar` section + the "Next wave" line |
| `docs/10` (M170 follow-up) | the `M170 follow-up — the calendar stated a USD amount as CAD` section |
| `backend/app/services/confirmed_subscriptions.py` (M170 follow-up) | `ConfirmedSubscription.currency` + `_group_currency` / `_stated_currency`; all three sources state their amount's currency or none |
| `backend/app/services/cashflow_calendar.py` (M170 follow-up) | events emit `amount` + `currency` (bill CAD; dividend row/account currency; subscription pass-through) |
| `backend/app/schemas/reports.py` (M170 follow-up) | `CashflowCalendarEvent.amount_cad` → `amount` + `currency` |
| `backend/tests/test_cashflow_calendar.py` (M170 follow-up) | currency cases: USD, mixed, unknown, no-fx, and the per-source query rule |
| `backend/tests/test_cashflow_calendar_demo.py`, `backend/tests/test_m174_recurring_intent_tags.py` (M170 follow-up) | demo dividend pairs follow each entry's row/account currency; CAD bill and subscription controls remain CAD |
| `backend/tests/test_calendar_recurrence_matrix.py` (M180+M170 consolidation) | new — the ACCEPTANCE MATRIX: month-end, leap-day, DST and currency boundaries, every row exercised against all three surfaces of the ONE rule (`next_due` / `occurrences` / `first_on_or_after` and their row-level readers), plus the accepted-group month-end regression; M174 tag-marked month-end behavior is pinned in `test_m174_recurring_intent_tags.py` |
| `frontend/src/api/cashflowCalendar.ts`, `frontend/src/pages/Calendar.tsx`, `frontend/tests/calendarPage.test.tsx` (M170 follow-up) | `amount` + `currency` pair, `formatAmount(amount, currency)`, CAD-only `amount` param in the draft hand-off |

Not touched: `auth.py`, `AppShell.tsx` / `MobileNav.tsx` (the sidebar entry is
deliberately out of this card's ownership — see "Out of scope"), E2EE slices,
`mcp_server/`, `bill_reminders.py`.

## Out of scope (reported, not hidden)

- ~~**No sidebar entry.**~~ **CLOSED by M68** (2026-09-29): `NAV_ITEMS` in
  `frontend/src/components/layout/AppShell.tsx` now carries a **Calendar** row
  between Reports and Taxes, and `/tax-projection` gained one beside Taxes.
  docs/09 §Routes is reconciled in the same commit.
- ~~**`_advance_due_date` day-31 clamp.**~~ **CLOSED by M180** (2026-10-12):
  `app/api/bill_reminders.py` could store `2026-02-31` on
  `POST /bills/{id}/mark-paid` for a bill due on the 31st. The schedule now lives
  in `bill_reminders.anchor_day` (migration `0054_bill_due_anchor`) and every
  write/read goes through `services/bill_schedule.py`, so an impossible value can
  no longer be stored; the migration repaired the rows the old path had already
  written, and `mark-paid` answers **422 `invalid_due_date`** (writing nothing)
  for a row something outside the schema layer corrupted — docs/25 §M180. M52's
  projection policy (clamp per decision 5, skip unparseable values) is unchanged
  and now comes from that same module.
- **Historical months are not reconstructed** (decision 5): past months show
  only what is still scheduled (i.e. usually nothing), because bills carry no
  payment history.
- **Dividend denomination follows the contributing ledger rows**: M29 keeps the
  unconverted native mean, and this grid states it only when every row's
  `trade.currency or account.currency` resolves to one normalized code. Mixed
  or unknown rows withhold the amount and currency together; a CAD row currency
  remains CAD.
- ~~**A recurring GROUP drifted off the shared recurrence rule.**~~
  **CLOSED:** `services/confirmed_subscriptions.py::_group_next_date` derives
  the schedule day from the newest effective charge on every read (no schema
  migration). Accepted and M174 tag-marked groups both use this projection. For
  monthly/quarterly/yearly cadences it calls
  `bill_schedule.first_on_or_after(base, frequency, charge_day, today)`, the
  same whole-month projection rule as bills. The newest stored
  `recurring_next_date` remains the base when present (including a user-edited
  date); the accept route still stores a real clamped next date, and the charge
  day prevents that clamp from poisoning later months. Tag-marked groups have no
  stored base, so their detector-derived first date is clamped only as the initial
  base; the charge-day anchor is retained thereafter. Existing accepted rows
  need no backfill. Day-based recurring cadences keep `next_expected_date`
  arithmetic. A January 31 group therefore projects to April 30, matching an
  identical bill; `test_month_end_accepted_group_uses_the_shared_schedule_rule`
  and `test_tagged_month_end_group_keeps_its_day_in_confirmed_set_and_calendar`
  pin the accepted and tag-marked confirmed/calendar paths.
- **No MCP tool** (decision 12).

## Verification

### Backend — `backend/tests/test_cashflow_calendar.py` (real Postgres, isolated `fintrack_m52_test`)

1. **Shape / zero-fill.** Fresh user, `month=2026-02` → 200; `month` echoed;
   `today` == `today_et().isoformat()`; `days` has 28 entries, ascending, every
   `events` a list; `2026-01` → 31 entries; `2026-12` → 31.
2. **Default month.** No `month` param → 200 and `month == today_et()[:7]`.
3. **Bill on its due date.** Monthly bill due `2026-09-05`, amount `16.99` →
   the `2026-09-05` day carries `kind="bill"`, `label` = bill name,
   `amount == "-16.99"`, `currency == "CAD"`, `source_id == str(id)`, `status`
   consistent with `today_et()`.
4. **Forward projection + clamping.** Monthly bill due `2026-01-31` →
   occurrences in `2026-02` = `2026-02-28` (clamped); in `2026-04` =
   `2026-04-30`; quarterly bill due `2026-01-15` → `2026-04-15`; yearly →
   `2027-01-15`; `one_time` → exactly one occurrence, never repeated.
   **M180 anchor (decision 5):** a monthly bill created on `2026-01-31` through
   `POST /bills` and advanced once (`mark-paid` stores `2026-02-28` with
   `anchor_day = 31`) lands on **`2026-03-31`** in `2026-03` — not the
   `2026-03-28` the drift rendered — and `2026-04-30` in `2026-04`; a yearly
   `2028-02-29` bill advanced four times comes back to `2032-02-29`; the grid's
   date for that bill equals the confirmed query's `next_expected` for it
   (`test_the_calendar_and_the_confirmed_query_agree_on_a_month_end_bill`); and
   a raw pre-M180 row (`anchor_day IS NULL`) keeps the stored day's own answer.
5. **No backward replay.** Bill due `2026-09-05`, ask `month=2026-08` → no
   bill events (decision 5).
6. **Disabled bill excluded**; `amount: null` bill → `amount` **and**
   `currency` are both `null` (decision 15).
7. **Dividend date = M29's date.** Seed committed dividend trades so
   `compute_dividend_calendar` projects `next_expected_date`; the calendar's
   dividend event sits on exactly that date in exactly that month, with
   `amount == str(avg_amount)` (bare decimal, **no `+` prefix** — e.g.
   `"118.40"`), `currency == "CAD"`, `source_id == str(security_id)`, label
   containing the symbol.
8. **Subscription date = the ACCEPTED group's next occurrence.** ≥2 same-payee
   committed charges the user accepted → the subscription event sits on
   `confirmed_subscriptions()[i].next_expected` for the `recurring` source (the
   accept route's stored `recurring_next_date` as the base, projected with the
   newest effective charge's day anchor for whole-month cadences until it is on
   or after `today`, §M170), with
   `amount == "-" + avg_amount`, `currency == "CAD"` (the CAD control),
   `source_id == payee`. The detector still
   sees the very same rows and still does not reach this surface — only an
   accepted group does (§M170).
9. **Validation.** `month=2026-13` → **422**, envelope
   `{"detail": …, "code": "invalid_month"}`; same for `2026-00`, `2026-1`,
   `abc`, `2026-09-31`, `1899-01`, `2201-01`; missing `month` → 200 (not 422).
10. **Tenancy.** User B's identical request contains none of user A's bills,
    dividends or subscriptions; unauthenticated → 401.
11. **Staging.** A *pending* (uncommitted) dividend trade does not produce a
    dividend event; after commit it does.
12. **Demo month (hermetic demo DB, `test_cashflow_calendar_demo.py`).**
    `POST /auth/demo` → for every month the seeded demo actually has events in,
    `GET /reports/cashflow-calendar` shows a bill event on each seeded
    `next_due_date` (docs/30) and a dividend event on each M29
    `next_expected_date`, all zero-filled, and the read answers 200 for the
    demo tenant while a mutation on the same token is still refused
    (`403 demo_account_restricted`). **Since M170 the demo's subscription
    series is EMPTY** — its confirmed set is exactly its seeded bills (docs/30
    seeds five, all enabled; the tenant never answered a recurring candidate),
    so the detector's Spotify/Netflix/utility candidates render nowhere,
    `confirmed_subscription_count` answers the enabled-bill count and only
    `bill` + `dividend` are observed. The acceptance is renamed
    `test_demo_month_shows_bills_dividends_and_no_unconfirmed_subscriptions`.
    The fetch seams are patched to raise *after* seeding, so a network call
    during the calendar read fails the test.

13. **M170 — only confirmed sources reach the series.**
    `test_m170_subscriptions_series_shows_only_confirmed_sources` (Mark's
    fixture: a YouTube BILL, an accepted Netflix group, the detector guessing a
    mangled `Google*Youtubeprmium` spelling and a raw `Electronic Funds
    Transfer −359.08`) → Netflix renders once as a subscription, YouTube once
    as a BILL (never twice, in two colours), and the EFT reaches neither the
    calendar (no month carries it) nor the detector (the statement-generic
    stoplist refuses it), while the whole confirmed set is exactly 2 entries
    (the bill + the accepted group — the bill-covered group does not add a
    third). The query's own contracts live in
    `backend/tests/test_m170_confirmed_subscriptions.py` (confirmed-set
    composition, bill-wins dedup across every spelling, recurring credits,
    stale `recurring_next_date` rolled forward, disabled bills and other
    tenants excluded, the stoplist as detector-only).
14. **M170 — one query, two surfaces.**
    `test_m170_the_shared_confirmed_query_answers_both_surfaces` → the month's
    `subscription` events equal `confirmed_subscriptions()`'s `recurring`
    entries in that month, event for event, and its `bill` events equal the
    same query's `bill` entries.
15. **M170 — the count distinguishes "nothing confirmed yet" from "nothing
    this month".**
    `test_m170_confirmed_count_distinguishes_nothing_yet_from_nothing_this_month`
    → empty ledger: count `0` and no subscription events; one bill: count `1`
    still with no subscription event (it renders under Bills); a detector-only
    group (charges, no accept, no bill): still count `1`, still no event, while
    `detect_subscriptions()` demonstrably sees it.

16. **M174 — a tag-marked group is confirmed, and it dates the calendar.**
    `backend/tests/test_m174_recurring_intent_tags.py`:
    `test_the_calendar_dates_a_tag_matched_group_it_never_billed` → a merchant
    with three monthly charges, every one of them carrying a nominated tag and
    no bill and no accept, produces `confirmed_subscription_count == 1` and one
    `subscription` event on `next_expected` with `amount == "-15.99"` and
    `currency == "CAD"` (a CAD tag-marked group states CAD too — the same
    `_group_currency` rule as an accepted one).
    The same file pins the source's own rules: the ALL-rows requirement (one
    untagged row → nothing), the empty set = OFF (and ON the moment the tag is
    nominated), two nominated tags on one row → still ONE group, a suppression
    winning over the tag, the bill-wins and accepted-wins dedups, the
    cadence/occurrence floor still applying to a tagged group, a tagged CREDIT
    group staying out of the series, and the `PATCH /users/me` write seam
    (whole-list round-trip, `[]` clears, a foreign id → `422
    invalid_recurring_intent_tag_ids`, a malformed id → `422 validation_error`).

17. **M170 follow-up — a USD group is stated in USD, not CAD.**
    `test_usd_subscription_is_stated_in_usd_not_cad` → two `-11.99` charges on
    a **USD** account, accepted → the month's `subscription` event carries
    `amount == "-11.99"` and `currency == "USD"`, and the confirmed entry
    itself is `(str(entry.amount), entry.currency) == ("-11.99", "USD")`. The
    figure is the rows' own `amount_native` — nothing converts it.
18. **M170 follow-up — mixed currency omits the amount.**
    `test_mixed_currency_group_omits_the_amount` → a `-11.99` CAD charge and a
    `-12.99` USD charge under ONE payee, accepted → the event still renders on
    its date with the payee as its label, but `amount` and `currency` are both
    `null` — the mean of two currencies is not a figure the query can state.
19. **M170 follow-up — an unknown currency omits the amount.**
    `test_unknown_currency_row_omits_the_amount` → a group of one CAD row and
    one row whose `currency` is blank → `(amount, currency) == (None, None)`,
    the same as a mixed group. One undemonstrable row is enough.
20. **M170 follow-up — no fx read on the path.**
    `test_the_amount_is_the_native_figure_never_a_converted_one` → with
    `services/fx.py::cached_cad_amount` and `latest_rate` patched to raise, the
    endpoint still answers 200 and still reports `("-11.99", "USD")` — the
    calendar never consults the rate book, cache-only or otherwise (the
    file-wide `_no_network` autouse fixture additionally fails any fetch).
21. **M170 follow-up — the rule lives in the ONE query, for every source.**
    `test_confirmed_query_states_the_currency_or_refuses_the_amount` → for a
    user holding a CAD bill, a variable bill (no amount), a CAD group, a USD
    group and a mixed group, `confirmed_subscriptions()` states exactly
    `{("Mapleview Rent","bill"): ("-1400.00","CAD"),
    ("Hydro (variable)","bill"): (None,None), ("NETFLIX.COM","recurring"):
    ("-15.99","CAD"), ("SPOTIFY","recurring"): ("-11.99","USD"),
    ("DROPBOX","recurring"): (None,None)}` — amount and currency are stated
    together or not at all, and both readers inherit it.
    `test_cashflow_calendar_demo.py` and
    `test_m174_recurring_intent_tags.py::test_the_calendar_dates_a_tag_matched_group_it_never_billed`
    are reconciled to the renamed field and assert `currency == "CAD"` on the
    demo bill/dividend events and the tag-marked event respectively.

### Backend — `backend/tests/test_calendar_recurrence_matrix.py` (the consolidated boundary matrix)

The M180 (recurrence) and M170 (currency) fixes each left their own module
(`test_bill_schedule.py`, `test_cashflow_calendar.py`). This one is the
consolidation: **68 cases** over the four shared boundary classes, every row
exercised against the SAME three surfaces of the ONE rule — the write path
(`next_due`, `POST /bills/{id}/mark-paid`), the grid (`occurrences` /
`bill_occurrences`, `GET /reports/cashflow-calendar`) and the confirmed set
(`first_on_or_after` / `bill_next_on_or_after`,
`services/confirmed_subscriptions.py`) — so a divergence fails in one place
instead of in whichever caller changed.

A. **The schedule matrix (9 rows × 3 files):** `monthly-31st`,
`monthly-30th`, `monthly-29th` in a common AND a leap year, `monthly-28th`
(the never-clamped control), `quarterly-31st`, `yearly-leap-day`,
`monthly-31st-unanchored` and `one-time`; each chain is hand-computed IN the
row. Per row: `next_due` walks exactly that chain; `occurrences` answers the
stored date plus the chain (and `[]` for any span before the stored date);
`first_on_or_after` answers each chain date from itself and the NEXT one from
the day after; and the two ROW-level readers answer for a real
`bill_reminders` row what the rule answers for `(start, frequency, anchor)` —
including the un-anchored legacy row.
B. **Write path × grid, on one row:** for every chained row, `POST /bills` →
`mark-paid` × N is stored (a real date, anchor kept) and the month of EACH
chain date is read back from the route — the grid draws the bill on exactly
the date the write stored (13 months for the 31st schedule, 5 for the
leap-day one).
C. **Confirmed set × grid:** for a `today` before, ON and after the first
chain date, `confirmed_subscriptions()`'s `next_expected` equals
`first_on_or_after(...)` and the grid draws it on the same date.
D. **`one_time` negative control:** `mark-paid` disables and keeps the date,
the grid drops it, the confirmed set has no next date once it has passed.
E. **DST (the class nothing pinned before).** `today_et()` is asserted to be
zone-aware at six instants — the two rows a fixed offset would get WRONG
(2026-07-15T04:30Z: a UTC-5 estimate answers 07-14; 2026-12-15T04:30Z: a UTC-4
estimate answers 12-15) and the four around both transitions — with the naive
reading computed in the row as the proof the row discriminates. Then the grid
itself: at 2026-03-08T06:30Z (a 23-hour ET day) the month default is `2026-03`,
`today` is the 8th, the day list is 31 distinct dates with the transition day
present exactly ONCE and a 31st schedule still on the 31st; at
2026-11-01T06:30Z (a 25-hour day) it is 30 distinct dates with `today` the 1st;
and at 2026-09-01T03:30Z the ET date is still 2026-08-31, so the default month
is August.
F. **Currency (10 boundary rows + 3 sweeps).** The group-currency matrix:
agreeing rows state their own code (`USD`, `CAD`, **`EUR`** — any ISO code, not
just the reporting one), a lowercase/padded token is normalized rather than
printed, and mixed (`CAD`+`USD`), blank, two-letter (`US`), symbol (`US$`) or
mixed-with-unknown rows state `null`/`null` on BOTH surfaces. Then the
invariant over one month holding every kind (CAD bill, variable bill,
dividend, CAD/USD/mixed groups): `(amount is None) == (currency is None)` on
every event, with the exact per-kind pair table beside it so the sweep cannot
pass on an empty grid; and the rate book patched to raise still answers the
native `("-11.99","USD")` — no fx read on the path.
G. **Consolidation, pinned by identity:** each reader's `bill_occurrences` /
`bill_next_on_or_after` / `next_due` IS the `bill_schedule` function object,
and `calendar.monthrange` appears in no other module on the bill path (the
clamp cannot be re-derived by hand again).
H. **Accepted-group month-end recurrence:**
`test_month_end_accepted_group_uses_the_shared_schedule_rule` pins the same
April 30 answer for the confirmed-set API and calendar grid as an identical
bill, for both a stored January 31 base and the actual accepted February 28
base.

**Mutation proof (the matrix is not decorative).** On a detached worktree at
`origin/main` (`c265cdd`), with this module copied in: baseline **68 passed**;
with `bill_schedule.advance_months` mutated back to clamping from the STORED
day (the pre-M180 rule) **8 fail** — the write-path chain rows and the
end-to-end write-path↔grid walks for `monthly-31st`, `quarterly-31st`,
`monthly-29th-common-year` and `yearly-leap-day` (the projection rows stay
green, which is the point: they step from `due` by whole periods, so they are
immune to the chaining bug and it is the write path that must catch it); with
`today_et()` mutated to a fixed **UTC-5** clock exactly the `2026-07-15` row
fails, and with a fixed **UTC-4** clock exactly the `2026-12-15` row fails —
each mutation lands on the rows that carry it, and nothing else moves.

### Frontend — `frontend/tests/calendarPage.test.tsx`

22. Zero-filled month renders the right number of day buttons; an empty month
    shows the "no events" line and **no** error panel.
23. Dots render per kind and the popover lists label/amount/status for the
    clicked day; `Escape` closes it.
24. Click-through: bill row navigates to `/bills`, dividend row to
    `/securities/{id}`, subscription row to `/bills?autofill=…` (with
    `amount=15.99` for the CAD fixture — the control).
25. Prev/Next change the fetched `month`; **Today** refetches with no `month`.
26. The cell for the response's `today` carries `aria-current="date"`.
27. **M170 confirmed-subscriptions empty state** (`describe "M170
    confirmed-subscriptions empty state"`) → count `0` with no dots renders a
    line containing `No confirmed subscriptions yet` and `/transactions`;
    count `5` with no dots renders a line containing `No subscription payments
    in February 2026` and `confirmed`; a month that DOES show a confirmed
    subscription renders no line at all.
28. **M170 follow-up — currency honesty** (`describe "M170 follow-up currency
    honesty"`):
    - a USD subscription renders `-US$11.99` and **not** `-$11.99` (en-CA's
      `US$` marker is what makes the two distinguishable on screen);
    - the CAD bill/dividend controls still render `-$16.99` / `$30.00`;
    - a `null`/`null` pair renders the payee with the `—` dash, never `$0.00`;
    - the USD row's link to `/bills?autofill=SPOTIFY…` carries **no** `amount`
      param (a CAD bill draft must not be prefilled with a USD figure) while
      still carrying `due=2026-02-10`;
    - the unstated row's link carries no `amount` param either.

### Backend — `backend/tests/test_calendar_currency_parity.py` (the pair, end to end)

The sibling suites pin what each surface says; this module pins the PATH — real
ledger rows, the REAL accept route, `confirmed_subscriptions()` and the HTTP
route in one test, compared against each other rather than against a literal.

29. **Both readers state the same `(day, kind, label, amount, currency)`.**
    `test_the_two_surfaces_state_the_same_amount_and_currency_for_every_source`
    → one user holding a CAD bill, a VARIABLE bill, a CAD group, a USD group, a
    mixed group and a group of one CAD + one blank-currency row, all pinned into
    one month → the month's `bill` + `subscription` events equal the confirmed
    query's own answer for that month, tuple for tuple (including
    `("SPOTIFY","-11.99","USD")`, `("DROPBOX",None,None)`,
    `("Hydro (variable)",None,None)`), `confirmed_subscription_count ==
    len(confirmed_subscriptions(...))`, and every event in the payload is
    all-or-nothing (`(amount is None) == (currency is None)`).
30. **The series' THIRD source obeys it too (M174 tag-marked groups).**
    `test_the_tag_marked_source_states_its_own_currency_too` → a USD group every
    one of whose rows carries a nominated tag (no accept, no bill) reaches the
    calendar as `("ICLOUD","-2.99","USD")`, equal to the confirmed query's own
    entry — the tag branch of `_group_currency` is pinned on the wire, not just
    on the CAD control in `test_m174_recurring_intent_tags.py`.
31. **The code comes from the recorded ROW, not the account or a constant.**
    `test_the_stated_currency_is_the_one_the_rows_were_recorded_in` → a CAD
    charge on a USD card and a USD charge on a CAD account both reach the wire
    with their own code (`CAD`/`USD`) and their unconverted mean.
32. **No fx read on the whole round trip.**
    `test_the_read_path_never_consults_the_rate_book` → with
    `services/fx.py::cached_cad_amount` and `latest_rate` patched to raise,
    `GET /reports/cashflow-calendar` still answers 200 with `("-11.99","USD")`.
33. **Dividend rows determine the event denomination without conversion.**
    `test_dividend_event_preserves_the_rows_native_currency` → two `85.00`
    dividend trades on USD-account `VTI` rows with NULL currency produce
    `("VTI dividend","85.00","USD")`: the native mean is unchanged, with the
    account fallback supplying the denomination. CAD and unnameable/mixed
    denomination cases are independently exercised with the upcoming M29 panel
    contract; no fx lookup is required for this calendar leg.

All five cases fail on the pre-fix baseline (`0d38a08`, detached worktree, the
module's own database): `5 failed` there, `5 passed` on the tree that carries
`bbd9b26`.

### Gates (AGENTS §4)

`ruff format --check` + `ruff check`; `pytest -q` on the isolated
`fintrack_m52_test` database through the example.internal tunnel; `tsc --noEmit` +
`eslint . --max-warnings 0`; `NODE_ENV=test npx vitest run`; `npm run build`
(entry chunk stays ≤ 700 000 B — the page ships as its own lazy chunk); doc
reconciliation for `docs/36`/`docs/08`/`docs/09`/`docs/10` (plus `docs/24`
§M27, since M170) in the same commit.
Merge, full gates, push, deploy and the running-stack browser smoke belong to
the integration card.
