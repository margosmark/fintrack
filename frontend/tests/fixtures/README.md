# Test fixtures

Golden vectors + frozen inputs. Nothing in this directory is loaded by the app
or by `vitest` (`vitest.config.ts` only collects `tests/**/*.test.{ts,tsx}`).

**M160 (P2-5) changed the "Consumed by" column for every fixture whose client
mirror retired:** the `tests/*Parity.test.ts` / `tests/s*Compute.test.ts`
readers that diffed a CLIENT engine against these goldens are deleted, and the
goldens became SERVER-ROUTE REGRESSION PINS (M158 Decision 2: "keep capture +
goldens … they become server-route regression pins"). The replay modules seed
the fixture's captured inputs into a throwaway tenant, replay the captured
request against the mounted route and byte-diff the answer.

| File | What it is | Consumed by |
|---|---|---|
| `s2a-vectors.json` | **S2a** — `GET /reports/spending` (12 shapes incl. `group_by=tag` + both drill filters), `/reports/spending/categories` and `/reports/cashflow` goldens + inputs, 29 cases across two tenants | `backend/tests/test_m160_replay_s2a_matrix.py` (29 replay cases, M160) + `capture.ts --slice s2a` (re-capture) |
| `matrix-vectors.json` | **M124 R3** — `GET /reports/category-tag-matrix` goldens + inputs, 6 cases (top-8 cut, `Other tags` spill, `Untagged`, raw-category rows) | `backend/tests/test_m160_replay_s2a_matrix.py` (6 replay cases, M160) + `capture.ts --slice matrix` (re-capture) |
| `s2b-vectors.json` | net-worth / investment-performance goldens (live server JSON, 11 ranges) | **replayed** — `backend/tests/test_m160_replay_s2b.py` (card `t_62afc8a5`): the 7 net-worth ranges + 4 investment-performance ranges replayed against `GET /reports/net-worth` and `GET /reports/investment-performance` at the capture's `captured_at`; its only reader was `tests/s2bReports.test.ts`, deleted by M160 |
| `s2c-vectors.json` | dividends goldens (4 dividend-year ranges) | **replayed** — `backend/tests/test_m160_replay_s2c.py` (card `t_62afc8a5`): `default`/`2026`/`2025`/`2018` replayed against `GET /reports/dividends` with the clock pinned to `captured_at` (the TTM window reads today); its only reader was `tests/s2cDividends.test.ts`, deleted by M160 |
| `s3-vectors.json` | **M48 S3** — rules-preview / subscriptions / budgets-progress goldens + inputs | `backend/tests/test_m160_replay_s3.py` (16 replay cases, M160) + `lockedVaultDegradation.test.ts` (the surviving `budgets.progress` lock-invariance pin) |
| `s4-vectors.json` | **M48 S4** — capital-gains (13 tax years) / ACB-pools goldens + inputs, 14 cases | `backend/tests/test_m160_replay_s4.py` (14 replay cases, M160) |
| `m77-vectors.json` | **M77** — cash-flow Sankey + total-return server JSON for 3 ranges, plus the frozen input rows (accounts / transactions / trades / categories / securities / prices / fx) they were computed from | **replayed** — `backend/tests/test_m160_replay_m77.py` (card `t_62afc8a5`): sankey 3 + total-return 3 replayed against `GET /reports/cashflow-sankey` and `GET /reports/total-return`; the Sankey's node ids embed captured category ids, so the golden is translated old→new before the diff; its only reader was `tests/m77Parity.test.ts`, deleted by M160 |
| `projection-parity.json` | **M65** — 7 `POST /projections/run` requests + the server JSON each produced (frozen, never re-captured). A bare `[{name, request, response}]` array, not the harness `{meta, datasets, cases}` shape | `tests/projectionEngine.test.ts` (client mirror — M160 did NOT retire `projectionEngine`, it was never on M158's list) + `backend/tests/test_projections.py` (server route, both directions) + `scripts/parity/parity.ts --slice projection` (live, as the demo tenant) |
| `calendar-failure-modes.json` | **t_742c9dc0** — one `CashflowCalendar` payload (`?month=2027-02`) captured from the real route: a 31st-anchored all-USD group on the month that lacks the 31st, `today` frozen to the 1st so every field is reproducible | `backend/tests/test_calendar_failure_modes.py` (byte-diffs the live route against it; re-capture with `CALENDAR_GOLDEN=1 uv run pytest tests/test_calendar_failure_modes.py`) + `tests/calendarFailureModes.test.tsx` (renders it through the real app: currency, mask flip, restore-null) |
| `household-vectors.json` | **M159** — `GET /reports/spending?scope=household` + `/reports/spending/categories?scope=household` goldens + inputs across a throwaway OWNER and PARTNER linked on one shared account: 12 cases (3 granularities, 3 drills, the breakdown, the narrow-scope contrasts, the degenerate owner seat, the privacy-mode mask) | `backend/tests/test_m160_replay_household.py` (12 replay cases, M160) + `capture.ts --slice household` (re-capture) |

## M77 fixture (`m77-vectors.json`)

Shape: `{captured_at, base_commit, reporting_currency, ranges, accounts,
transactions, trades, categories, securities, prices, fx, sankey, total_return}`

- `ranges` — `{full: 2025-01-01..2025-12-31, q2: 2025-04-01..2025-06-30,
  empty: 2024-01-01..2024-12-31}`; `empty` has no activity at all, so it pins
  the empty-Sankey shape (`nodes: []`, zeroed totals) and a `total_return`
  whose percentages are legitimately `0.00000000` — the portfolio IS valued
  every day, the value simply never moves. A window with no valued day at all
  is the OTHER case and returns `status: "no_data"` with `null` percentages
  (`backend/tests/test_total_return.py::test_empty_portfolio_is_no_data_not_zero`).
- `accounts` / `transactions` / `trades` / `securities` / `categories` /
  `prices` / `fx` are the RAW list-endpoint rows (staged rows already excluded
  by `committed_only=true` / the server's committed predicate) — everything an
  engine is allowed to see. `sankey` and `total_return` hold the SERVER's own
  JSON per range.
- Regenerate with `backend/scripts/capture_m77.py` (it drops and re-migrates
  its target database, so point `DATABASE_URL` at a throwaway — see the
  script's module docstring). Keys are written `sort_keys=True`, so the parity
  test compares canonical key-sorted JSON: values must match byte-for-byte,
  field insertion order may not.
- **Last re-captured for M132** (multi-currency trade-cash leg, docs/05
  §Portfolio value): trade cash became a per-CURRENCY balance valued at the
  day it is *valued* instead of a per-row-date conversion folded into one CAD
  number, so every `total_return` golden legitimately moved. The fixture's
  rows carry no `currency`, so the re-capture also pins that a NULL row
  currency behaves exactly as the pre-M132 engine did.

## M65 projection fixture (`projection-parity.json`)

Shape: a bare array of `{name, request, response}` — this file predates the
`{meta, datasets, cases}` harness, and M135 §4.1 forbids re-capturing a golden
to satisfy a parity gate, so `parity.ts --slice projection` ADAPTS it in
memory (`adaptProjectionFixture`) rather than rewriting it. The projection run
is pure request-body math (docs/12 §7 — no DB reads), so the adapted dataset is
empty and `captured_at_et` is inert: no server-side clock enters a response.

- `request` is the exact `ProjectionRequest` body; `response` is the exact
  `ProjectionResponse` `backend/app/api/projections.py::run_projections`
  produced for it (Decimal, 28 significant digits, ROUND_HALF_UP `_cents`).
- **Both directions are pinned from this one file**: `tests/projectionEngine.test.ts`
  runs the CLIENT mirror against it; `backend/tests/test_projections.py::
  test_run_matches_the_frozen_m65_golden` runs the SERVER route against it
  (7/7, 0 diffs); and `node frontend/scripts/parity/parity.ts --slice
  projection` posts every request to a RUNNING stack **as the demo tenant**,
  which is the invariant M135 §2 K-12 restores — a 403 there is a `$.status`
  diff and exit 1, never a pass.
- The one deliberate client/server deviation (a gain of exactly zero prints
  `0.00` where Python's Decimal prints `-0.00`, docs/12 §7) does not appear in
  these seven cases, so the goldens compare byte-identically both ways.

## S2a + matrix fixtures (`s2a-vectors.json`, `matrix-vectors.json`)

Same `{meta, datasets, cases}` shape as the M48 fixtures below, captured
**2026-10-11** by `capture.ts --slice s2a` / `--slice matrix` from the deployed
stack. These close M135 §4.4-1 ("S2a shipped with no golden vectors at all —
capture first, then swap") and §4.3 ("`GET /reports/category-tag-matrix`:
capture required, no fixture today"): until they existed, the three report
routes and the matrix had a client engine with **nothing to diff it against**,
which is why M137's report flips kept the client engines as fallback — and why
the captures had to exist BEFORE M160 could retire those engines (M135 §4.1: no
parity pass ⇒ no retirement).

- **Two datasets per fixture.** `demo` is the demo tenant (read-only GETs,
  370 committed rows, 2025-10-01..2026-09-28). `scratch` is a throwaway
  tenant **registered, seeded and deleted during the capture** (verified 204).
  The demo carries *no* tags, *no* uncategorized rows, *no* linked transfers
  and *no* non-CAD transactions, so `group_by=tag`, the `uncategorized` drill,
  a multi-tag row, the root/child drill and the fx path would all have been
  captured as empty answers. M135 §4.4-3's rule applies: extend the capture,
  never relax the comparison.
- **The synthetic ledger is one deterministic 20-row set**: a parent/child
  category pair (`parity-root` → `parity-child`), a two-tag row, two
  uncategorized rows, one USD row (dated on a cached fx rate's own date, so
  the server resolves `cad_amount` at write time and the capture asserts it),
  one income row, one `0.00` row, and one row per tag `tag-e`…`tag-l` so the
  matrix's top-8 cut has a deterministic ranking AND genuinely spills.
- **Ranges are fixed calendar windows**, never today-relative: the engines
  read `from`/`to` out of the request path, so a re-capture only moves when
  the data moves.
- Regenerate with `node frontend/scripts/parity/capture.ts --slice s2a` (or
  `--slice matrix`) against the live stack — never by hand. `capture.ts`
  asserts every expected status, so a broken response can never become a
  golden.

### What these two fixtures do NOT pin

- **M143's `_is_transfer` exclusion** (`services/reports.py`: the two spending
  readers skip linked transfers, cash flow keeps them). Neither tenant carries
  a linked transfer, and the client engine does not implement the exclusion —
  a divergence there would NOT show up in either run. Named here rather than
  papered over; the synthetic seeder is the obvious place to add a linked pair.
- `format=csv` on any of the four routes (the JSON contract is what the client
  engine reproduces).

## Household fixture (`household-vectors.json`, M159)

Captured by `capture.ts --slice household` against a running stack: it
registers a throwaway **OWNER** and **PARTNER**, seeds both ledgers (deliberate
category-id collisions across the two tenants), links them on ONE shared
account, and freezes the server's `scope=household` answers as the PARTNER —
the only seat the Owner lens runs for — plus three controls: the narrow scope
over the same rows, the degenerate OWNER seat (M90's grant is one-directional,
docs/43 §2), and a privacy-mode owner whose household must collapse to the
partner's own rows (docs/43 §9 D4). Both tenants are deleted at the end of the
run, on success or on a throw.

`engines/household.ts` (deleted by M160 — it was the client-comparison half)
reproduced each golden with the shipped client engines over
`ownTransactions ∪ projectionTransactions`, with the OWNER's category tree
layered over the partner's exactly as docs/08's namespace rule says. That
half is gone; `backend/tests/test_m160_replay_household.py` now seeds the
fixture's two tenants, replays all 12 captured requests against the mounted
route and byte-diffs them. The tag dimension is intentionally absent: the
projection does not serve the owner's tags to the partner, so a tag-mode
household golden would compare a population neither side can be handed — that
path is pinned by `backend/tests/test_reports_household.py` instead.

- Capture: `node frontend/scripts/parity/capture.ts --slice household`
- Diff: **retired as a client comparison by M160** — `parity.ts --slice
  household` now refuses loudly (exit 4, "SLICE RETIRED") instead of
  pretending to compare. The replay gate is
  `cd backend && uv run pytest tests/test_m160_replay_household.py -q`
  (12/12), and it carried M159's 12/12, 0-diff result forward.

## M48 parity fixtures (`s3-vectors.json`, `s4-vectors.json`)

Shape: `{ meta, datasets, cases }`

- `meta` — provenance: `base_commit` (the commit the goldens reflect),
  `captured_at_et` / `captured_at_utc`, `reporting_currency`, the exact capture
  and diff commands, and `notes` (deviations and docs discrepancies found while
  capturing — read them before trusting a number).
- `datasets` — frozen **inputs**, keyed by tenant (`demo`, and for s3 `scratch`).
  This is all an engine is allowed to see: `EngineCall` carries
  `{meta, dataset, datasetName, request}` and never `expected`.
- `cases[]` — `{ id, dataset, request: {method, path, body}, expected: {status, body} }`.
  Status is part of the contract: a case may be a `422` error envelope and the
  client engine must reproduce `{detail, code}` exactly.

### S3 goldens are FROZEN (M48-S3 removed the routes they captured)

`POST /rules/preview` and `GET /subscriptions` no longer exist on a
post-M48-S3 stack, so `capture.ts --slice s3` **cannot** be re-run against
production any more — it now stops with an explicit
"route was removed in M48-S3" error instead of recording a 405/404 as a
golden. The S3 fixture stays valid as the frozen server JSON it always was
(`meta.base_commit`); re-capturing it would require a checkout of that commit.
Since M160 it is replayed at the MOUNTED routes by
`backend/tests/test_m160_replay_s3.py` (the routes M137 re-mounted), with the
capture's `captured_at_et` injected so the `today_et()`-derived windows stay
put. `GET /budgets` is still live, but its golden is only regenerated together
with the fixture as a whole.

### Commands (from the repo root)

```bash
node frontend/scripts/parity/parity.ts --self-test            # diff machinery self-test
node frontend/scripts/parity/parity.ts --verify --slice s3    # fixture structure only (works for every kept fixture)
node frontend/scripts/parity/parity.ts --slice projection     # the ONLY slice still runnable here (live; needs a stack)
node frontend/scripts/parity/capture.ts  --slice s3           # re-capture (overwrites the fixture)
node frontend/scripts/parity/capture.ts  --slice s4
node frontend/scripts/parity/capture.ts  --slice s2a          # live re-capture (needs a stack)
node frontend/scripts/parity/capture.ts  --slice matrix
node frontend/scripts/parity/capture.ts  --slice household
```

**M160:** `--slice s2a|matrix|s3|s4|s5|household` are CLIENT-COMPARISON runs
and their engines are deleted, so the harness refuses them with **exit 4** and
prints the replay command instead — a retired slice must never read as a pass.
The comparison work moved to the backend:

```bash
cd backend && uv run pytest tests/test_m160_replay_*.py -q   # s2a 29 + matrix 6 + s3 16 + s4 14 + household 12
cd backend && uv run pytest tests/test_search_goldens.py -q   # s5 (M137 already did this)
```

Exit codes: `0` pass · `1` a case differed · `2` **engine not wired (this is
not a pass)** · `3` self-test failure · `4` usage **or a retired slice** ·
`5` fixture invalid.

### Filling in an engine (retired with the client-comparison half by M160)

The only adapter still on the tree is `scripts/parity/engines/projection.ts`,
whose "engine" IS the server over HTTP. The other six were deleted with their
mirrors (M158 Decision 2 / M160-R1 ruling 3), and re-adding one would be
re-introducing the mirror this wave retired. Their gate job is now
`backend/tests/test_m160_replay_*.py`: seed the fixture's captured inputs,
replay the captured request at the mounted route, byte-diff the answer.

`--engine <path>` still runs a different adapter (used for plumbing checks);
`--fixture <path>` diffs a copy — the tamper test below depends on both.

### Rules that make these goldens worth something

- **Re-capture, don't hand-edit.** `capture.ts` asserts every expected status, so
  a broken response can never be recorded as a golden. Hand-editing a golden
  silently converts a bug into an expectation.
- **Time-dependent cases.** `budgets.progress` and `subscriptions.detect` derive
  their window from `today_et()` (America/Toronto). Inject
  `meta.captured_at_et`, not the machine's today — otherwise the numbers move
  and the run "fails" for reasons that have nothing to do with the port.
- **Money is a 2dp string.** `transactions.amount_native` is `Numeric(18,2)`;
  the API echoes `10.00`, `0.00`, `-10.01`. Reproduce the formatting exactly —
  a one-cent or one-zero drift is a real diff, not noise.
- **Arrays are order-sensitive** (server sort order is part of the contract);
  object key order is not.
- **Never run two captures at once** against the same fixture file.

### Deviations and discrepancies (reported here, not silently reconciled)

1. **S3's `POST /rules/preview` goldens come from a throwaway tenant**, not the
   demo: the demo guard method-blocks every non-GET route
   (`403 demo_account_restricted`, `app/api/deps.py::DEMO_MUTATION_EXEMPT_PATHS`
   only exempts `POST /acb/rebuild`) although preview is dry-run and never
   writes (docs/31). `capture.ts` registers `parity-capture-…@example.com`,
   seeds 21 deterministic transactions, captures 14 preview goldens and deletes
   the account in a `finally` (verified 204). `datasets.scratch` is that
   tenant's rows; `datasets.demo` is the demo's.
2. **docs/08 §`GET /subscriptions` pinned `occurrence_count`**; the live
   response returns `occurrences` **and** an extra `last_date`. docs/08 was the
   stale side. **Reconciled by M48-S3** (which rewrote §Subscriptions around
   the removed route); found here, not silently patched (AGENTS §3).
3. **docs/08 §`POST /rules/preview` response omitted `assigned_skipped`**, which
   docs/31 pins and the server returns. **Reconciled by M48-S3** — docs/08 now
   points at docs/31 for the removed route's contract.
4. **docs/08 §preview stated `limit` default 25** but not the schema cap
   (`le=100` → 422 above 100). **Reconciled by M48-S3**: docs/31 §Preview pins
   the 1–100 range and the client reproduces the 422 (golden
   `rules.preview.limit-cap`, replayed at the route by
   `backend/tests/test_m160_replay_s3.py`).
5. **S4 coverage on the demo tenant is thin**: 1/13 tax years carries a disposal
   row, 0/13 carry superficial-loss denials. The goldens are valid but they do
   not exercise denial/ROC math. If S4 needs that coverage, extend `capture.ts`
   with a synthetic tenant (same pattern as the S3 previews) rather than
   relaxing the comparison.
6. `reporting_currency` is `get_settings().reporting_currency` and **no endpoint
   exposes it**; both fixtures pin `CAD`, mirroring the existing
   `lib/dec.ts` `REPORTING = "CAD"` precedent (carved out of the retired
   `reportEngine.ts` by M160-R1).
7. **S4's `GET /acb` is a case AND an input.** `datasets.demo.acbPools` is the
   very response that also backs the `acb.pools` golden (one fetch in
   `capture.ts`, no second call). The S4 adapter computes the pool set,
   `account_id` / `security_id` / `symbol` / `quantity` / `total_acb_cad`, the
   sort order and the `total` / `limit` / `offset` envelope from the trades,
   and reads **only** `id` + `updated_at` from that input — Postgres-assigned
   columns that any demo-exempt `POST /acb/rebuild` re-issues, so gating on
   them would assert on non-determinism rather than on the math (PM scope
   decision A, 2026-09-27). The join is the table's own UNIQUE
   `(account_id, security_id)`; a computed pool the frozen page does not carry
   fails the run instead of silently dropping a row. `GET /acb` itself stays
   mounted (Taxes.tsx renders both columns; the MCP `acb_list` tool has no
   other source).

### Verification this harness has actually been exercised

> **M160 note.** The bullets below are the historical record of the CLIENT
> comparison runs, whose engines this wave deleted. They are kept because the
> tamper probes prove the diff machinery still works (`--self-test`, and the
> `--verify` structure check that runs for every fixture today) and because the
> same fixtures are now replayed at the route with the same case counts —
> `backend/tests/test_m160_replay_{s2a_matrix,s3,s4,household}.py`.

- `--self-test`: 19/19 checks pass (key-order-insensitive equality, value/type/
  missing/extra/length/array-order diffs, and the runner returning 0/1 correctly,
  including a throwing engine).
- Plumbing check with a deliberate echo engine (returns the golden, so it proves
  wiring only — **not** parity): s3 `16/16 exit 0`, s4 `14/14 exit 0`; with three
  values tampered in a copy of each fixture the same engine exits `1` and prints
  the exact paths (`$[0].spent`, `$.items[0].amount_native`,
  `$.totals.gain_loss_cad`).
- `--slice s3` exits `0` since M48-S3 wired `engines/s3.ts` to the shipped
  `src/lib/s3Engine.ts`: **16/16 cases, 0 diffs**, and the same fixture drives
  `tests/s3ClientCompute.test.ts` (33 tests) so CI re-runs parity on every push.
- `--slice s4` exits **0 at 14/14** since M48-S4 wired `engines/s4.ts` to
  `src/lib/reportEngine.ts` (with `--self-test` 19/19).
- A slice whose engine is not wired yet exits `2` (ENGINE NOT WIRED) — that is
  **not** a pass.
- **S2a + matrix, 2026-10-11 (M155)**: `--slice s2a` exits `0` at **29/29
  cases** (14 demo + 15 synthetic) and `--slice matrix` exits `0` at **6/6**
  (2 demo + 4 synthetic), both against the deployed stack; the same two
  fixtures drive `tests/s2aParity.test.ts` (**39 tests**: the 35 cases plus 4
  hand-computed anchors over the frozen rows, so a capture that froze the same
  wrong number on both sides still fails). Tamper probe: one value changed in
  a copy of each fixture makes the run exit `1` naming the exact path
  (`$.items[0].total_cad`, `$.items[0].cad_total`).
- **S4 engine probes, 2026-09-27** (each run against a throwaway copy via
  `--fixture`; the committed fixture is untouched). Tampering a *computed*
  field in the `acb.pools` golden makes the run fail with the exact path
  (`quantity` → `$.items[0].quantity`, `total` → `$.total`,
  `symbol` → `$.items[0].symbol`); tampering that same field inside
  `datasets.demo.acbPools` changes **nothing** — those values are derived from
  the trades, not copied from the fetch. Tampering `id` / `updated_at` inside
  the input does fail (`$.items[0].id`, `$.items[0].updated_at`), because those
  two columns are the input's whole job; deleting a row from the input also
  fails, since the pool-set join then has no metadata for a pool the engine
  computed.
