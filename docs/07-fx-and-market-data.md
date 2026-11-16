# 07 — FX & Market Data

## Bank of Canada Valet client

`backend/app/services/fx.py` wraps the BoC Valet API:

- Endpoint: `https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json` (BoC Valet's data endpoint; the legacy `/valet/rates/…` path 404s — live-probed 2026-09-24)
- Used by the tax engine for daily USD/CAD rates on trade dates (docs/06) and portfolio CAD conversion (docs/05).
- Range requests pass `?start_date=&end_date=` on the same series URL — that IS the bulk-backfill mechanism below; per-date lookups hit the cache first.
- HTTP discipline: 10 s timeout, 3 retries with exponential backoff (0.5 s base); exhausted -> raise, caller falls back to manual rates (a missing rate never becomes a 5xx to the user).
- **Multi-currency board:** the client is not USD/CAD-only — `fetch_valet_series` / `get_rates_between` accept arbitrary `base`/`quote` pairs. `GET /fx/board` exposes a curated board over `CURATED_FX_BASES` (17 bases: USD, EUR, GBP, JPY, CHF, CNY, AUD, NZD, HKD, SGD, INR, MXN, BRL, ZAR, KRW, NOK, SEK), each queried as `FX{base}CAD`, backing the Currency Tracker (docs/15). Membership is a rule, not a wish list — see §Curated board membership (M151), which is why DKK is not in it.

### Cache strategy

- Fetch **on demand per missing date** — a lookup for a date with no cached row triggers one fetch. Since M150 the book is *also* refreshed on a timer (§Scheduled refresh), so the read path is a backstop rather than the only writer.
- Cached forever: rows in `fx_rates` never expire (`source=boc_valet`).
- **Bulk backfill on first use**: when a range query misses many dates, backfill the whole observed span in one Valet series call rather than per-date fetches.
- **Publication calendar & carry-forward (M115):** BoC publishes business days only, so weekends and holidays never get a row. Every exact-date consumer therefore resolves in two steps — the target-date row, else the newest published row on or before it within **7 days** (`CARRY_FORWARD_MAX_DAYS` — canonical in `app/services/fx.py`, aliased by `app/tax/acb.py`); a gap wider than that, with the backfill unable to close it, keeps the `422 fx_rate_missing` (docs/06). The ACB sweep (`tax/acb.py::FxRateBook`) is the one place that backfills *for this reason*: when the cache cannot resolve a date the replay will actually ask for (cash-only rows and registered sells do not count), it issues ONE `get_rates_between` call per currency covering `[min(needed) − 7d, max(needed)]` **before** the replay starts, fail-soft per symbol — a failure logs and the other currencies still get their attempt — with `commit=False`, because `rebuild_user_pools` runs inside the caller's transaction and must never commit (docs/02). A fully cached sweep makes zero network calls, which is why a holiday-dated sweep re-reads the cache instead of re-hitting Valet on every run. `fx_rate_cad` — the single-date read behind positions / registered book cost — stays cache-only: it carry-forwards but never fetches, so no read path opens a network call of its own. Whenever a non-publication day resolves this way the sweep records it (`fx carry-forward: USD/CAD for NVDA on 2025-12-26 uses the 2025-12-24 rate …`), so an operator can audit a holiday-priced trade straight from `docker logs` — which needed `app.main._configure_app_logging`, because uvicorn's LOGGING_CONFIG configures only the `uvicorn*` loggers and an unhandled `app.*` tree fell through to `lastResort` (WARNING-only) with its INFO records lost. Under privacy mode the sweep skips the seam outright (no packet leaves the machine) and reports the missing rate with the privacy wording.
- Offline fallback = manually entered rate (`source=manual`); manual rows win over nothing — they fill gaps only, never overwrite BoC data.
- **Source split — the complete set of `fx_rates` write rules:**
  - `source=boc_valet`: written only by `services/fx.py::_store_fetched` (BoC Valet). Authoritative and permanent; nothing ever overwrites a fetched row — not `POST /fx` (409), not a later fetch.
  - `source=manual`: written only by `POST /fx` (and the MCP `fx_create` tool) into an **empty** `(base, quote, date)`. Permanent user data: a later BoC fetch never replaces it — `_store_fetched` inserts missing dates only (this is the AGENTS.md "manual fills gaps only, never overwrites BoC" invariant read both ways: neither source ever overwrites the other; only empty dates are written).
  - There is **no demo/seeded source and no seeding at all**: neither the boot lifespan (`main.py`, demo seeding) nor `POST /auth/demo` writes `fx_rates` (`services/demo_seed.py::ensure_demo_account` touches no fx rows). Gaps therefore stay empty until a BoC fetch or a manual entry fills them — and since M150 the scheduled refresh (§Scheduled refresh) fills the curated pairs on its next tick, so a booted demo's gaps close within `fx_refresh_interval_seconds` **without the seed having written anything** (the seed is still not the writer; every "the seed touches no fx rows" pin still holds). A demo whose USD trades land before that first tick leaves its fx gaps empty, so its ACB pool rebuild defers on the documented `422 fx_rate_missing` (docs/06) and `POST /acb/rebuild` populates pools once rates exist. Seeding passes `allow_fetch=False` to `rebuild_user_pools`, so the M115 sweep backfill is structurally shut on that path (docs/03: ingestion never opens a connection) — without it a boot would pull live rates into the shared cache and break every "gaps are empty" pin.
- **Egress inventory:** this client and the yfinance fetches below are the two *market-data* seams wrapped by the privacy ContextVar lock; the complete network-egress list (also `app/llm/base.py` and `mcp_server` → `FIN_TRACKER_URL`) lives in docs/11 "What leaves the machine" and AGENTS.md §2 — new outbound calls go through those seams only (audit H4).
- **`POST /fx` is rate limited to 10/min (`FX_WRITE_LIMIT`) and its currency codes are length-bounded.** `fx_rates` is the only table with **no `user_id`** (docs/02) — it is the shared cross-tenant cache every tenant's valuations read — and `POST /fx` (plus the MCP `fx_create` tool) is the only write path any tenant has into it. The create-once rule above bounds rows *per `(pair, date)`* but says nothing about how many distinct pairs a caller may invent, and registration is open, so without a ceiling a fresh account could fill the shared cache with unbounded keys. `FxRateCreate.base`/`quote` are now `Annotated[str, StringConstraints(to_upper=True, min_length=3, max_length=3)]`, matching `AccountCreate.currency` exactly so the two entry points cannot drift. A malformed code is `422`. Tests: `backend/tests/test_credential_rate_limits.py`.

## Curated board membership (M151) — why DKK is not on the board

**The rule:** `CURATED_FX_BASES` holds a base only when Bank of Canada
publishes a **live `FX{base}CAD` daily series** for it. It is not a list of
"major currencies" and it is never derived from tenant rows (docs/11) — it
is a list of series that answer.

**The defect it fixes.** M150's first live tick (deploy of `f738986`):

```
fx refresh: DKK/CAD failed — 404 Client Error: Not Found for url
  https://www.bankofcanada.ca/valet/observations/FXDKKCAD/json?start_date=…&end_date=…
fx refresh: 113 row(s) cached for USD/CAD, …, SEK/CAD (0 pair(s) already current; failed: DKK)
```

17 of the 18 bases resolved, DKK could not. `pairs_behind` reads "no cached
row" as "behind", so DKK was behind **forever**: every tick spent a call, the
whole 3-retry backoff and a WARNING on a series that does not exist — for a
board row `GET /fx/board` could never have rendered anyway.

**Why the alternative ("find the series BoC actually publishes for DKK")
does not exist.** Live-probed 2026-10-11:

| probe | answer |
| --- | --- |
| `…/valet/observations/FXDKKCAD/json` | **404** `Series FXDKKCAD not found.` |
| `…/valet/observations/FXDKK100CAD/json` | **404** |
| `…/valet/observations/DKKCAD/json` | **404** |
| `…/valet/observations/FXEURCAD/json` | 200 — 9 observations in the trailing 14 days (control) |
| `…/valet/lists/series/json` (16,170 series) | 81 `FX*CAD` members and **no DKK**; the only DKK series BoC ever carried are `IEXE0301` (`DKK_NOON`, last 2017-04-28), `IEXE0301.CL` (`DKK_CLOSE`, 2017-04-28), `IEXM0301` (monthly, 2017-04-01) and `IEXA0301` (annual, 2016-01-01) |

BoC stopped publishing DKK a decade ago and there is no label to repoint the
constant at. Deriving one instead (a EUR cross through Denmark's peg, say)
would put **our** number under `source=boc_valet`, which the source split in
this doc forbids: `boc_valet` means *BoC published this*.

**The change is only the removal.** `"DKK"` leaves `CURATED_FX_BASES`, and
because `pairs_behind` iterates that constant, "permanently unavailable" is
a state nothing has to carry — no guard was added to `pairs_behind`,
`run_once` or `_store_fetched` (which only ever writes empty dates). The
board renders 17 rows and a tick makes at most 17 calls.

**What a DKK holder keeps.** The constant is read only by `GET /fx/board`
and the refresh worker; every pair-explicit path ignores it:

- `GET /fx?base=DKK&quote=CAD` still answers from the cache or a manual row,
  and with neither returns the documented `404 fx_unavailable` envelope —
  never a 5xx;
- `POST /fx` still gap-fills DKK, and that row then converts normally;
- `GET /fx/status` reads the currencies **in use**
  (`services/fx_status.currencies_in_use`), not the board, so a tenant whose
  only foreign money is DKK still gets its one pair — `as_of: null`,
  `age_days: null`, `stale: true` — and the chip says `FX rates missing`.
  That state never heals now, which is exactly why it must not be silent.

**Re-adding a base** = live-probe `FX{base}CAD` first, then change
`CURATED_FX_BASES`, the frontend's `CURRENCY_META`, and every count in
docs/07 / docs/08 / docs/15 together. `tests/test_fx_board.py` pins the set
as a literal tuple, so an accidental addition cannot land quietly.

## FX endpoints

All under `/api/v1/fx` (canonical index: docs/08):

- `GET /fx?base=&quote=&date=` — single cached/manual rate (`FxRateRead`).
- `GET /fx?base=&quote=&start_date=&end_date=` — **range form** returns `FxRangeRead` (list of daily rates); `start_date`/`end_date` must be provided together, else `422 invalid_date_range`.
- `POST /fx` — manual gap-fill entry (`source=manual`). Request body = `FxRateCreate` `{base: str, quote: str, date: YYYY-MM-DD, rate: Decimal-as-string}` (3-letter ISO codes, normalized with strip/upper); success returns `FxRateRead` with `201`. Guards, in order:
  - `date` **after today** (server UTC) → `422 fx_future_date` — a future BoC publication date can never be pre-poisoned through this endpoint.
  - an existing `(base, quote, date)` row — BoC **or** manual — → `409 fx_date_not_gap` ("manual entries fill gaps only"): fetched rows are never overwritable via `POST /fx`, and a manual gap-fill is create-once (first writer wins; there is no fx edit/delete endpoint).
  - Only a genuinely empty past/today date creates a row (`201`, `source=manual`) — manual rows fill gaps only.
- `GET /fx/board?days=90` — multi-currency board over `CURATED_FX_BASES` (17 bases → CAD), `FxBoardRead`. Cache-only: it never fetches (`allow_fetch=False`), so the board's freshness is the rate book's freshness — which is exactly what `GET /fx/status` reports (below).

## Scheduled refresh (M150)

The cache used to fill itself on a **true miss** only — `latest_rate` fetches
when *no* row exists on or before the target date, and `fx_rate_cad` never
fetches at all. The carry-forward window then did the rest of the work
silently: a book last fetched on Tuesday prices Wednesday…today's cash legs off
Tuesday's rate, because a row *within* `CARRY_FORWARD_MAX_DAYS` is not a miss.
Nothing errors, nothing logs — the gap re-prices silently, which is the defect
this section closes.

- **Worker** `app/services/fx_refresh.py` — one in-process loop started from
  `lifespan` (`app.main`), the same shape as the M59 delivery worker.
  Settings: `fx_refresh_enabled` (default **true**) and
  `fx_refresh_interval_seconds` (default **21600** = 6 h). Tests force it off
  (`FX_REFRESH_ENABLED=false` in `tests/conftest.py`), so a fixture never races
  a background fetch.
- **What it refreshes**: `CURATED_FX_BASES` → CAD — a STATIC constant, never
  derived from tenant rows, so a tick's egress carries no tenant data (docs/11
  §Egress). A currency outside that list keeps today's on-demand behaviour.
  Membership is the §Curated board membership rule (M151): every member has a
  **live** `FX{base}CAD` series, so the loop cannot name a pair whose only
  possible answer is a 404. That is what stopped DKK — a base BoC stopped
  publishing in 2017 — from costing a call, the full retry backoff and a
  WARNING on every tick for a row the board could never render.
- **When**: `pairs_behind` fetches a pair only when its newest cached row is
  missing or older than `REFRESH_MIN_AGE_DAYS` (1 day) — one Valet series call
  per behind pair over `[today − FETCH_LOOKBACK_DAYS, today]`, through
  `fx.refresh_pair`, which lives in this module so Valet stays behind the one
  egress seam (AGENTS §2). A current book costs **zero** network calls. The
  function needs no "unavailable" branch of its own: it iterates the curated
  constant, and a series that cannot answer is by definition not in it.
- **Store discipline is unchanged**: `_store_fetched` writes EMPTY dates only
  and returns the number of rows added, so a tick can never overwrite a BoC row
  or a user's manual gap-fill, and re-running a cached window adds nothing.
- **Fail-soft**: `ValetUnavailable` is per-pair — it is logged and the other
  pairs still get their attempt; the loop outlives any tick (docs/37's
  contract). One INFO line reports what a tick that *changed* something did.
- **Privacy**: a tick is skipped entirely while ANY user has `privacy_mode`
  on (docs/11). `fx_rates` is shared with no `user_id`, so a per-user flag
  cannot be honoured pair by pair — privacy wins over freshness, and the
  staleness chip below is what makes that visible instead of silent.
- One uvicorn process in the compose stack, so exactly one worker runs; the
  empty-dates-only store makes a second one harmless anyway.

## Rate-book status (M150) — `GET /fx/status`

The reporting half: it answers *how old is the rate book for the money I
actually hold?* — cache-only, so a page load can never become a Valet call and
privacy mode cannot 403 it (docs/12).

- **Pairs in use** (`services/fx_status.currencies_in_use`) — the union of
  `trades.currency` (the M132 row currency), `accounts.currency` (what a
  pre-M132 row with NULL currency reads in) and `securities.listing_currency`,
  each → the reporting currency, minus the reporting currency itself. Staged
  trades are excluded, like every other report input.
- **Per pair** (`FxPairStatus`): `as_of` (the newest cached row on or before
  today — never a carried-forward value for some target date), `age_days`,
  `source`, and `stale`. **Stale = past `CARRY_FORWARD_MAX_DAYS` (7), or no row
  at all** — the same window consumers carry a rate forward through, so the
  chip's threshold and the engine's ceiling are one number (the constant lives
  in `services/fx.py` and `tax/acb.py` aliases it).
- **Top level** (`FxStatusRead`): `reporting_currency`, `as_of`/`age_days` of
  the **worst-covered** pair (the one that decides whether a number on screen
  is off a carried-forward rate), and `stale` when ANY pair is stale.
  `pairs: []` means the tenant holds no foreign currency at all — no FX
  dependency, and the UI chip then renders nothing.
- **Not the board's list.** These pairs come from the tenant's own rows, never
  from `CURATED_FX_BASES`, so a currency the board cannot price is still
  reported. DKK is the worked case (§Curated board membership): no tick will
  ever fill it, so a DKK-only tenant answers exactly one pair — `as_of: null`,
  `age_days: null`, `stale: true`, `source: null` — and the chip says
  `FX rates missing`. A *permanent* gap is the state most in need of being
  said out loud, and it travels the same code path as a transient one; there
  is nothing to special-case.
- The UI half is `components/ui/FxStalenessChip.tsx` (docs/09 §FX staleness),
  mounted in the Currency Tracker header (docs/15).

## yfinance quotes

### Cached dividend history and cache-first suggestions (M184)

`services/market_data.fetch_dividend_history(symbol)` remains the only yfinance
seam for dividend events. `GET /trades/dividend-suggestions?account_id=` reads
raw per-share event rows from `dividend_history` and recomputes suggestions
from the current committed/manual trade ledger on every request. The route body
remains the existing `DividendSuggestion[]`; an additive
`X-Dividend-History-State` response header reports JSON
`{"pending": int, "unavailable": int, "empty": int, "stale": int, "quote_miss": int, "missing": int, "source": "yfinance", "as_of": UTC timestamp|null, "age_days": int|null}`.
`as_of` is the oldest successful refresh in the requested scope; each count is per distinct tenant/security source, not per account holding. `empty` counts successful empty provider responses separately from unavailable data. The UI labels stale source age, pending/refreshing work, unavailable state, and successful empty histories even when the list body is empty.
`stale` counts only histories with a prior successful refresh older than 24 hours;
never-successful histories are pending or unavailable, not stale. `missing`
counts the tenant/security sources that have NEVER answered — `last_success_at`
is null — which is distinct from `unavailable` (an answer we could not read,
where last-good events are retained) and from `stale` (an answer older than 24
hours). `source` names the provider those events came from: the
`dividend_history.source = 'yfinance'` CHECK (docs/02) makes it a schema fact,
so it is one constant, not a per-row read. `age_days` is the whole-day age of
that oldest `as_of` against the server's own clock, `null` when `as_of` is.
`quote_miss` (M186) is the subset of `unavailable` whose failure is a verdict about the SYMBOL — a hard Yahoo quote miss (`error_code="quote_not_found"`) or the M141 `dead_symbol` flag — not the network; the UI reports it separately because automatic retry cannot help it.
Both are additive: a client that reads only the keys it already knew behaves
exactly as before, and the header is identical (all keys present, zeros) on the
no-open-positions early return.

### Dividend-view provenance block (source · timestamp · stale-or-missing)

Every dividend view answers the same three questions — WHICH source produced
these numbers, WHEN it last answered, and whether it is stale or missing — with
ONE shape, built by `backend/app/services/dividend_provenance.py` and rendered
by `frontend/src/components/reports/DividendProvenanceStrip.tsx`:

```json
{"status": "ok|stale|missing", "as_of": "YYYY-MM-DD"|null, "sources": [
  {"key": "…", "kind": "market_data|ledger", "label": "…", "provider": "yfinance|boc_valet/manual|…"|null,
   "as_of": "YYYY-MM-DD"|null, "age_days": int|null,
   "status": "ok|stale|missing", "detail": "one sentence the hover shows"}]}
```

- **Carried by** `GET /reports/dividends` and `GET /reports/dividend-calendar`
  as a top-level `provenance` key (additive; the CSV twins are row exports and
  never carry it — they serialize `items` / `upcoming` only). The suggestions
  route answers the same vocabulary through its header, which the UI folds into
  one `market_data` entry with `provenanceFromHistory` (docs/22).
- **Severity:** `missing > stale > ok`, so the top-level `status` is the WORST
  source verdict, and the top-level `as_of` is the OLDEST source timestamp —
  the same "least fresh evidence" rule the header's `as_of` already used.
- **Only a `market_data` source can be `stale`.** A `ledger` source is a
  recorded fact: it answers `ok`/`missing` with `age_days: null`, never stale.
  `market_data` staleness reuses the existing thresholds, never a new one:
  the dividend-history source at 24 h (§M184) and the FX book past
  `CARRY_FORWARD_MAX_DAYS` (7 days, §Cache strategy).
- **Sources per view.** `income_trades` (ledger, `as_of` = newest recorded
  income payment in scope) on both report routes; `trade_date_fx`
  (market_data, only when the report converts a foreign denomination —
  `provider` = the distinct `fx_rates.source` values for the bases used,
  `as_of`/`age_days` = the newest cached rate for them, `missing` when
  `unresolved_fx > 0`) on `/reports/dividends`; `dividend_history`
  (market_data) for the suggestions header. A CAD-only report therefore lists
  one source and claims no market-data dependency it does not have.
- **The strip never prints money**, so it needs no display mask (docs/46); the
  per-source sentence rides in the container `title`, and the state word
  (`(stale)` / `(missing)`) is always in the badge text itself.

`dividend_history` stores only tenant-scoped raw per-share events, never computed totals. Provider response ordering and duplicate ex-dates are preserved; `provider_order` distinguishes rows without collapsing same-day inputs. Refresh is 24 hours from `last_success_at`; a provider failure only updates
attempt/state/error metadata, retaining last-good rows and success time. A
malformed date or non-finite/non-positive amount makes the whole refresh
unavailable with `invalid_provider_payload`; it never publishes a partial or
false-empty history. A successful empty response is distinct, but it exists
only after `probe_quote` returns `QuoteOutcome.ok`: Yahoo uses an empty dividend
frame for both a genuine no-dividend listing and `Quote not found`. A missing
quote is therefore `unavailable`, retains old provider events and
`last_success_at`, and is booked through `record_quote_outcome` so three hard
misses set the existing `dead_symbol` flag. Only the quote-verified empty path
removes old provider events and sets state `empty`. Refresh jobs are
PostgreSQL-persisted as `pending` and claimed under a row lock as `refreshing`
before provider I/O; this is single-flight across worker processes, not only
within one process. The lifecycle-managed `dividend_refresh` worker polls every
2 seconds, processes at most 8 jobs serially per tick (global concurrency 1),
owns short-lived sessions and re-queues `refreshing` rows older than five
minutes after a crash. Requests coalesce by `(user_id, security_id)`. Failures
are retry-eligible no sooner than 15 minutes, so a provider outage cannot
create a retry storm. M186 makes that bound exponential and finite in rate:
each consecutive failure (`dividend_history_refresh.consecutive_failures`)
doubles the window — 15m → 30m → 1h → 2h → 4h — capped at `MAX_RETRY_AGE`
(6h), so a symbol that stops resolving is retried four times a day instead of
every 15 minutes. Recovery is one success: any successful refresh (including a
quote-verified empty) resets the streak to 0 and the row returns to the flat
15-minute cadence. A dead-symbol row climbs the same streak when re-queued and
re-refused, so the flag's fan-out also backs off. A hard Yahoo quote miss
additionally books `error_code="quote_not_found"` (distinct from
`provider_unavailable`) so the failure class is user-visible. Provider calls
retain the existing finite yfinance
5-second transport deadline. The worker rechecks current security ownership and
tenant privacy immediately before fetch and again before storing; privacy mode
or a dead-symbol verdict prevents egress. No request session is held across
provider I/O.

Portfolio valuation is a cache-only read: `services/valuation.compute_positions` batches
cached latest closes, at most 31 closes per held security for spark/previous
close, and latest reporting-currency FX rows by distinct pair. Registered
accounts' trade-date FX dependencies are preloaded in one bounded query using
the same exact/on-or-before-seven-day resolution as the prior per-trade cache
lookup; tax report exact-date FX remains separate. It does not call providers. Freshness remains explicit refresh via `POST /portfolio/refresh`
and `POST /securities/{id}/refresh`, plus the existing automatic market-hours
background refresh. Date/source/age and stale iff age > 7 days are unchanged;
unknown values remain null, and manual prices still win.

### Body contract — a junk body is a MISS, never a quote

Every Yahoo parser path in `services/market_data.py` obeys one rule, pinned by
`backend/tests/test_market_data_body_contract.py` (a corpus of bodies Yahoo
has REALLY answered with — named in the file — plus a seeded fuzzer that
mutates a real body with type-junk):

> a junk / empty / unreadable body is always a **MISS**, never a quote: no
> payload, no chooser row, no stored number, no `ok` verdict, and no
> exception escaping to the caller.

"Miss" means the module's documented degradation (`{}` / `None` / `none` /
`not_found`), because the failure modes above it are all SILENT: M142's junk
`.info` (`5fd7433`), M143's venue stub and M162's ID triple were each caught
LIVE against the deployed stack, not by a test. Two of the readings are
verdicts rather than payloads — an `ok` carrying no closes would RESET M141's
dead-symbol counter on evidence of nothing (§Dead symbols), and a raise where
a miss belongs turns the documented 404 `lookup_unavailable` into a 500.

| Seam | Body | The contract |
| --- | --- | --- |
| `_is_real_quote` | `Ticker.info` | A body that is not a dict is a MISS. The ADOPTED name (`_adopted_name`: `shortName`, else `longName`) must be a non-blank **STRING** and not an artifact. The gate and both payload builders read the name through that ONE helper, so a body can only be accepted on a name it would also be adopted under. |
| `_search_equity_quotes` | the `v1/finance/search` answer | Non-dict JSON, no `quotes` list, or a non-dict row yields no rows. A row qualifies only with a STRING `symbol` and a STRING `quoteType` in `EQUITY`/`ETF`/`MUTUALFUND`; a malformed `shortname` never reaches a payload (the builders fall back to the row's symbol — the ticker is what makes it a listing). |
| `_http_search_json` | the same answer, raw | Unparsable, non-dict, or non-200 after the documented 429 retries → `None`. |
| `_fetch_one_history` | `Ticker.history` | An unreadable body (`None`, an error stub, a dict), a row we cannot date, or no FINITE `Close` in the window → `({}, not_found)`, never `({}, ok)`. Unusable cells are SKIPPED (one bad day costs one day), not fatal. |
| `probe_quote` | `Ticker.history(period="1d")` | `ok` only for a datable, finite close — the same reading as the fetch, so the add form's warning and the fan-out's flag can never disagree (§Symbol check). |
| `_dec` / `fetch_fundamentals` | `Ticker.info` numbers | `NaN` / `Infinity` are NOT numbers — they parse as valid `Decimal`s and Postgres stores them — so they read as "no value"; a non-dict body → `None`. |
| `fetch_fund_holdings` | `funds_data.top_holdings` | A row needs a non-blank name and a finite POSITIVE weight (the rule `_parse_issuer_csv` already applied), else the row is dropped. |

### Dead symbols (M141) — the fan-out's stop signal

A fetch failure is **classified, not swallowed**. `QuoteOutcome`
(`app/models/securities.py`) is the verdict, and only one member of it is a
verdict *about the symbol*:

| Outcome | Meaning | Counts toward `dead_symbol`? |
| --- | --- | --- |
| `ok` | at least one close came back | clears the flag |
| `not_found` | Yahoo said the symbol does not exist | **yes** |
| `unavailable` | rate-limit, timeout, DNS, parse failure | no |
| `unknown` | no fetch ran here (monkeypatched seam, privacy guard, cached read) | no |

`not_found` is detected from the yfinance exception family
`YFTickerMissingError` (subclasses `YFTzMissingError`, `YFPricesMissingError`).
Pinned by LIVE PROBE against yfinance 1.6.0 (2026-10-10): `APOTEX` raises
`YFTzMissingError("$APOTEX: possibly delisted; no timezone found")` with the
HTTP layer logging `404 {"error":{"code":"Not Found","description":"Quote not
found for symbol: APOTEX"}}`, and an empty frame is the same verdict when the
library does not raise. `YFRateLimitError` and every other exception are
`unavailable`: one flaky request out of two must never march a live symbol
toward the flag, so when the `.TO` retry candidate and the bare symbol
disagree, a single `unavailable` dominates the `not_found`s.

**The counter** (`services/market_data.py::record_quote_outcome`) advances
only on `not_found`; three consecutive misses
(`DEAD_SYMBOL_MISSES = 3`) set `securities.dead_symbol` and store the verdict
in `dead_symbol_reason`. **Any successful fetch resets the counter and clears
the flag** — a relisted symbol heals with no manual action and no repair
script.

**A verdict belongs to the fetch that produced it.** The verdict rides the
request context (`_last_quote_outcome` / `_last_quote_reason`) because the
fetch layer has no session — so it outlives the call that published it, and
the test that published it. Every seam that reads it back after its own fetch
(`backfill_range`, `ensure_recent_price`, `deep_backfill`, `split_repair`, the
benchmark sweep) therefore clears it first, alongside its
`_reset_fetch_provenance()`: a fetch that never reaches yfinance (a
substituted seam) then reads `unknown` and books nothing, instead of a
previous fetch's `not_found` being booked against THIS row — which would also
`commit()` whatever the caller had pending, i.e. the dry-run rollback boundary
`scripts/repair_symbol.py` relies on. `dividend_refresh` already reset it this
way. Pinned by
`tests/test_m141_dead_symbol.py::test_a_seam_books_only_the_verdict_its_own_fetch_produced`.

**The gate** (`_skip_dead_symbol`) makes the automatic paths skip the row
outright — no network call, not a failed one: `backfill_range`,
`ensure_recent_price` and `ensure_fundamentals` all serve from cache, and both
fan-outs (`GET /portfolio/positions`' market-hours tasks and
`POST /portfolio/refresh`) filter dead rows out before queueing.
`POST /portfolio/refresh` counts what it skipped as `skipped_dead` so the UI
can state what it did not do. The ONE exception is
`POST /securities/{id}/refresh` — the per-security **"Try refresh"**, which
passes `probe_dead=True`. It is the escape hatch: the only path that can
disprove the flag, and it does so by simply succeeding.

`probe_dead` is deliberately a SEPARATE parameter from `ensure_recent_price`'s
`force`: the market-hours fan-out passes `force=True` purely as a freshness
knob and must still honour the gate. One boolean doing both jobs would turn
that fan-out straight back into the 404 machine the flag exists to stop.

### Symbol check (M141)

`GET /securities/quote-check?symbol=` answers
`{symbol, quote_found: bool|null, reason}` — one `history(period="1d")` call,
the same seam the fan-out uses, so the add-form warning and the later
`dead_symbol` flag can never disagree. `quote_found: true` requires a datable,
FINITE close: a frame that is merely non-empty (all-NaN closes, an index we
cannot date) answers the same miss the fetch would make (§Body contract), so
the two verdicts cannot drift apart. `quote_found: null` means **we could not
ask** (network failure, rate-limit, privacy mode): an answer we do not have is
never reported as "no quote". It is deliberately NOT a hard block — a
never-fetched symbol is a legitimate thing to watch (a future listing, a cash
equivalent) — so the router always answers 200 and the FORM decides how loudly
to warn (docs/09 §No public quote). Rate-limited 30/minute like `/lookup`;
privacy mode raises the standard 403 from the seam guard.

- Quote fetching with retry/backoff (exponential, capped; treat 429s as retryable).
- Used for securities autofill (docs/05), latest prices, and per-security fundamentals (M16): `fetch_fundamentals` maps `Ticker.info` into the `fundamentals` cache (docs/02) behind the same privacy guard and degrade-to-None discipline; `ensure_fundamentals` serves a 7-day freshness window (`source="manual"` rows are never refreshed or overwritten) and read paths wrap it in silent degradation — a blocked or failed fetch leaves the cached row or null, never an error.
- Fund holdings composition (M17): `fetch_fund_holdings` maps `Ticker.funds_data` top holdings into `etf_holdings` (docs/02) behind the same privacy guard and degrade-to-None discipline; `funds_data` shape varies across yfinance versions, so every attribute access is defensive and an empty/non-fund payload answers 503 `market_data_unavailable` at the endpoint instead of erroring. Import replaces only `source="yfinance"` rows — manual rows are never touched.
- Historical daily-close range fetches cache into `prices` **forever** with `source='yfinance'`, mirroring the `fx_rates` cache policy above; feeds the net-worth trend (docs/05) and position valuation. Backfill inserts ONLY dates lacking a row; existing `manual` rows are never touched (manual wins, docs/08). Closes are stored in the **ledger's as-traded frame**, never Yahoo's split-adjusted one — see §Price frame (M149) below, which is the contract that keeps `quantity × price` meaningful across a split.
- Unofficial API that may break at any time. Contingency, noted here rather than as a blocker: if yfinance dies, users keep entering prices manually.
- Call discipline: 5 s timeout; failures degrade to "no data" (UI offers manual entry) rather than surfacing errors; 429 handled by the shared retry/backoff.
- **The 5 s is ours, not yfinance's (M149).** yfinance exposes no per-call timeout on `Ticker.info` / `.history` / `.funds_data` / `.dividends`; it applies its own default to every request it makes — `timeout=30` in yfinance 1.6.0, verified by probe at the transport — and that number is undocumented and free to move with the version (AGENTS §6). Before M149 this module built `Ticker(sym)` with no session, so the "5 s" above was a promise nothing kept (`HISTORY_TIMEOUT_SECONDS = 5` was a constant no code read). `market_data._yf_session()` closes it: it takes yfinance's OWN default session (curl_cffi impersonating Chrome where that backend is installed, a plain requests session where it is not), re-types it into a subclass that forces `timeout=YF_TIMEOUT_SECONDS` (5) on `Session.request`. It FORCES rather than `setdefault`s, because yfinance always passes its own value, so a default would be a no-op and a stalled Yahoo would still be bounded at 30 s. `Session.request` is the single seam that matters: every yfinance call — the cookie/crumb handshake included — ends there. It is INSTALLED on yfinance's `YfData` singleton — the one session the library keeps for the whole process and re-uses for every ticker — through `market_data._ticker(sym)`, the module's only `Ticker` constructor. The constructor's signature is deliberately left alone: the deadline belongs to the shared session, not to each call site. Worst case is now 3 attempts × 5 s per candidate instead of 3 × 30 s. Pinned by `tests/test_m149_yf_timeout.py`: the deadline reaches the transport, it overrides yfinance's 30 s, it is the session the singleton actually holds, and every fetch seam still routes through `_ticker`. The field mapping is unchanged — live-probed 2026-10-11 against yfinance 1.6.0: `resolve_symbol`, `probe_quote` and `fetch_history` return byte-identical payloads with and without the deadline session, and the APOTEX dead-symbol verdict is unchanged.

### Price frame (M149) — what a split does to the cache

`prices.price` is stored **as-traded**: the price that day actually traded at,
in the SAME frame as the ledger's quantities. Yahoo cannot serve that number
directly, because its `Close` is ALWAYS split-adjusted — `auto_adjust=False`
removes only the *dividend* adjustment. LIVE PROBE against the deployed
yfinance 1.6.0, 2026-10-11, against prod security 86 `VGT` (8:1 split on
2026-04-21):

| | 2025-11-18 | 2026-04-21 |
| --- | --- | --- |
| `Close` (`auto_adjust=False`) | 91.5737533569336 | 101.0199966430664 |
| `Adj Close` | 91.1711196899414 | 100.7825546264648 |
| `Stock Splits` | 0.0 | **8.0** |
| the trade that day (broker export) | **732.59** | — |

So `_fetch_one_history` fetches `period="max"` — ONE request, measured no
slower than the windowed one it replaced (VGT's 5710 rows in 0.15 s against
0.26 s for the 252-row window) — slices it to the caller's window, reads every
`Stock Splits` event the FULL frame carries, and un-adjusts:

```
as_traded(d) = yahoo(d) × Π{ratio : split_date > d}
```

The split's own effective date is already quoted post-split (measured: VGT
2026-04-20 reads 101.1413 — 1/8 of the 809.13 actually traded — while
2026-04-21 reads 101.0200, its real close), so the comparison is STRICTLY
before it. A reverse split needs no special case: Yahoo's `0.1` for a 1:10
DIVIDES the pre-split closes, and multiplying them back by `0.1` restores the
traded price. A zero/NaN `Stock Splits` cell is not an event, and a frame with
no events leaves the closes byte-identical.

**No usable close is a miss, not a success.** A `Close` cell reads only when
it is a FINITE number — NaN is yfinance's "no quote that day", and a junk body
may hold `"n/a"` or an object — and a row whose index we cannot date is
skipped with it. A window where every in-window row fails that read produces
`({}, not_found)`, never `({}, ok)`: `backfill_range` books the fetch's verdict
against the row, so an `ok` carrying no closes would RESET the M141
dead-symbol counter on evidence of nothing (§Dead symbols). An unreadable
body — `None`, an error stub, a dict — is the same verdict (§Body contract).

**Why the max fetch, not a window.** A window that ends before the split
carries no event of its own, so a window-only reader cannot tell "all
post-split, correct as served" from "all pre-split, divide by eight" — and
that is exactly the question a deep backfill of an old window asks (docs/07
§Backfill depth, whose start bound is the row's own first trade). The
`period="max"` frame answers both, for the same single request; the caller's
window is then applied by slicing in-process, so `fetch_history`'s contract is
unchanged and `fetch_latest_close` still gets one 21-day answer rather than a
dict of 11k rows.

**Why the cache is normalized instead of the quantities.** docs/06 pins a
split to move the ledger's SHARE COUNT (the manual
`corporate_action_adjustment`), while historical trades keep the quantities
the broker exported. The cache is therefore the only side that can be brought
into the ledger's frame without rewriting history — and it is the only choice
that stays honest for a user who has NOT entered the corporate action yet:
51 shares × the as-traded 732.59 reads $37.4k before the split and 51 ×
101.02 reads $5.2k after it — a visible cliff that points at the missing
`corporate_action_adjustment` instead of hiding it behind a flat, wrong line.

**What already-wrong cached rows get.** The sweep inserts only missing dates,
so a window cached before this seam existed keeps its wrong rows forever —
that is the whole reason the repair below exists. Nothing else in the read
path changes: `PriceCache`, `account_value`, `total_return` and
`/portfolio/positions` all keep reading one already-correct number.

- **Call discipline is unchanged**: one request per candidate, the same retry
  and the same M141 classification. The frame is fetched ONCE per candidate
  (not once per window and once per split), so the fan-out's Yahoo traffic does
  not grow.
- **`market_data.last_splits()`** publishes the events the fetch saw on the
  request context — the same mechanism as `last_quote_verdict()` — which is
  what keeps the fetch layer session-free (docs/01) while still letting the
  repair report what it applied.

### Split repair (`POST /securities/{id}/split-repair`)

The operator escape hatch for a window that was cached in the wrong frame.
Owner-scoped like every other `/securities` route; the demo tenant's write
guard rejects it like any other mutating endpoint.

- `POST /api/v1/securities/{id}/split-repair?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
  → `200 SplitRepairRead`; `404` for another tenant's row; `422
  invalid_date_range` when `start_date` is after `end_date`; `403
  privacy_mode_blocks_external_api` from the fetch seam, in which case nothing
  is written.
- `SplitRepairRead` = `{security_id, symbol, start, end, fetched, splits[],
  updated, inserted, unchanged, skipped_manual, refused}`, with `splits[]` one
  `{date, ratio}` per split Yahoo reported (`ratio` is a Decimal string:
  `"8.0"` for an 8:1, `"0.1"` for a 1:10 reverse split) and `refused` null
  unless the fetch answered nothing (`no_closes`).
- **It re-derives the window, it does not scale the rows.** Multiplying the
  stored closes by the ratio is the obvious repair and is NOT idempotent — run
  twice and the window is worth 64×. The repair re-fetches through
  `fetch_history` (which normalizes) and rewrites `source=yfinance` rows in
  place, quantized to the column's `Numeric(20, 8)`, so a second run reports
  `updated=0, unchanged=N` and leaves the numbers alone.
- **Write rules are the shared ones**: a missing date is INSERTED, an existing
  yfinance row is UPDATEd, an existing `manual` row is counted as
  `skipped_manual` and left alone. The M141 verdict of the refetch is booked
  through `record_quote_outcome`, so a successful manual repair clears a dead
  symbol exactly like "Try refresh" does.
- Privacy mode refuses from inside the seam; the transaction is never opened.

```bash
# from outside the stack — the image COPYs scripts/, the container owns the
# app package, the settings and DATABASE_URL
ssh example.internal 'cd /home/developer/fin_tracker && docker exec fin_tracker-api-1 \
    python scripts/repair_split.py --security-id 86 --start 2025-10-09 --end 2026-10-09'
# …same command with --apply to write; it prints the window's rows before and after
```

`scripts/repair_split.py` drives the very service the endpoint calls
(`split_repair.repair_split_window`), with an `apply` switch: the preview
makes the fetch, reports the splits it would un-adjust with and the counts it
would write, then rolls back.

### Re-probe (`backend/scripts/split_probe.py`)

Cache-only and read-only — no yfinance call, no write. For every security with
a cached close it compares the cache against the row's OWN committed trades:

```
ratio(d) = (cached close(d) × fx(listing currency → trade currency, d)) ÷ trade price(d)
```

tested in **both** readings the row can be read in — the trade price
UNCONVERTED and the close fx-converted — because §Series validation already
proved a row's stated currency and the number the broker exported can
disagree (the pre-M132 rows). A row **either** reading puts within 1% of 1 is
explained and does not vote; only a row neither reading explains may. Without
that second reading a stale `listing_currency` reports a ~1.37 "split" that is
really the USD/CAD rate — measured on prod, five such rows.

A **constant** ratio well away from 1 across at least two dates is the
signature: commission and spread move a price by under 1% (prod's explained
rows measured 1.0002–1.0032), a split moves it by the whole ratio — and an
under-valued position reads the RECIPROCAL (0.125, not 8), so both sides of 1
are checked. Rows whose trade price or fx rate cannot be compared at all are
counted `unclear`, never reported as a hit.

```bash
ssh example.internal 'cd /home/developer/fin_tracker && docker exec fin_tracker-api-1 \
    python scripts/split_probe.py'                    # every held security
... python scripts/split_probe.py --user-id 343       # one tenant
... python scripts/split_probe.py --security-id 86    # one row
```

Exit code 2 when a split signature is found, 0 otherwise, so a re-probe after
the repair can be scripted. Measured on prod **before** the repair (2026-10-11,
114 rows probed): security 86 `VGT` reported 0.1250 / 0.1252 / 0.1244 across
its three trade dates — the 8:1 split, to four decimals — and every other row
of user 343 reported 1.0002–1.0032.

## Fund composition providers (M147 / M147-followup)

Three providers feed `etf_holdings` (the chain lives in
`market_data.fetch_fund_composition`; every fetch sits behind the single
privacy guard, so privacy mode blocks the whole chain — docs/11 step 6):

1. **Issuer full-holdings CSV** (`market_data.fetch_issuer_holdings`) —
   iShares/BlackRock publishes one per US product at
   `{product-url}/latest-holdings.csv` (the slug is part of the URL; a
   bare product id 404s — verified live 2026-10-10). The registry
   registry — `provider_fund_registry` family `ishares_us` — maps the STORED
   ticker to that stem, and a row with `status='miss'` is a miss with NO
   network call. **Fund-id LOOKUP: it exists** (M168, verified live
   2026-10-11) — iShares' own product screener
   `GET https://www.ishares.com/us/products/etf-investments` answers a
   server-rendered table of **526 rows**, one per US product, the product id
   in the URL and the ticker as the link text, unique across the live parse —
   so a first-encounter ticker is resolved against IT instead of against a
   code literal (§Provider registry), and coverage grows without a deploy.
   Pinned
   shape (parser `market_data._parse_issuer_csv`): a fund-facts
   preamble, then the `Ticker,Name,Sector,Asset Class,Market Value,Weight
   (%),…,Location,…` header row, then quoted CSV lines; `Weight (%)` is
   already a percent of NAV; `-` means unstated; the header row must be
   present or the body is an error page → None. Timeout 20 s (a full CSV
   is 80–250 KB), 3000-row cap, any failure degrades to None and the
   chain falls through. `Asset Class` maps deterministically
   (Equity→stock, Fixed Income→bond, Cash/Currency→cash,
   Alternative→commodity, else other). Canadian iShares products still
   publish their CSVs behind a JS-only app with no stable URL — the known
   gap; those tickers simply aren't registered yet (Vanguard's Canadian gap
   is closed by the next provider, which reads the issuer's API instead of
   hunting for a CSV).
2. **Vanguard's own full holdings** (`market_data.fetch_vanguard_holdings`,
   M147-followup) — Vanguard publishes no CSV: both of its sites serve their
   holdings from first-party JSON APIs, each LIVE-verified 2026-10-11 from
   this host AND from inside the example.internal `api` container (~0.3 s/call):

   * **US** — `GET https://investor.vanguard.com/irr/funds/profile/{TICKER}-AdditionalFundData`
     → `.holdingDetails` carrying `equityHoldings` / `fixedIncomeHoldings` /
     `shortTermReservesHoldings` / `derivativeHoldings` — a fund publishes only
     the groups it holds (BND has no equity block at all). Pinned shape
     (parser `_parse_vanguard_us_payload`): `securityType` + `securityMainType`,
     `securityLongDescription`, `marketValuePercentage` as a STRING percent
     (`"3.94%"`), `ticker`, `isin`. Registry family `vanguard_us` (50 rows
     seeded from the literals this replaced, each an HTTP 200 with a
     parseable payload on 2026-10-11); a `miss` row is a miss with NO network
     call. A ticker NO row knows is probed ONCE — the endpoint is
     parametrized by ticker, so a fund nobody hand-listed resolves on first
     encounter (LIVE 2026-10-11: `VOT`, `VONE`, `VTWO` and `VTIP` all answer
     200 and were in no literal) — and the verdict, 404 included, is written
     back (§Provider registry).
   * **CA** — `POST https://www.vanguard.ca/gpx/graphql`, the GraphQL operation
     `FundsHoldingsQuery` (pinned verbatim in
     `market_data.VANGUARD_CA_HOLDINGS_QUERY`) →
     `.data.borHoldings[0].holdings.items`, paged at `limit: 1500` until
     `lastItemKey` is null. Headers are `Content-Type: application/json` and
     `X-Consumer-ID: ca0` — live-checked: without the consumer id the gateway
     answers 400 `x-consumer-id must be provided`. No cookies, no CSRF, no
     auth. Registry family `vanguard_ca` maps the Canada ticker →
     `(port_id, venue_suffix)` (42 rows seeded from the literals this
     replaced: portIds from the issuer's own product pages, tickers +
     exchanges from its `FundFinderFunds` listing metadata), and a stored
     `VCN.TO` / `VBG.NE` resolves by stripping the exchange suffix
     (`_vanguard_ca_key`). A first encounter asks the ISSUER for the pair
     instead of a literal: the published portId list in
     `GET https://www.vanguard.ca/en/product.model.json` (the AEM page
     model's `site-config.portIds` — 54 products live 2026-10-11, of which
     the 42 listed ETFs are the ones a user's holdings can carry) plus one
     profile+listings query over those portIds answers ticker, portId and the
     listing's `stockExchangeMarketIdentifierCode` — `XTSE` → `.TO`,
     `NEOE` → `.NE` — and the live catalogue reproduces all 42 seeded pairs
     exactly (§Provider registry). **An unknown portId answers
     HTTP 200 with EMPTY arrays** — emptiness, not the status line, is the
     failure signal (live negative control). Pinned shape (parser
     `_parse_vanguard_ca_items`): `securityLongDescription` (fallback
     `issuerName`), `marketValuePercentage` as a NUMBER, `gicsSectorDescription`
     (null for bonds / cash / fund legs), `bloombergIsoCountry` (ISO3166
     alpha-2), `securityType`, `ticker`.
   * **Country** (`country_for_iso2` in `app/services/company_country_map.py`)
     is derived from the ISIN's first two characters (the US payload has no
     country column) or read from `bloombergIsoCountry`, spelled exactly like
     the names yfinance writes into `fundamentals.country` so one country never
     splits across two pie slices. Deliberate gaps: `XS` / `EU` are ISIN
     ISSUING categories rather than places, and offshore incorporation
     domiciles (`KY`, `BM`, `JE`, `GG`, `IM`, `MU`, `AN`, `LR`, `VG`, `MH` …)
     state where the ISSUER is incorporated, not where the business operates
     (Alibaba's ISIN starts `KY`) — those rows stay country-less and fall
     through the docs/05 chain instead of asserting a geography the payload
     cannot support (measured cost: ~3% of VXUS).
   * A **fund-leg** row (`securityType == "EQ.ETF"`) carries no country either
     — its geography comes from recursing into the leg's own rows (docs/05) —
     and its ticker is stored WITH the registry's venue suffix (`VCN` inside
     VEQT lands as `VCN.TO`) because the look-through engine matches
     composition tickers against the user's stored securities exactly.
   * Rows the column cannot store are dropped in the parser, never at write
     time: the weight is quantized to `Numeric(7,4)` and anything ≤ 0 or > 100
     is skipped (VXUS publishes 6,586 `0.00%` rows among 8,983), and `CT.*`
     currency-hedge legs are skipped entirely — a CAD-hedged fund publishes
     BOTH sides of every forward (VUS `+291.71 / -290.69`), and `weight_pct >
     0` would keep only the positive side and store a 390–400% total. `asset_class`
     maps through `_vanguard_asset_class` (EQ→stock, FI→bond, MM/CT/CRNY→cash,
     DE/MF/other→other). Timeout 30 s (largest payload seen 5.1 MB), 20,000-row
     cap, 8 pages (largest Canadian fund seen: VIU 3,752 rows); any failure
     degrades to None and the chain falls through.
   * **A full list also bounds the M19 deepening.** `ensure_sub_fund_composition`
     receives every written ticker, and a RESOLVED non-fund ticker creates a
     linking security without counting toward the 12 enrichments — with
     2,000-row funds the loop would probe the whole fund (measured on prod:
     1,458 securities created for one tenant, minutes of yfinance fan-out per
     import). `market_data.SUB_FUND_PROBE_CAP` (60) caps the probes: providers
     publish weight-first, so the heaviest rows — every fund-of-fund leg in
     production — are the ones linked.
3. **yfinance `funds_data.top_holdings`** (`market_data.fetch_fund_holdings`,
   M17) — the fallback for every other fund: reliable, but a TOP SLICE
   only (10–25 rows, 12–65% of NAV), so the uncovered residual stays
   `Unclassified` by the renormalization rule (docs/16) and the UI's
   honesty copy names it (docs/09). The stored symbol is probed BARE
   first; only when it answers nothing does the chain retry the `.TO`
   form (prod catch: Canadian funds are commonly stored BARE —
   VEQT/VUN/XSP/XGRO — while Yahoo answers only `VEQT.TO`; the stored
   row is never rewritten by the probe — docs/05).

No provider is consulted at breakdown time — the exposure engine
stays cache-only (docs/05). Rows land with `source="issuer"` /
`source="vanguard"` / `source="yfinance"` (docs/02); the chain returns
exactly ONE provider's answer, and a re-import replaces all non-manual rows
together so one fund never carries two providers' weights.

**M169 adds no seam.** The breakdown drill (`?drill=<bucket key>`, docs/16
§Exposure drill) is aggregation over the rows the walk already reads — the
same `etf_holdings`, `fundamentals` and `positions` cache, the same request,
one extra query parameter. It never calls a provider, so drilling a bucket
under privacy mode costs nothing and changes no freshness. Its per-row
`provenance_source` is a report of WHICH cached source supplied that row's
weights, not a new fetch.
### Provider registry (M168) — coverage is DATA, not code

Before M168 the four tables above and the two static tables docs/16 §Sector
chain reads were Python literals: `VANGUARD_US_FUNDS` (50), `VANGUARD_CA_FUNDS`
(42), `ISHARES_US_HOLDINGS_CSV` (1), `STATIC_SECTOR_BY_SYMBOL` (28) and
`FUND_SECTOR_MIX_BY_SYMBOL` (11). Every engine was already generic; the five
tables feeding them were not, so a Vanguard Canada listing added after the
seed — or any Schwab/SPDR/Fidelity/T. Rowe product — answered NOTHING until
somebody edited code and deployed. That is the gap Mark's "is what we added
for sector/geography/underlying securities done generically?" named.

They are rows now: **`provider_fund_registry`** (docs/02), one table,
`UNIQUE (family, ticker)`.

| family | payload | what a `verified` row means |
|---|---|---|
| `vanguard_us` | — | `GET {profile}/{TICKER}-AdditionalFundData` answered 200 with a parseable `holdingDetails` |
| `vanguard_ca` | `port_id`, `venue_suffix` | the `gpx/graphql` gateway answered non-empty rows for that portId |
| `ishares_us` | `endpoint` (product stem) | `{endpoint}/latest-holdings.csv` answered 200 with a parseable CSV |
| `static_sector` | `sector` | a hand-curated ticker → GICS sector (docs/16 tier 3) |
| `fund_sector_mix` | `sector_mix` (JSON of sector → percent as decimal TEXT) | a fund's OWN published GICS mix (docs/16 tier 4) |

**Tenancy: none, deliberately.** No `user_id` column — these are issuer facts
about the world, the operator-level shared data AGENTS §2 already makes an
exception for alongside `fx_rates`. One verdict is written once and answers
every tenant; `etf_holdings` stays strictly per-user (docs/02). A registry row
is created by a probe, a CLI command or migration `0050`, never by a request's
user.

**Status and the verdict rules.** `status` is `verified` or `miss`.

* `verified` — a live 200 with a parseable payload. `verified_at` is stamped
  (it is the sweep's clock) and `verified_by_run` names the path that wrote the
  row: `seed:0050`, `discovery:<path>`, `cli:set`, `cli:verify`.
* `miss` — the issuer's own answer that there is no such fund. `miss_count`
  and `last_probe_at` are stamped instead. A `miss` row short-circuits the
  chain with **no network call**, which is the "no repeated 404 probes"
  discipline the old literals gave by omission (the `dead_symbol` lesson,
  docs/07 §Dead symbols).
* **Anything else writes NOTHING.** A timeout, a 500, a non-404 status, a 200
  whose body does not parse: none of those is an answer about the TICKER, and
  a registry that recorded "SPY is not a Vanguard fund" because Vanguard was
  down for ten minutes would be a coverage bug with no way to notice it. Only
  the issuer may retire a ticker.

**Discovery** — how a ticker nobody hand-listed joins a family
(`services/market_data.py`, all three behind the single privacy guard):

1. **`vanguard_us`** — the endpoint is parametrized by ticker, so the probe
   IS the lookup: 404 → `miss`, 200 with rows → `verified`.
2. **`vanguard_ca`** — two issuer documents, both fetched once per process and
   memoised (a failed fetch is NOT memoised, so an outage never becomes an
   answer): the portId list in `GET /en/product.model.json`, then one
   profile+listings GraphQL query over those portIds yielding ticker, portId
   and MIC. `XTSE` → `.TO`, `NEOE` → `.NE`; an MIC this table does not know
   registers the fund with NO suffix (its legs land bare, exactly as an
   unregistered fund's do today) and `provider-funds set --venue-suffix`
   corrects it. The gateway is then probed ONCE for that portId — its
   **empty arrays** are the verdict (an unknown portId answers 200 with empty
   arrays, not an error).
3. **`ishares_us`** — the product screener above; a ticker it names is probed
   at `{stem}/latest-holdings.csv`, a ticker it does not is a `miss`.

Both catalogues are memoised in-process with a *manual* cache rather than
`lru_cache`, because the two answers must stay apart: a successful fetch (even
an empty one) is an ANSWER and is cached, a failed fetch returns `None` and is
not.

**Range limit (the additive-module pattern).** This generalises **Vanguard +
iShares only** — the two families the portfolios in production hit first.
Schwab / SPDR / Fidelity / T. Rowe get their own card when a real portfolio
needs one. Each new family is exactly three things and touches no existing
code: **one URL template + one parser + one `ProviderFundFamily` value** (plus
its `miss`/`verified` rules and a seed). docs/16 §Sector chain's
`canonical_sector` already absorbs a new provider's spelling without a schema
change. The M147 country map (`company_country_map`) is deliberately left as a
code constant — it keys ISIN *issuing* prefixes, not funds, so it is outside
this card's "fund families" contract and is the next candidate if a portfolio
needs an ISIN prefix nobody has listed.

**The maintenance surface** (in place of a code change — AGENTS §4):

```bash
cd backend
uv run fintrackerctl provider-funds list                          # census
uv run fintrackerctl provider-funds list --family vanguard_ca --json
uv run fintrackerctl provider-funds show vanguard_ca VCN
uv run fintrackerctl provider-funds set static_sector SAP --sector Technology
uv run fintrackerctl provider-funds set vanguard_ca NEWF --port-id 1234 --venue-suffix .TO
uv run fintrackerctl provider-funds verify vanguard_us VOT       # probe now
uv run fintrackerctl provider-funds reverify --older-than-days 180 [--dry-run]
uv run fintrackerctl provider-funds remove ishares_us IVV
```

`reverify` is the **quarterly sweep** entry point: it re-probes every
`verified` row older than 180 days (or never stamped) and rewrites whatever
verdict the issuer gives now, so a fund that went dark flips to `miss` on the
spot instead of silently serving a dead endpoint. It never deletes a row
first — no verdict means no write — and it skips `static_sector` /
`fund_sector_mix`, which have no endpoint to probe: those two are hand-curated
data edited with `set`.

**Verification.** `tests/test_m168_provider_registry.py` (13): migration 0050
seeded exactly the 132 rows the five literals held, the two static families
are byte-identical to them (the card's "moved, not altered" test), three funds
outside the seed resolve end-to-end with zero code change, an unknown fund is
recorded once and never probed again, an outage records nothing, the registry
has no `user_id` while holdings stay per-user, and the CLI's list/set/verify/
reverify/remove. `tests/test_vanguard_holdings.py` pins the negative cache and
the seeded pairs; `git grep VANGUARD_US_FUNDS backend/app` returning nothing is
asserted as a test so it cannot be skipped.

### Sector enrichment tiers (M166) — where a sector actually comes from

The provider chain is a GEOMETRY chain: every tier exists to widen the
fund's row set. SECTOR is a different question, because no tier of it
guarantees a per-holding sector. Measured live 2026-10-11 on the portfolio
that motivated M166: the two providers that DO carry the column delivered
it for every row they wrote (the iShares CSV for all 506 IVV rows, the
Vanguard CANADIAN gateway for 3,638 of 3,663 VIU rows), and the Vanguard US
profile delivered ZERO sectors across 25,858 rows — its holding rows carry
`ticker` / `isin` / `cusip` / `sedol` and `marketValuePercentage`, never a
sector. That one payload left 62.77% of the sector pie `Unclassified` while
the same companies sat labelled in the same cache.

The response is a read-time tier chain in `services/sector_map.py` (never a
write path, never a fetch — docs/16 §Sector chain for the exact order):

1. **Canonicalise what the provider wrote.** `canonical_sector` maps the
   GICS-2016 spellings the issuers use (`Information Technology`,
   `Financials`, `Health Care`, `Consumer Discretionary`, `Consumer
   Staples`, `Materials`, `Telecommunications`) onto the yfinance names, so
   one company is one slice. A `Sector` cell that is not a sector
   (`Cash and/or Derivatives`, `Not Applicable`) resolves `None` and the
   row falls through.
2. **The cross-fund index** — the label that arrived with ONE of the
   tenant's funds answers for every other one. This is where the real win
   is: an IVV issuer-CSV import labels the S&P 500, and those exact tickers
   are the ones VTI's Vanguard rows leave blank (89.5% of VTI's weight by
   value, 97.6% of VEA's, 98.3% of VOO's). It is exact, not estimated —
   same ticker, same company, same GICS sector — and it REFUSES a bare
   ticker whose rows disagree, because `SU` is Suncor in Canada and Sogefi
   in Italy (the M143 lesson).
3. **The static ticker map** — `provider_fund_registry` family
   `static_sector` (M168: a ROW, added with `fintrackerctl provider-funds
   set static_sector <TICKER> --sector <Sector>`), the M147 country-map
   pattern, hand-curated for the handful of heavy names no provider in the
   cache has ever labelled.
4. **The published fund mix** — family `fund_sector_mix`: a fund's OWN
   GICS allocation, captured from the Vanguard fund profile
   (`.portfolioComposition.weightedExposures.sectorExposure`), applied only
   to equity value that steps 1–3 could not place, renormalized against the
   mix's own total, and reported `estimated`. Vanguard answers
   `isavailable: false` for VXUS / VT / VEA / VWO / BNDX and the
   sector-specific funds publish sub-industry names instead of sectors, so
   those funds are deliberately absent from the table — no published
   number, no estimate.

The chain degrades the way every other enrichment does: an unknown ticker,
an unregistered fund and a privacy-mode session all end at `Unclassified`,
which is the honest answer. Extending coverage means adding a verified
column to a provider, a verified row to a static table, or a fresh capture
of a fund mix — never inferring a sector from ticker text.

### Enrichment sessions (M153) — the pool is never the provider's to hold

The provider chain runs in three places and only one of them is a request:
`POST /securities/{id}/holdings/import` runs it inline, while
`fund_enrichment._run_enrichment` (queued by `api/imports.py` and
`api/trades.py` on every committed batch) and
`api/securities._run_deepen` (queued by the holdings import/PUT) both open
their own session out of `deps.get_engine()` — the API's pool — and run it
in a FastAPI `BackgroundTask`.

On the background paths a provider wait is not the request's to spend: the
connection the task holds is one the pool cannot hand to anybody else until
the provider answers, and a transaction left open across that wait is
precisely the `idle in transaction` row that took the API down in M148 and
that M152's prod sweep caught on a healthy operator run. So
`market_data.release_before_provider_wait(session, security?)` runs before
every wait on those paths — `_run_enrichment` after its `session.get`,
`ensure_sub_fund_composition` after its security map and before each of the
loop's lookups/imports/backfills, and `ensure_fundamentals` before
`fetch_fundamentals`. It commits the read transaction back to the pool; it
is a no-op write-wise because every write those seams make already commits
itself (`import_fund_composition`'s delete+insert, `ensure_fundamentals`,
`record_quote_outcome`, `_cache_closes`), so `import_fund_composition`'s
delete+insert+commit stays ONE transaction exactly as before — the release
always runs before any of its writes exist.

Two properties are pinned rather than assumed
(`tests/test_m153_enrichment_session.py`, which asks both questions from
inside a monkeypatched provider call):

- **The release must not expire the caller's objects.** A plain `commit()`
  expires every loaded ORM instance, and the next attribute read silently
  re-opens the transaction that was just released — the fix would then work
  for the two background-task sessions that happen to set
  `expire_on_commit=False` and quietly not work for anyone else. So the
  helper pins the flag off for the duration of the commit and restores it.
- **`ensure_sub_fund_composition` owns its transaction.** Both of its
  callers — and they are the only two — are background tasks with their own
  session, so a release there can never commit a request's partial work or
  roll one back. A future caller that hands it a request session is outside
  this contract and must say so.

Still the same class, deliberately NOT changed here: `backfill_range` and
`ensure_recent_price` are reached by the price paths (`_run_backfill`,
`/portfolio/positions`, `POST /securities/{id}/refresh`) with a session that
may be in a transaction across their fetch. They are not on the enrichment
path — `ensure_sub_fund_composition` releases before calling
`backfill_range` — and they are a request-path seam with its own callers to
weigh. **Closed by M161, below.**

### Price-path sessions (M155/M161) — the same rule, and the one caller it must not reach

M153 left two seams of the same class alone because they sit on the **price**
paths, not the enrichment path. They are closed now, and the per-caller
decision is the whole change:

| wait | caller | session | the release |
|---|---|---|---|
| `fetch_history` | `api/securities._run_backfill` (BackgroundTask, `POST /securities`) | its own, out of `deps.get_engine()` | **at the caller**, right after its `session.get` |
| `fetch_history` | `api/securities.refresh_security` (`POST /securities/{id}/refresh`) | the request's | **at the caller**, right after `owned_or_404` |
| `fetch_history` | `market_data.ensure_sub_fund_composition` | the background task's | already released by M153 |
| `fetch_history` | `services.symbol_repair.repair_symbol` (`scripts/repair_symbol.py`) | the operator CLI's | **deliberately NOT released** — below |
| `fetch_latest_close` | `market_data.ensure_recent_price` — reached by `_run_ensure_recent_price` (BackgroundTask, the market-hours fan-out) and `GET /securities/{id}/overview` | the task's own / the request's | **inside the function**, immediately before the quote |

The two functions are placed differently on purpose:

- **`ensure_recent_price` releases inside itself.** It does its own
  `newest_price` SELECT immediately before fetching, so no caller can place a
  release between that read and that wait. The market-hours fan-out and the
  security overview still use it; `valuation.compute_positions` now does not
  fetch and uses its batched cache-only reader below.
- **`backfill_range` releases at its callers.** Its fetch is its first act, so
  a caller-side release is equivalent — and one caller must not get it:
  `services/symbol_repair.repair_symbol` repoints the symbol and drops the
  stale closes INSIDE one transaction and backfills afterwards, and
  `scripts/repair_symbol.py` rolls that transaction back for a dry run
  (`--apply` is the write). An internal release would commit an operator's
  read-only dry run, and a fetch that then failed would leave the bars dropped
  with the repair row never written. That caller is pinned
  (`tests/test_m155_price_session.py::test_the_symbol_repair_keeps_its_transaction_across_its_backfill`).

The remaining request paths were weighed, not assumed: `GET
/securities/{id}/overview` and `POST /securities/{id}/refresh` are read-only up
to their wait (none writes before it; `refresh_security`'s own `db.commit()` is
still what persists its work), so the release commits only the read transaction.

**What it costs.** The market-hours fan-out (`_run_ensure_recent_price(force=True)`,
one BackgroundTask per held non-dead security) owns its own session and gets it
back before the quote. `GET /portfolio/positions` no longer invokes that seam;
the batched compute is cache-only and has no provider-wait commit.

`tests/test_m155_price_session.py` (8 tests) asks both M153 questions from
inside every provider seam on these paths — no session the caller opened may
be in a transaction, and no client backend on the database may be `idle in
transaction` — against a really-committed tenant driven through
`deps.get_engine()`, the engine the background tasks and `get_db` draw from.
RED before the fix: 6 failed, busy at every wait.

### Provider-wait ledger (M175) — the whole class, in one pass

M148, M152, M153 and M155/M161 each closed the seam the report happened to
name, and each wave left the class open behind a paragraph that said so
("still the same class, deliberately NOT changed here"). M175 is the
exhaustive pass: every path in `backend/app` that READS from the database and
then waits on a provider is listed here, with that path's answer — released,
not a session at all, or a deliberate exception with a test.

**The release seam has one canonical home now.** M153's helper lives in
`services/session_release.py::release_before_provider_wait`, because the class
it closes now spans the fx client, the LLM batch worker and the notification
dispatcher as well as the price/enrichment paths.
`market_data.release_before_provider_wait` remains a re-export, so every call
site and every paragraph below it stays literally true. `session_release.py`'s
docstring is the CONTRACT: release a session that has nothing pending, never
one carrying uncommitted work.

| wait | caller | session | the release |
|---|---|---|---|
| `fetch_history_candidates` | `services/deep_backfill.py::backfill_security`, walked by `deep_backfill_all` (`scripts/deep_price_backfill.py`, **dry run by default**) | the sweep's own | **inside the function**, after its own `_prefetch`/`trade_evidence` reads |
| `fetch_benchmark_history` | `benchmarks.backfill_symbol` — the four picks, via `benchmark_refresh.run_once` (worker) and `scripts/backfill_benchmarks.py` | the worker's own / the CLI's | **inside the function**, the last DB contact before the fetch |
| `fetch_valet_series` | `fx.latest_rate` — `GET /securities/{id}/overview` and `api/accounts._build_public`; `compute_positions` now reads cached rates in one batch | the request's | **inside the function**, after its `newest()` read |
| `fetch_valet_series` | `fx.get_rates_between` (`GET /fx` range) | the request's | **inside the function**, on the `commit=True` side only |
| `fetch_valet_series` | `fx.refresh_pair`, from `fx_refresh.run_once` (the scheduled worker) | the worker's own | **at the caller**, right after `pairs_behind` |
| `fetch_history` | `split_repair.repair_split_window` (`scripts/repair_split.py`, `POST /securities/{id}/split-repair`) | the operator's / the request's | **inside the function**, before its single fetch (the dry run's rollback discards what is written BELOW it) |
| `provider.chat` (60 s egress, retried) | `categorization._process_one_batch`, queued by `POST /suggestions/run` | the task's own | **inside the function**, after the batch's reads |
| `provider.chat` (60 s egress) | `POST /users/me/llm-test` (`api/users.py::test_llm_connection`) | the request's | **at the route**, immediately before the call — `get_current_user`'s auth read is what parked it; the probe writes nothing |
| `provider.chat` (60 s egress) | `POST /imports/suggest-profile` → `services/column_mapping.py::_llm_suggest`, through `suggest_column_mapping` | the request's | **at the route**, in `suggest_profile.core`, after `build_provider` — the response is exactly the mapping parsed below, so nothing is pending; `_llm_suggest` itself takes no session |
| `provider.chat` (60 s egress) | `POST /assistant/ask` → `services/assistant.py::ask` (docs/57) | the request's | **inside the function**, after the context reads and `build_provider` — the proposal row is persisted from locals after the call, so nothing is pending at the wait |
| `provider.list_institutions` (dynamic sync Protocol) | `GET /sync/providers/{slug}/institutions` (`services/sync/service.py::list_institutions`) | the request's | **inside the function**, before the dispatch — the auth read is what parked it |
| `provider.create_agreement` / `provider.pull_delta` / `provider.revoke` (dynamic sync Protocol) | `POST /sync/agreements`, `POST /sync/agreements/{id}/pull`, `POST /sync/agreements/{id}/revoke` (`services/sync/service.py`) | the request's | **inside the function**, after the dispatch snapshot, before the call |
| `send` (SMTP 10 s / webhook 10 s) | `notify_delivery._dispatch` (worker + `POST /notifications/deliveries/run`) | the worker's own / the request's | **inside the loop**, before EVERY send |
| `ensure_recent_price` | `price_refresh.run_once` — the scheduled price-refresh worker (M187, §Scheduled price refresh), one session per tick | the worker's own | **inside the function** (`ensure_recent_price` releases, M161) — the sweep adds no second transaction-holding path |
| `resolve_symbol`, `probe_quote` | `GET /securities/lookup`, `GET /securities/quote-check` | the request's | **at the route**, before the call (the auth read is what parked it) |
| `fetch_fund_composition` | `POST /securities/{id}/holdings/import` — the provider chain runs INLINE here, issuer → Vanguard → yfinance | the request's | **at the route**, right after `owned_or_404` |
| `fetch_dividend_history` | `services/dividend_refresh.refresh_one` — bounded persistent worker, one active job at a time | worker's own short-lived sessions | sessions close before provider wait; rechecks tenant privacy and ownership before fetch/store |  |
| `send_invite_email` | `POST /sharing/invites` (the send happens AFTER the invite commits) | the request's | **at the route**, before the send |

Already closed, unchanged by M175: `_run_enrichment`,
`ensure_sub_fund_composition` and `ensure_fundamentals` (M153);
`_run_backfill` + `refresh_security` (with `backfill_range` releasing at those
callers) and `ensure_recent_price` (M161); `scripts/backfill_etf_holdings.py`
(M152); `repair_report.scan`'s three-phase split.

**The three callers that deliberately keep their transaction.** Each is a write
contract that a release would break, and each is pinned rather than trusted:

* `services/symbol_repair.repair_symbol` (M155's exception, unchanged) —
  repoints the symbol and drops the stale closes inside ONE transaction that
  `scripts/repair_symbol.py` rolls back for a dry run.
* `api/trades._resolve_security` — the investment-CSV staging path flushes its
  `import_batches` row BEFORE it plans the file's rows, and plan-time may have
  created `securities` rows, so the lookup's wait sits on a transaction holding
  flushed, uncommitted writes. A release there would commit half an import the
  user never committed. Pinned by
  `tests/test_m175_provider_wait_sessions.py::test_the_import_staging_keeps_its_transaction_across_its_lookups`,
  which also proves the flushed row is still invisible to another connection
  afterwards.
* `tax/acb.py::FxRateBook._backfill` → `fx.get_rates_between(commit=False)` —
  `rebuild_user_pools` NEVER commits (docs/02), so the fetched rate rows ride
  the caller's transaction; that is why the range seam releases only on its
  `commit=True` side. Pinned by
  `test_get_rates_between_releases_on_its_committing_side`.

**What it costs.** A release is a commit, so a path pays one commit per wait
that actually fetched, and **zero** when the answer was already cached — the
price seams stop at a close dated today, `deep_backfill` skips
dead/cash/never-held rows before the fetch, `latest_rate` returns on a hit
without reaching the fetch, and the dispatcher commits state it had already
decided. Nothing here pays a commit per ROW (`import_fund_composition`'s
delete+insert stays ONE transaction; the release always runs before its writes
exist).

**The enumeration was blind to the LLM Protocol (t_4eb1fcbc).** `chat` sat in
NONE of the guard's three sets, and its dynamic-dispatch branch matched on the
receiver's variable name (`provider.<method>`), so every `provider.chat(...)`
call site was invisible — `test_every_provider_wait_in_app_is_classified`
passed while two request routes held their session across the 60 s wait:

* `POST /users/me/llm-test` (`api/users.py::test_llm_connection`), and
* `POST /imports/suggest-profile` → `services/column_mapping.py::_llm_suggest`.

Both were confirmed by probe at `d488589`: **1 connection `idle in
transaction`** on the `SELECT users…` auth read for the whole blocked `chat()` —
one pinned pool slot per probe (a burst of "Test connection" clicks drains the
pool: the M148 shape). The collector now matches dynamic dispatch on the
METHOD ALONE, so the receiver's name can never hide a wait (`provider`,
`resolved.provider` and a return value are the same call):

* `LLM_PROTOCOL_METHODS = {chat}` — the LLM Protocol (`app/llm/base.py`),
  beside `DYNAMIC_PROVIDER_METHODS` for the four bank-sync methods;
* `suggest_column_mapping` joined `PROVIDER_SEAM_LEAVES` as the seam that wraps
  `chat` — which is what puts `app/api/imports.py::suggest_profile.core` on the
  ledger as a ROUTE row, so the route's release is checked rather than
  described.

The four newly visible keys are classified exactly as the table above says:
two route releases, the batch worker's pre-existing release
(`_process_one_batch`), and `_llm_suggest`'s `no-session` (it takes no session
parameter — its caller releases before calling it).
`tests/test_llm_session_release.py` pins both routes on a one-slot pool with a
delayed `chat()`: **RED before the fix with `QueuePool limit of size 1 overflow
0 reached, connection timed out`, GREEN after.**

**The guard.** `tests/test_provider_wait_ledger.py` is the executable form of
the table above: it AST-walks `backend/app`, collects every call that is a
network primitive (`httpx`/`requests`/`yfinance`/`smtplib`/`urllib`/`socket`),
a call to one of the app's provider seams (`PROVIDER_SEAM_LEAVES`), or a
dynamic Protocol dispatch (`DYNAMIC_PROVIDER_METHODS`, `LLM_PROTOCOL_METHODS` —
matched on the method, never on the receiver's name), keys each by its
enclosing function, and FAILS when a key is not in its ledger — a NEW member of
the class cannot appear quietly — as well as when a ledger entry matches no
code path any more (stale) or when a function classified as releasing does not
actually contain the release call. `tests/test_m175_provider_wait_sessions.py`
(15 tests) asks both M153 questions from inside every seam released here, and
pins the two exceptions: **RED before the fix (15 failed / 5 passed), GREEN
after (20 passed)**.

### Symbol matching (M154) — one index for two spellings

A provider publishes holding tickers BARE (`005930`, `9984`, `HSBA`,
`LRCX`) while the user's own `securities` rows are frequently
EXCHANGE-SUFFIXED (`005930.KS`, `9984.T`, `HSBA.L`) — `lookup_symbol`
resolves a bare ticker to the canonical listing when the row is created.
Measured on prod, 2026-10-11: 17,530 of the demo tenant's non-NULL holding
symbols carry no dot, while 825 of its 1,496 securities are suffixed.
Every seam that matched one spelling against the other did it on the RAW
string, so the two never met:

* `import_fund_composition` left `holding_security_id` NULL (3,117 of
  23,042 rows linked — 13.5%);
* `ensure_sub_fund_composition` then resolved the bare probe over the
  network and INSERTED the resolved row — colliding with the row that
  already existed: **231** caught `uq_securities_symbol` violations in the
  M152 seed run, each a wasted provider call plus a failed INSERT. The
  `except IntegrityError` there called itself a "concurrent deepen race";
  it was a lookup miss, and its recovery re-queried the BARE spelling, so
  the sub-fund was skipped silently.

**The fix is one index**, `market_data.security_symbol_index`, which every
matching seam now reads: `import_fund_composition`,
`ensure_sub_fund_composition` (both the probe and the end-of-run backlink)
and `PUT /securities/{id}/holdings`. Its rules:

* the EXACT spelling always wins — `VUN` answers for the bare `VUN` row
  even when a `VUN.TO` twin exists (the CSV import's twin rule, docs/05);
* a stored `005930.KS` also answers for the bare `005930`, but only when
  that stem is UNAMBIGUOUS. `005930.KS` (KSC stock) and `005930.KQ` (KOSDAQ
  fund-facts row) share a stem, and a shared stem never links by guesswork:
  the index carries no stem entry for it, and the deepening loop lets
  `lookup_symbol`'s answer pick the listing — and then REUSES that row;
* only KNOWN venue suffixes are stripped — `market_data.EXCHANGE_SUFFIXES`
  (45 entries: the 37 prod's `securities.symbol` carried at fix time plus
  the 20 `company_country_map` already knew; verified against every
  suffixed row on prod — 823 distinct symbols, 1,135 rows, zero
  refusals). Warrants, units, rights and preferred-share suffixes (`.WS`,
  `.U`, `.RT`, `.PR`) are deliberately absent — those are different
  instruments that must never inherit the common share's row. An unknown
  venue suffix degrades to exact-only matching, which is all it did before
  M154 — never to a wrong link;
* the creator REUSES the row `lookup_symbol` resolved to instead of
  inserting it (`index.get(resolved)` before the INSERT), and the
  `IntegrityError` recovery re-queries by the RESOLVED spelling — so a
  genuine race now recovers instead of silently skipping. A bare probe the
  index already answers costs NO provider call at all.

Weight math is untouched: linking sets one FK column, never a
`weight_pct`, so a fund's imported weights and the docs/16
unclassified-residual rule that reads them are unchanged. The pies' own
resolver (`services/exposure_breakdown._resolve_sub_security`) is
deliberately NOT changed: it stays EXACT-match by design (a provider
stores the venue suffix precisely so that match works — the fund-leg note
above), it does not read `holding_security_id`, and this change therefore
alters no exposure bucket. Measured, not assumed: of the 27,609 holdings
on prod, 3,666 already match a user security exactly (only **111** of them
a fund that itself has composition rows, i.e. the rows that actually
recurse) and **zero** more holdings would start recursing under stem
matching — so exact-only resolution is not costing look-through anything
today. `tests/test_m154_symbol_link.py` (9) pins the
index rules, both write seams, the reuse guard's two halves (recovery
included) and the zero-INSERT property — measured by counting
`INSERT INTO securities` statements at the engine, so "no violation was
even attempted" is a fact rather than an inference.

**Existing rows are re-linked without a re-import** — an operator UPDATE
against prod, dress-rehearsed as a SELECT with aggregates first (the
data-purge rule): every unlinked holding whose exact spelling, or whose
unambiguous stem, answers through this same index. Ambiguous stems are
left NULL on purpose; the deepening loop links them when its resolver
names a listing. Ran 2026-10-11: rehearsal 2,176 → `UPDATE 2176` (4,063 →
6,239 of 27,699 rows linked estate-wide), `Σ weight_pct` unchanged, and an
in-transaction A/B of the pies (links vs NULLed, rolled back) came back
byte-identical on all four dimensions.

### Venue aliases (M164) — one venue, one spelling

`securities.exchange` holds a VENUE CODE, and `uq_securities_symbol UNIQUE
(user_id, symbol, exchange)` (docs/02) means a second spelling of one venue is
a second row for one listing. The provider really does spell a venue two ways —
LIVE-PROBED 2026-10-11 from the api container:

* `600900.SS` answered `SHH` / `fullExchangeName: "Shanghai"` while prod row
  2894 `603986.SS` was stored under `SHSE`; `PNDORA.CO` answered `CPH`
  "Copenhagen" while row 1721 stores `OCSE`;
* a BARE Japanese ticker answers a microsite code and its `.T` form answers the
  exchange: `7182` → `TOKY1` "Japan Post Bank Co Ltd", `7182.T` → `JPX`
  "Tokyo" "JAPAN POST BANK CO LTD" (`2454`/`2454.T`, `1299`/`1299.HK` the same
  way — `JASDQ`/`JPX`, `HONGK`/`HKG`).

Two rows for one listing then split its prices, trades and ACB pool across two
ids (the M117 defect, docs/02 §securities) — and, because the M154 index counts
two rows sharing a stem as two LISTINGS, every bare-stem fund leg behind them
stays `holding_security_id = NULL` on an ambiguity that is not one. Measured on
prod 2026-10-11: the last 10 NULL legs estate-wide were exactly tenant 343's
`600900` and `PNDORA` legs, behind rows 2555/2705 and 1721/2502.

**The table is `services/venue_codes.py`** — `VENUE_ALIASES`, an explicit
alias → canonical map, plus `canonical_exchange()` (`strip().upper()` then the
map; an unknown code passes through). Two seams read it and
`security_symbol_index` deliberately does not:

* `market_data._normalize_exchange` (every provider payload — the same seam
  that has mapped Yahoo's Canadian MICs `TOR`/`TSE`/`XTSE`/`XTSX`/`TSXV` to
  `TSX` since M142) writes the canonical code, so a second spelling cannot
  reach the table again. USER-typed codes (`POST /securities`, the CSV
  import) stay exactly as typed — docs/02's normalization contract is case and
  whitespace, and the repair below catches what slips through;
* `security_dedup` groups on the canonical code, so migration
  `0049_merge_venue_aliases` (a re-run of the M117 repair) merges the twins
  with the machinery it always had — keeper = most price history (tie → lowest
  id), every reference repointed, `acb_pools` collisions REFUSED and reported —
  and rewrites the survivor's exchange to the canonical spelling, so the next
  provider answer collides instead of duplicating. Idempotent: a second run
  reports `merged_groups=0`. A lone row with an unusual venue code is the
  user's own statement of venue and is never re-spelled.

**A pair enters the table only with same-listing evidence**, and it is per pair
of venue codes — never per symbol, because one symbol stem can name different
companies on different venues. Probed the same day: bare `9503` is SAUDI
"Baazeem Trading Co" while `9503.T` is JPX "KANSAI ELECTRIC POWER CO INC";
bare `2330` is JSD "For-side.com Co Ltd" while `2330.TW` is TAI "TSMC"; `ROP`
is NMS "Roper Technologies" while `ROP.SW` is EBS "ROCHE PS"; and in prod
`VIU`/`VIU.TO`, `VUN`/`VUN.TO`, `XBB`/`XBB.TO`, `BN`/`BN.TO`,
`SHEL`/`SHEL.L` are a US-listed name against a Canadian/UK listing (the CSV
import's twin rule, docs/05). `SHOP` NYSE vs `SHOP` TSX is the same case. All
of them stay two listings. Codes the provider names SEPARATELY are not aliases
either: the Nasdaq tiers `NMS` "NasdaqGS" / `NGM` "NasdaqGM" / `NCM` "NasdaqCM"
/ `NAS` "Nasdaq" stay distinct, and `CHIX` is left unruled (its prod rows probe
to `CXE` "Cboe UK", but the code also names Chi-X Canada).

**Re-linking the legs the twins blocked** is `scripts/relink_holdings.py` →
`services/holding_relink.relink_holdings`: the M154 operator UPDATE, made
re-runnable and testable. It matches through the SAME index the write seams
use (exact spelling, then an unambiguous bare stem), dry-run by default, sets
only `etf_holdings.holding_security_id`, and never touches a `weight_pct`. Ran
on prod after migration 0049: `UPDATE 10` — the `600900`/`PNDORA` legs now
link to the merged rows (estate linked 6,245 → 6,255 of 27,699), and the
exposure pies came back byte-identical on all four dimensions (the pies'
resolver does not read this column, docs/05/M154).

## Symbol search fallback (M142)

A typed **name** (`APOTEX`) 404s on both quote candidates — a
name-not-ticker problem Yahoo's own search endpoint answers in ONE call:

- **Endpoint**: `GET https://query2.finance.yahoo.com/v1/finance/search?q=<query>&quotesCount=10&newsCount=0`,
  field names and values LIVE-PROBED 2026-10-10 (from this host and from the
  api container): `symbol`, `shortname`, `longname`, `exchange` (the venue
  code — `TOR`), `exchDisp` (the human name — `Toronto`), `quoteType`. The row
  carries **no currency** — the heuristic below derives it, and still does:
  unlike a `Ticker.info` body, a search row has no provider currency to
  believe (§Listing currency, M163).
- **Only `quoteType ∈ {EQUITY, ETF, MUTUALFUND}` counts as a hit.** A name
  matching only an INDEX/CURRENCY/NEWS row is not a listing this app can hold
  a price for, so it answers as no hit at all.
- **`market_data.resolve_symbol(symbol)` is the one resolver** behind the
  autofill, each step costing at most one network call:
  1. `direct` — the bare symbol answers a `Ticker.info` probe;
  2. `suffix` — the `.TO` form does (Canadian tickers are usually queried
     without the suffix yfinance requires);
  3. `search` — both 404'd: one search call, and EXACTLY ONE equity hit means
     that row's **canonical symbol is adopted** (`APOTEX` → `APTX.TO`) —
     including its name, kind, venue and currency, built from the search row
     with no second call;
  4. `ambiguous` — **more than one listing answered**: several search hits
     (`APTX.TO` + `APTX.NE`) *or* both quote candidates (`VGRO` + `VGRO.TO`)
     → a **pick-one chooser**; a silent guess is never allowed to become a
     stored symbol (docs/09 §New security — the symbol suggestion chip).

  Zero hits — or a search Yahoo cannot answer — is `none`.

### Candidate resolution (M143) — the contract

**Bare + `.TO` both resolving = require disambiguation.** Steps 1 and 2 used
to run in order and return on the FIRST hit, so whichever market answered
first won by ordering — and it was not ambiguous-looking, which is exactly
how the wrong instrument got stored: `VGRO` answered *Virtus Silvant Growth
Opportunities* (NYSEArca, USD) while Vanguard's `VGRO.TO` was never probed
(security 294, three wrong closes, USD stamped on a CAD holding).

- **Both candidates are probed.** When BOTH name a real listing the answer is
  `ambiguous` with a chooser carrying symbol, name, kind, **currency** and
  venue for each side. The `"`.TO` → CAD, otherwise USD` heuristic still
  runs, but only AFTER a quote has been chosen — it never chooses the quote.
- **A candidate is create-ready.** `SecurityLookupCandidate` carries
  `kind` + `listing_currency`, so applying a pick needs no second lookup.
  Re-resolving a picked symbol would land on the same collision and hand the
  user the chooser they just answered (an infinite pick loop).
- **A `.info` body must NAME a security.** `_is_real_quote` requires a
  `shortName`/`longName` that is a non-blank **STRING** (read through
  `_adopted_name`, the SAME helper both payload builders use — so a body can
  only be accepted on a name it would also be adopted under) — LIVE-PROBED
  2026-10-11, bare `VFV` answers
  `{"quoteType": "ECNQUOTE", "exchange": "NMS"}`, a venue stub that names
  nothing; under the looser four-key test it passed as a quote and the
  payload fell back to the typed string for the name and USD for the
  currency ("VFV, USD" — security 296). A body that names no security — or
  whose name field is a number, an object or blank — is a MISS, and
  resolution falls through to `.TO` and then to search (§Body contract).
- **Two documented opt-outs.** `resolve_symbol(..., disambiguate=False)` is
  for a caller with NO picker — a background price fetch or an operator
  repair naming its target explicitly — and keeps the old bare-first order.
  `prefer_currency=` is for a caller that already knows the listing's
  currency from the USER'S OWN DATA: the CSV import's currency cell
  (`trades._resolve_security`) selects the matching side outright, so a
  collision does not demote a file row to the `.TO`-probe fallback and
  overwrite the currency the file stated. A hint that fits neither (or both)
  side disambiguates nothing and the chooser still appears.
- **Single-market symbols are untouched**: one real candidate →
  `direct`/`suffix` as before, no chooser, no UX tax. A suffix-typed symbol
  always lands here (`SHOP.TO`, `RY.TO`), and so does a bare ticker whose
  `.TO` sibling does not answer. Note that "single-market" is a fact about
  YAHOO, not about the ticker's fame: **bare `AAPL` asks**, because
  `AAPL.TO` is a real TSX listing (`APPLE CDR (CAD HEDGED)`, CAD — probed
  2026-10-11 alongside `AAPL`'s Nasdaq USD line). Two named listings is two
  answers; the chooser says so in one row per side instead of guessing which
  one the typist meant.
- **Repair**: `repair_symbol` resolves with `disambiguate=True` when no
  `--symbol` is given, so a collision REFUSES with the chooser's message
  (`several listings match 'VGRO' — pick one and repair again`) instead of
  keeping whichever side answered first; an explicit `--symbol VGRO.TO` skips
  the collision check because naming it IS the choice.
- **`lookup_symbol()` keeps its single-result contract** (`dict | None`) and
  delegates to `resolve_symbol`, so the CSV-import autofill
  (`trades._resolve_security`) and the sub-fund creator get the fallback for
  free; `ambiguous` answers `None` there, because that contract cannot say
  "pick one". `GET /securities/lookup` calls `resolve_symbol` directly, which
  is how the chooser reaches the client (docs/08).
- **Listing-currency heuristic** (`_listing_currency`): `.TO` → CAD, else
  Yahoo's Canadian venue codes (`_normalize_exchange`'s TSX/TSXV convention
  plus NEO — `APTX.NE` reports currency CAD live) → CAD, else USD. It reads
  the RESOLVED candidate's venue, never the typed string, so the M116 FX
  seams (docs/06) see the right currency on every path. **M163 makes it
  the FALLBACK**: a `Ticker.info` body's own `currency` field wins when it
  is a well-formed code (`005930.KS` → KRW, live), because this guess
  answers USD for every market it does not name — see §Listing currency
  (M163). Search rows carry no currency at all, so they are exactly the
  path the heuristic still owns.
- **A true ticker keeps its typed spelling.** The direct/suffix path still
  stores what was typed (`VEQT` stays `VEQT`, not `VEQT.TO`): the CSV-import
  dedup matches existing rows on the RAW symbol cell (docs/05
  `_resolve_security`), so adopting the `.TO` form there would put a second
  securities row next to every `VEQT` a Questrade file imports. Only a
  name→ticker resolution adopts a new symbol — there the typed string was
  never a symbol at all. A symbol the user **picked from the chooser** is
  likewise stored verbatim: they named the listing, and `VGRO` would keep
  resolving bare-first on the next fetch. So the import seam binds a bare
  cell to its repaired `.TO` TWIN when the file's currency agrees with the
  row's (`trades._find_existing_security`) — a USD cell still cannot latch
  onto a CAD row — and one holding never splits across two securities.
- **`fetch_history` heals the same way.** Both candidates empty → one search
  call → a single equity hit is fetched under its canonical ticker, so a row
  stored under a name gets closes again on its next refresh. Several hits are
  never guessed at (a background fetch has no picker) and answer `{}` like a
  miss; M141's `dead_symbol` guard owns the repetition.
- **Privacy**: `_guard_external_fetch()` runs before the first quote probe and
  before the search, so privacy mode makes zero packets and the lookup RAISES
  the documented `403 privacy_mode_blocks_external_api` (a refusal, not a
  miss).
- **Call discipline**: one search call per lookup, 5 s timeout, 429 retryable
  under the module's capped backoff; every other failure is a terminal "no
  answer" — a lookup must never become a retry storm.
- **Resolution log policy**: exactly ONE `INFO` line per heal, naming both
  spellings — `symbol search healed the price fetch: APOTEX -> APTX.TO
  (APOTEX HEALTH CORP)` — so an operator can find rows still spelled as names
  in `docker logs`. A lookup itself never logs; it just answers.

### Provider-name artifacts (M162) — an ID triple is not a name

A provider payload can carry a NAME that is not a name at all: LIVE-PROBED
2026-10-11 in the api container, `Ticker("005930.KQ").info` answers
`quoteType=MUTUALFUND`, `shortName="005930.KQ,0P0000B2XZ,18569122"` (the
symbol, a Morningstar-style fund id and a numeric secId, comma-joined),
`longName="Samsung Electronics Co Ltd"`, `exchange=KOE`, `currency=KRW`.
`000660.KQ` answers the same shape (`000660.KQ,0P0000AZ1B,4519170`). The
payload builders adopt `shortName` first, so that triple was persisted
verbatim as a security NAME: six such rows reached prod
(`kind=mutual_fund`, `exchange=KOE`, `listing_currency=USD` — the currency
heuristic knows no KRW venue), created by the deepening loop's
`lookup_symbol` → INSERT path from a yfinance-composed fund leg spelled
`.KQ`.

The damage is the ambiguity, not the garbage name: while a `005930.KQ` row
existed next to the real `005930.KS`, stem `005930` was AMBIGUOUS under
the M154 rule, so bare legs stayed unlinked and a refresh re-linked fresh
`.KQ` legs to the junk rows by exact match — a treadmill.

- **The predicate**: `market_data.provider_name_is_artifact(name)` — two
  or more commas, every part non-empty, and NO whitespace anywhere. Human
  instrument names are prose and carry spaces (`Principal Fds, Inc.
  California ...` — SRCMX, a real fund — has a comma and fails on
  whitespace).
- **Both payload paths refuse it.** `_is_real_quote` refuses a
  `Ticker.info` body whose ADOPTED name (`shortName`, else `longName`) is
  an artifact — the whole BODY, not just the name: the longName is the
  underlying company's real name, but the record is a fund-facts MIRROR
  of the equity, and adopting it under the longName would re-create the
  `mutual_fund` twin that shadows the stem. `_search_equity_quotes` drops
  a search row whose adopted name is an artifact, so neither the
  single-hit adopter nor the human chooser can re-introduce one.
- **The answer degrades to `none`**, which every caller already handles:
  the search fallback answers this symbol with ZERO rows (live-probed), so
  `lookup_symbol` → `None` and the deepening loop `continue`s — no row, no
  ambiguity, no treadmill. `GET /securities/lookup` 404s
  `lookup_unavailable` like any other miss.
- **Non-regression pinned by test**: the real `005930.KS` body
  (`shortName="SamsungElec"`, `EQUITY`, `KSC`) resolves exactly as before
  (`backend/tests/test_m162_artifact_guard.py`, 7 tests).

### Listing currency (M163) — the provider's field, not the venue guess

`_listing_currency` is a VENUE heuristic (`.TO`/TSX/TSXV/NEO → CAD, else
USD) and it was the ONLY source of `securities.listing_currency`. It is
blind to every venue it does not name, so it stamped **USD** on every
non-North-American listing the app adopted. LIVE-PROBED 2026-10-11 in the
api container, one run, seven listings:

| `Ticker(...).info` | `currency` | `exchange` | stored before M163 |
| --- | --- | --- | --- |
| `005930.KS` (Samsung, KRX) | `KRW` | `KSC` | **USD** |
| `000660.KS` (SK hynix, KRX) | `KRW` | `KSC` | **USD** |
| `9984.T` (SoftBank, Tokyo) | `JPY` | `JPX` | **USD** |
| `RY.TO` (Royal Bank, TSX) | `CAD` | `TOR` | CAD |
| `AAPL.TO` (Apple CDR, TSX) | `CAD` | `TOR` | CAD |
| `AAPL` (Nasdaq) | `USD` | `NMS` | USD |
| `HSBA.L` (HSBC, LSE) | `GBp` | `LSE` | USD (still — see below) |

The label is not cosmetic: docs/06 pool math, docs/12 valuation and docs/15
exposure all spend it in an FX conversion, so a KRW holding was valued at
the KRW/USD ratio — the security-296 class generalised to every market the
heuristic does not name. Prod's two real KSC rows (1381/1669, 1382/1670)
carry USD and reference **zero trades**, so nothing was mis-valued yet;
they are the repair target below.

- **The contract: the body's `currency` WINS when it is well-formed.**
  `market_data._normalize_currency_code` is the one reader — a non-empty
  string of exactly three ASCII letters, all-upper (kept) or all-lower
  (normalized up). The two `Ticker.info` builders (`_payload_from_info`,
  `_candidate_from_info`) read it through `_info_listing_currency`, so
  every path that adopts a listing carries the real currency: the add
  form's autofill and `POST /securities`, the CSV import's
  `_resolve_security` (whose `prefer_currency` now disambiguates a
  collision against real currencies), the deepening loop's sub-fund
  creator, and `repair_symbol`.
- **The heuristic is the FALLBACK, unchanged where nothing else can say.**
  `_listing_currency` still decides for (a) every SEARCH row — they carry
  no currency field at all, live-measured (M142) — and (b) any body whose
  `currency` is absent or type-junk. The body-contract fuzzer
  (`test_market_data_body_contract`) mutates a real body's `currency`
  with `float("nan")`, `True`, `b"AAPL"` and friends; none of them may
  reach a payload, which is what the three-letter + uniform-case rule
  buys.
- **Mixed case is refused: `GBp` is pence — and since M167 that refusal
  takes the whole LISTING with it**, not only the code (§A minor-unit
  listing is refused). Storing `GBP` would multiply every price by 100 —
  the M87 split class of silent wrong number — and `GBp` is a base the FX
  book cannot serve, so a body that says it is a body this app does not
  adopt. Before M167 such a row silently fell back to the venue heuristic
  and kept a **USD** label; that fallback was the last wrong label the app
  carried by design, and it is gone.
- **A currency the FX book cannot serve is a loud 422, never a wrong
  number.** Ruling on "fetch-on-demand vs refuse-at-create": **adopt and
  let FX serve it.** KRW is in `CURATED_FX_BASES` because BoC publishes a
  live `FXKRWCAD` series — live-probed 2026-10-11, `…/observations/FXKRWCAD/json`
  → 200 (`2026-10-01 → 0.001046`) while the DKK control 404s — so the
  6-hourly worker keeps the pair warm, `latest_rate` fetches a true miss,
  `FxRateBook` backfills a trade-date span, and `GET /fx/status` reports
  the pair as soon as one row holds it. A DKK-class currency (one BoC
  never published) answers `422 fx_rate_missing` at report time — the
  documented "add a manual rate" path: loud, not silently wrong. `POST
  /securities` never refuses a currency either: refusing at create would
  turn a wrong label into a rejected holding, and the provider is the
  only party that knows the answer.
- **`CandidateSeries.currency` (M144) still reads the heuristic.** The
  history path fetches closes, not bodies, and an extra `.info` call per
  candidate is not worth it. `score_series` reads the trade price RAW
  first — currency-agnostic, which is the reading a KRW trade priced in
  KRW hits — and only then fx-converts. A row whose trades are stated in
  another currency and whose closes never agree reports
  `no_listing_agrees`: an honest gap, the same verdict it had before.
- **Repair**: the repair report now SEES the class — `currency_mismatch`
  compares the stored currency against the provider's, so the two prod
  KSC rows are named by `scripts/repair_needed_report.py` and repaired
  with `scripts/repair_symbol.py --security-id <id> --apply`. The
  corroboration rule is unchanged: `repair` only when EVERY recorded
  trade states the live currency, else `review` with no command.

Tests: `backend/tests/test_m163_provider_currency.py` (33), every
`currency` value verbatim from the probe above.

### A minor-unit listing is refused (M167) — the quoting unit is a fact about the listing, not the venue

M163 left London as a "known residual": a body whose `currency` is a
minor-unit code (`GBp`) was refused **as a code** and the row fell back to
the venue heuristic, which answers **USD** — a wrong label carried by
design, and the last one the app knowingly kept. Two honest exits existed
(board card `t_8b9d7b6e`): (a) refuse the listing outright at the lookup
seam, like M162's artifact refusal; or (b) adopt it and normalise the unit
— store `GBP` and divide the provider's quoted prices and trades by 100
through a per-venue unit factor that the price cache and the trade
importer both carry. The card asked for a ruling **before** code, and the
probe that was meant to size option (b) killed it.

**London has no single quoting unit.** LIVE-PROBED 2026-10-11 in the api
container, one run (`m167_lse_probe.json`, plus a second run for Cboe UK):
every distinct `.L` listing
prod's `securities` table holds (`HSBA.L`, `SHEL.L`, `AV98.L`, `HKLD.L`,
`PEEL.L`), the one its `etf_holdings` look-through names but has no row
for (`AZN.L`), three more listings (`VOD.L`, `RIO.L`, and `HSBAL.XC` —
the same HSBC line on Cboe UK) and two controls:

| `Ticker(...).info` | `currency` | `exchange` | `regularMarketPrice` |
| --- | --- | --- | --- |
| `HSBA.L` (HSBC) | `GBp` | LSE | 1395.8 |
| `SHEL.L` (Shell) | `GBp` | LSE | 3793.5 |
| `AZN.L` (AstraZeneca) | `GBp` | LSE | 11998.0 |
| `PEEL.L` (Peel Hunt) | `GBp` | LSE | 93.0 |
| `VOD.L` (Vodafone) | `GBp` | LSE | 118.35 |
| `RIO.L` (Rio Tinto) | `GBp` | LSE | 7126.0 |
| `HSBAL.XC` (HSBC, Cboe UK) | `GBp` | CXE | 1395.8 |
| `AV98.L` (ADIB Sukuk II) | **`USD`** | LSE | 100.432 |
| `HKLD.L` (Hongkong Land) | **`USD`** | LSE | 7.41 |
| `SHEL` (NYSE, control) | `USD` | NYQ | 100.18 |
| `RY.TO` (control) | `CAD` | TOR | 273.78 |

The quoting unit is a fact about the LISTING and never about the venue:
**four of the six `.L` listings prod holds are USD-quoted on the very same
`exchange: LSE`**, so option (b)'s per-venue factor would have divided
`HKLD.L`'s $7.41 by 100 — and it had no signal to divide BY on the price
path either, because `Ticker.history` carries no currency and a `.info`
call per London fetch is the extra round trip M144 deliberately refused.

**The ruling: (a), narrowed to the signal that is real.**

- **A minor-unit body is a MISS, not a fallback.** M163's
  `_normalize_currency_code` still refuses the code; M167 adds the reading
  that can TELL a minor-unit spelling apart from an absent or type-junk
  field — `market_data.provider_currency_is_minor_unit` (three ASCII
  letters, MIXED case) — and `_is_real_quote` refuses the whole body on it,
  exactly like the M162 artifact rule. The junk half of that distinction is
  load-bearing: the body-contract fuzzer's corpus (`float("nan")`, `True`,
  `b"AAPL"`, the artifact triple) must keep falling back to the venue
  heuristic; only a real minor-unit spelling refuses. Every consequence
  runs through seams that already exist: `resolve_symbol("HSBA.L")` answers
  `none`, `lookup_symbol` answers `None`, `GET /securities/lookup` 404s
  `lookup_unavailable`, the add form falls into the UNRESOLVED path
  (empty `listing_currency`, `dead_symbol=True`, no backfill queued —
  §Unresolved symbols), and the deepening loop `continue`s without
  creating a row.
- **The name-search fallback never adopts a UK listing whose unit the row
  cannot state.** A search row carries no `currency` field at all
  (live-measured, M142), so the venue is the only thing a builder could
  key a label off — and both UK venues fail that test:
  `_search_equity_quotes` drops a row whose symbol carries the `.L` suffix
  or whose venue canonicalizes into `_UNIT_UNKNOWN_VENUES` (`LSE`, `CXE`) —
  the same seam M162 uses to drop artifact rows, the same arbitration
  principle: a loud gap beats a silent 100×. `CXE` is there on its own
  probe (2026-10-11): `HSBAL.XC`, `fullExchangeName: "Cboe UK"`, answers
  `GBp` at `regularMarketPrice` 1395.8 — the SAME pence number `HSBA.L`
  reads, one HSBC listing on a second venue — while every other `.XC`
  spelling tried (`AZN.XC`, `RIO.XC`, `VOD.XC`, `SHEL.XC`, `BP.XC`,
  `ULVR.XC`) 404s. A venue with no probe behind it is not in the set, and
  the standing repair report (`currency_mismatch`) is what names it if one
  ever lands wrong.
- **The price path is deliberately UNCHANGED.** With adoption sealed, no
  pence row can enter through market data, so the fetch seams keep their
  one-call shape (M149/M161). A blanket `.L` fetch refusal was measured and
  REJECTED: it would refuse `HKLD.L`/`AV98.L`, whose USD closes and USD
  labels are correct today. A hand-created London row stays the user's own
  claim (M163's "never refuse at create" ruling) and is named by the
  standing repair report, whose `currency_mismatch` check compares the
  stored label against a provider answer that now refuses to be read as a
  base at all.
- **The split-repair axis is untouched.** `to_as_traded` multiplies closes
  by split RATIOS; a quoting-unit factor is a different axis, and M167
  introduces none — `scripts/split_probe.py` / `repair_split.py` still
  re-derive rather than scale.

**Prod measured, and the card's premise was false.** The card said there
were zero `.L` rows to repair; the SELECT says **9** across three tenants
(demo user 1: 3; real tenant 343: 5; the M146 probe account 516: 1), all
`exchange=LSE`, all `listing_currency=USD`, **zero trades, zero cached
prices, zero ACB pools, zero watchlist rows, zero fundamentals** on any of
them, and 21 `etf_holdings` weight rows pointing at them — they are
look-through legs of global funds, not holdings. Five of the nine are the
pence class (`HSBA.L` ×3, `SHEL.L`, `PEEL.L`) and carry the wrong label;
four are USD-quoted on the LSE and are labelled correctly today. The
repair takes the five to the state this contract describes for a listing
the app cannot value — the UNRESOLVED marker (`listing_currency=''`,
`dead_symbol=True`) that the add form itself writes — and leaves the four
alone (docs/10 §M167).

Tests: `backend/tests/test_m167_pence_listing_refusal.py` (65), every
`currency` / `regularMarketPrice` value verbatim from the probe above, and
M163's own suite re-pinned
(`test_a_pence_listing_is_refused_rather_than_mislabelled_usd`).

### Symbol repair (`backend/scripts/repair_symbol.py`)

Correcting a row that was stored under a name is a **correction in place**,
never a delete + re-create: the row keeps its id, so the watchlist
attachment, trades, cached prices, ACB pools, fundamentals and composition
rows pointing at it all stay attached (docs/02 declares no ON DELETE rules —
`DELETE /securities` pre-checks every reference and refuses an in-use row,
which is exactly why re-adding loses history).

- Thin CLI over `app/services/symbol_repair.repair_symbol`: resolution goes
  through the SAME `resolve_symbol` seam the add form uses; only the fields
  market data owns are written (symbol, name, kind, listing currency,
  exchange); a hand-entered price row, a manual fundamentals block and manual
  composition rows are untouched (manual wins).
- **Dry run by default**, `--apply` writes; with `--apply` the normal refresh
  machinery runs in the same step (`market_data.backfill_range`, the exact
  call behind `POST /securities/{id}/refresh`), so closes land and the M129
  day-delta has a prior session to measure against.
- The database comes from `DATABASE_URL`; from outside the stack the run is
  `docker exec fin_tracker-api-1 python scripts/repair_symbol.py
  --security-id <id> --apply` (the image COPYs `scripts/`, and the container
  owns both the app package and the settings).
- **Refusals are reported, never worked around**: nothing resolves, several
  listings match (the human picks one and repairs again — since M143 that
  includes a bare-vs-`.TO` collision, which used to be silently resolved
  bare-first), another row already owns the target `(user_id, symbol,
  exchange)` key (merging duplicates is `services/security_dedup.py`'s job),
  or the row is already canonical.
- **A symbol change drops the closes fetched for the symbol it left**
  (M143): `fetch_history` keys off the symbol, so a year of *Virtus* bars
  next to a Vanguard symbol is the exact defect being repaired. Manual rows
  survive (manual wins), and the follow-up backfill spans the **oldest
  dropped close**, not just the default 365-day window — a 5-year series
  drops and comes back as a 5-year series (`--days` widens the window
  explicitly). An exchange- or name-only correction is metadata about the
  SAME instrument and never touches price history.

### Repair-needed report (`backend/scripts/repair_needed_report.py`)

**M141/M142/M143 validate a symbol at ADD time. They cannot reach backward.**
Every wrong row called out in this document was found because a human happened
to look — 294 (bare `VGRO` stored as *Virtus Silvant*, USD, three wrong
closes), 293 (`APOTEX` stored as a SYMBOL), 296 (`VFV, USD` out of a body
that named no security), 298/299 (the same name-stored-as-symbol row for two
tenants, 298 flagged dead along the way) — and each was corrected by hand with
`scripts/repair_symbol.py --security-id N`. The next one is still out there.

`app/services/repair_report.py::scan` is the standing answer: a re-runnable,
**read-only** sweep over every OWNED row (`deep_backfill.held_security_ids` —
a committed trade or a watchlist row, the same set the deep backfill sweeps)
that reports which of them disagree with the market. It writes NOTHING; every
`repair` finding prints the exact `scripts/repair_symbol.py` command, because
a repair never picks a listing for a human (§Symbol repair).

**It cannot be an endpoint, by design.** AGENTS §2 keeps fetching out of
report endpoints ("Ingestion and report endpoints never fetch — cache reads
only"), so the surface is this operator CLI — the same shape as
`split_probe.py` and `deep_price_backfill.py`.

#### Family 1 — bare collisions (the M143 class)

`resolve_symbol` answers `ambiguous`, so two listings are candidates for the
row's symbol. Which one the LEDGER means is asked in the order the rest of
this document already trusts:

1. **trade currencies** — one stated currency across every committed trade and
   exactly one candidate quoting it = an answer. A CAD buy against a USD
   listing is the whole of the 294 bug, and it needs no network call to see.
2. **recorded trade prices** — only when (1) cannot say (two CAD listings such
   as `APTX.TO` + `APTX.NE`): `deep_backfill.score_series` scores each
   candidate's closes over the row's own evidence window and a candidate must
   explain a MAJORITY of the trades it can be compared against (§Series
   validation). Nothing is fetched when the currency already decided.
3. **a human** — neither can say, so the finding is `review` with both
   candidates named and a `--symbol <PICK>` placeholder. The report never
   picks.

| check id | severity | fires when |
| --- | --- | --- |
| `bare_collision_wrong_side` | repair | the ledger's side ≠ the side the row stores |
| `bare_collision_agrees` | info | two listings answer, the ledger agrees with the stored side |
| `bare_collision_undecided` | review | no stated currency and no usable price evidence |
| `bare_twin_rows` | review | one user owns BOTH `X` and `X.TO` — the 293/298 leftover `security_dedup` can never merge (it only merges rows whose NORMALIZED keys collide) |

#### Family 2 — live-symbol heuristics (the M141/M142 class)

One `resolve_symbol` per row, then the stored identity is compared with what
the listing says NOW:

| check id | severity | fires when |
| --- | --- | --- |
| `name_stored_as_symbol` | repair | `match: search` — the stored symbol is a NAME and the listing is `result.symbol` (293) |
| `currency_mismatch` | repair / review | `listing_currency` ≠ the live venue's — `repair` only when EVERY recorded trade states the live currency (the corroboration is what makes it actionable), else `review` with no command |
| `exchange_mismatch` | repair | the venue differs and the live symbol equals the stored one (a metadata fix: same symbol, so no closes are dropped) — 294's hand-repair leftover |
| `identity_missing` | repair | an unresolved row (`listing_currency: ""`, §Unresolved symbols) now resolves |
| `name_mismatch` | review | the row's name and the listing's name disagree (a rename may have been deliberate) |
| `name_is_symbol` | info | the row's name is its own ticker — cosmetic |
| `no_listing` | review | nothing answers and M141 has not booked the verdict yet |
| `dead_symbol_stale` | review | `dead_symbol` is set but the listing answers again (298) — a `probe_dead` refresh disproves it |
| `dead_symbol_confirmed` | info | `dead_symbol` and a live probe agrees: nothing to do |
| `unresolved_still_no_listing` | info | an unresolved row that still resolves to nothing: honest as-is |
| `fetch_refused` | info | privacy mode (zero packets) or the seam refused the call |

Names are compared tolerantly (`_names_agree`): equal after folding case,
`&` and punctuation, or one contained in the other — Yahoo's `shortName` is
not a stable string, and the stored `Virtus Silvant Growth Opportuni`
truncation is the same instrument as the live `…Growth Opportunities ETF`.

#### Noise control

- **A canary answers first.** `probe_quote(--canary`, default `AAPL`) must come
  back `ok` before any row is judged. If it does not, the run prints ONE
  `SCAN INCOMPLETE` line and exits **2** — a network blip must never read as
  "every ticker you own was delisted" (the `unavailable` vs `not_found`
  distinction M141 draws, applied to the whole run).
- **Privacy mode still costs zero packets** (docs/11): the row's owner binds
  the ContextVar before its own probe, refused rows are reported, and when
  every in-scope owner is in privacy mode the canary is not sent either.
- **The `CASH` placeholder is skipped** (`deep_backfill.never_held_shares`):
  `CASH` really resolves to Pathward Financial (§Backfill depth).

#### Sessions: three phases, none across a wait

docs/01 §Sessions and the event loop. `scan()` reads every row (evidence,
counts, the owner's privacy flag) into FROZOWN plain values in ONE session and
closes it; the whole network phase then runs with no session in existence; a
second session scores the price tie-breaks and closes. Pinned from inside
both network seams by `tests/test_repair_report.py::
test_no_session_survives_a_provider_wait`, which also asserts no backend is
parked `idle in transaction` on the scan's own `securities` SELECT — the M152
incident's exact symptom.

```bash
ssh example.internal 'docker exec fin_tracker-api-1 python \
    scripts/repair_needed_report.py --user-id 343'          # one tenant, human report
... scripts/repair_needed_report.py                          # every tenant
... scripts/repair_needed_report.py --user-id 343 --json     # machine-readable
... scripts/repair_needed_report.py --sleep 0.5              # Yahoo pacing
... scripts/repair_needed_report.py --canary VFV.TO          # different canary
... scripts/repair_needed_report.py --verbose                # keep yfinance's
                                                             # expected .TO 404s
```

A run with no `--user-id` sweeps EVERY tenant, and a prod box carries fixture
tenants whose symbols were never meant to resolve: measured 2026-10-11, the
all-tenant run reported 135 rows / 41 repair / 80 review, 72 of the reviews
being `no_listing` on rows such as `M130AAA` and `NODATA42M126`. Scope the
scheduled run to the tenant that matters (`--user-id 343`).

Exit codes: **0** nothing to do, **1** at least one row needs a repair, **2**
the scan could not run (canary missed). `--json` emits
`{canary, canary_ok, probe_error, rows_scanned, rows_skipped, counts, findings,
elapsed_seconds}` with one finding per `{security_id, user_id, symbol, check,
severity, detail, suggestion}`.

#### Periodic

A report that has to be remembered rots, so the scheduled run is part of the
deliverable: `~/.hermes/profiles/soft-dev/scripts/fintrack_repair_report.sh`
runs the command above weekly through the host scheduler (the same path the
M148 autoheal watchdog uses — example.internal has no unattended crontab, and the
scheduler lives on the box that already `ssh`es there), always exits 0 so a
finding is never mistaken for a failed run, and delivers the report. Any host
with cron can run the same line:

```cron
0 6 * * 1  ssh example.internal 'docker exec fin_tracker-api-1 python scripts/repair_needed_report.py --user-id 343'
```

## Unresolved symbols (M143)

**A row that no market data could identify says so; it never borrows an
identity.** The failure path used to be the quiet one: a `quote-check` miss
plus a pre-filled form default produced a row that looked exactly like a
resolved one, and a `.info` body that named no security was enough to stamp
a name and a currency on a row (`VFV, USD` — security 296).

- **`listing_currency: ""` is the marker.** `POST /securities` with an empty
  currency (docs/08) creates the row with the name the caller supplied, NO
  invented venue/currency, `dead_symbol = true` +
  `dead_symbol_reason = "No market data for <symbol> when the row was
  created"`, and **no backfill queued** — there is nothing to fetch, and no
  reason to spend three 404s learning that again. The M141 machinery is
  reused deliberately: "Try refresh" (`probe_dead`) remains the way to
  disprove the flag if the listing appears later.
- **The resolver never fabricates.** `_is_real_quote` requires a name that is
  a non-blank STRING (§Body contract), so a venue stub, a type-junk name
  field or a body that is not even a dict answers as a MISS rather than as a
  listing whose name falls back to the typed string.
- **UI**: `/securities/:id` renders `Unresolved symbol — no market listing
  found, enter prices by hand` in place of the `exchange · currency` line
  (docs/09 §New security). The add form's Currency field starts empty.
- Prices are entered by hand (manual wins), so the row is usable; every
  automatic fetch stays off it until a refresh proves a listing exists.

### Backfill depth (M144) — how far back the cache goes, and how it widens

Two different depths live side by side, and only one of them is automatic:

| path | window | trigger |
| --- | --- | --- |
| `securities.BACKFILL_DAYS = 365` (`_run_backfill`) | the last 365 days | a security is created, added to the watchlist, a trade lands, `POST /securities/{id}/refresh`, `POST /portfolio/refresh` |
| `services/deep_backfill.py` | **the row's own need-date → today** | an operator runs `scripts/deep_price_backfill.py` |

The daily sweep is a freshness mechanism: it keeps the newest ~365 days of
closes current and the M129 day-delta has a prior session to measure against.
It never walks further back, so a position bought before that window has no
close to value it with — and ONE held security without a close blanks every
bucket it was held in, because `account_value` marks the bucket `valued: false`
and the portfolio merge propagates `None` (docs/08 §Honesty). Real case
(M144): `VUS` in account 459 had ZERO cached rows while being held
2025-02-27 → 2026-02-27, which turned every YTD day it was held into a red
"no valuation for this period" row and started the portfolio chart at the day
it was sold.

`deep_backfill.deep_backfill_all` (CLI: `scripts/deep_price_backfill.py`) is
the one-time, re-runnable sweep that closes the hole:

- **Start bound = `min(committed trade date, watchlist added_at)`** per
  security — the row's own need, never a hard-coded decade. A security held
  since last month costs one window and nothing earlier. Staged rows never
  drive it (docs/03), and a `CASH` placeholder — a row whose trades have all
  moved zero shares — is skipped outright (`cash_placeholder`): it represents
  cash, not a listed instrument, and `CASH` really resolves to Pathward
  Financial on NasdaqGS.
- **Dead symbols are skipped** (`dead_symbol`, M141) unless `--probe-dead`,
  which is the same escape hatch `POST /securities/{id}/refresh` offers.
- **Privacy mode refuses per owner**: the row's tenant binds the ContextVar
  lock before its own fetch, the guard raises, and the sweep records
  `fetch_refused` instead of fetching — one privacy-mode tenant never leaks
  another's calls into its context.
- **Idempotent and abortable.** Caching goes through `_cache_closes`, which
  inserts only missing `(security, date)` rows and commits per security: a
  re-run adds nothing, and a run killed halfway leaves the finished rows
  cached and the rest resumable by running it again. Every row's verdict is
  logged (`deep backfill: VUS: 2022-03-04 -> 2026-10-11 via VUS.TO, 1043
  close(s) inserted`) and returned as a `BackfillResult`.
- **Dry run by default** (`--apply` writes): the preview makes the same
  fetches and reports which listing each row would be deepened under.

Runbook (this is the M134 §3.5 "backfill depth" promise, shipped here):

```bash
# from outside the stack — the image COPYs scripts/, the container owns the
# app package, the settings and DATABASE_URL
docker exec -i fin_tracker-api-1 python scripts/deep_price_backfill.py               # report
docker exec -i fin_tracker-api-1 python scripts/deep_price_backfill.py --user-id 343 # one tenant
docker exec -i fin_tracker-api-1 python scripts/deep_price_backfill.py --apply       # write
```

The engines below the cache need no change for the deeper history to show up:
`account_value`/`total_return` already handle long windows, and the YTD table
simply stops printing "no valuation" once closes exist under it.

### Series validation (M144) — which listing a bare ticker means

`fetch_history` answers with the FIRST candidate that produces closes (the
stored spelling, then `.TO`, then the M142 single-search canonical). That is
right for a background refresh and WRONG for a sweep that is about to write
four years of history, because a bare ticker can be a completely different
instrument. Measured live 2026-10-10:

| stored symbol | bare answer | `.TO` answer | the ledger says |
| --- | --- | --- | --- |
| `VUS` | *Virtus U.S. Dividend ETF*, NYSEArca, USD, $30.23 | *Vanguard Morningstar U.S. Total Market Index ETF (CAD-hedged)*, $130.70 | buy @ $79.69 CAD on 2022-03-04; `VUS.TO` closed $75.88 that day |
| `VGRO` | *Virtus Silvant Growth Opportunities ETF*, NYSEArca, USD, $25.99 | *Vanguard Growth ETF Portfolio*, $48.10 | buy @ $30.86 CAD on 2022-03-03; `VGRO.TO` closed $27.97 |

(The one cached `VGRO` row, 2026-10-09 = 25.99, is already the Virtus series —
the daily sweep picked it the same way. That row is a data repair, not a
backfill, and is out of M144's scope.)

So the sweep never caches the first answer blindly:

- `market_data.fetch_history_candidates(symbol, start, end)` returns EVERY
  answering candidate as a `CandidateSeries(symbol, currency, closes)`, each
  carrying the currency its closes are quoted in (`_listing_currency`'s own
  heuristic);
- `deep_backfill.choose_series` scores each candidate against the row's
  committed trade prices on the trades' own dates. Two readings per trade
  (docs/07 §Series validation), because a row's stated currency and the
  number the broker exported can disagree:
  - **raw** — the trade price UNCONVERTED against the candidate's close,
    within a TIGHT `RAW_SERIES_TOLERANCE = 0.01` (commission/spread scale;
    measured ≤0.52% on prod rows). This is the pre-M132 case: the row has
    no `trades.currency` and reads in the account's CAD, but its price IS
    the US listing's own close — prod `VTI` bought at 329.91 against VTI's
    $329.91 USD close. The wide tolerance below is only safe AFTER fx;
    never compare it to a bare number.
  - **converted** — the close fx-converted into the trade's stated currency
    through the cache in whichever direction resolves (`_FxLookup`,
    cache-only), within the wide `SERIES_TOLERANCE = 0.30` (broker rows
    carry commission and the close is a different moment; the separation
    being made is "this listing" vs "a different listing", which the M144
    cases put 4x apart);
  A candidate must agree with a MAJORITY of the trades it can be compared
  against (either reading).
- a candidate whose data does not reach any trade date is *unverifiable*, and
  only wins when nothing else is checkable (`verdict=unverified` — the legacy
  order, nothing contradicts it);
- **when trades ARE comparable and no listing agrees, nothing is cached**:
  the row is reported `no_listing_agrees` with the candidate list, and the
  honest gap stays. An honest red row beats a fabricated valuation.

`GET /securities/lookup`, the add form and the daily sweep still take the
first answer — fixing THAT root (a typed ticker is trusted at add time) is a
separate change, not part of this sweep.

## Benchmark series (M173) — the vs-Benchmark panel's index prices

`GET /portfolio/benchmarks` (docs/28) compares the portfolio against one of
four fixed indexes. Until M173 that comparison was a declared stub
(`benchmark_return_pct = 0`, `benchmark_points = 0`); the series it needs now
exists, cached like any other price series and keyed to the BENCHMARK symbol —
**never** to a user's `securities` row (a benchmark is not held, and a security
for `SPY` would put a fund in every tenant's picker that no trade explains).

**The four picks and their LISTING VENUES** — a code constant,
`services/benchmarks.py::BENCHMARKS`, because a venue is a fact about the
index, not tenant data:

| benchmark | venue ticker | quote currency | venue |
| --- | --- | --- | --- |
| `SPY` | `SPY` | USD | NYSE Arca |
| `XIU` | `XIU.TO` | CAD | TSX |
| `XBB` | `XBB.TO` | CAD | TSX |
| `XAW` | `XAW.TO` | CAD | TSX |

**Fetch.** `market_data.fetch_benchmark_history(symbol, start, end)` is the one
seam — the privacy ContextVar lock and the M141 backoff apply exactly as they
do to a security. It differs from `fetch_history` in two deliberate ways:

- **the pinned ticker only, with no candidate fan-out and no M142 search
  heal.** Resolving a bare stem can answer as a DIFFERENT instrument (the M144
  `VUS` = *Virtus U.S. Dividend ETF* vs `VUS.TO` case), and comparing a
  Canadian investor against the wrong index is worse than answering nothing;
- **the provider's split-adjusted close** (`as_traded=False`). `prices`
  un-adjusts to match the ledger's quantities (§Price frame above); a benchmark
  has no ledger, and un-adjusting a price index would read a 2:1 split as a
  −50% return.

**Cache.** `benchmark_prices` (docs/02 §benchmark_prices, alembic
`0052_benchmark_prices`) — `(symbol, date)` UNIQUE, `close`, the venue's
`currency`, `source`. **No `user_id`**: one fetch writes one row and every
tenant's panel reads it (the `fx_rates` operator level, AGENTS §2). Writes are
**insert-only**, so a re-run adds missing dates and rewrites nothing — the
manual-wins rule `prices` and `fx_rates` already carry.

**Who writes it.** Two callers, both through the seam above:

- `services/benchmark_refresh.py` — the scheduled worker (started from
  `lifespan`, `benchmark_refresh_interval_seconds`, tests force it off with
  `BENCHMARK_REFRESH_ENABLED=false`). It fetches only the symbols whose newest
  cached close is missing or older than `REFRESH_MIN_AGE_DAYS`, so a current
  series costs zero network calls.
- `backend/scripts/backfill_benchmarks.py` — the one-time deep walk, from the
  earliest COMMITTED trade in the ledger (M144's own "the row's own need" rule,
  at the granularity a shared series actually has) to today. Dry run by
  default.

**Privacy (docs/11).** `benchmark_prices` is shared, so there is no honest
per-tenant answer and the WHOLE sweep is refused while ANY user has
`privacy_mode` on (`benchmarks.privacy_blocks`) — one tenant's decision cannot
be overridden on behalf of the others, and a shared row cannot be un-fetched
for one of them. The M150 `fx_rates` rule, for the same reason.

**The benchmark reads are cache-only.** Everything the panel serves on the
BENCHMARK side never fetches: the return is computed from cached closes and the
cached rate book (§Cache strategy above — `fx.latest_rate(..., allow_fetch=False)`),
and a missing rate is an honest `null` rather than a face-value comparison
across currencies. Fetches happen only in the two writers above. The
portfolio side of the same endpoint uses the cache-only M184
`valuation.compute_positions` pass; automatic refresh remains the
market-hours BackgroundTask and explicit refresh routes. The FX-inclusive
return rule itself lives in docs/28 §Benchmark calculation.

## Manual price overrides

A `prices` table holds per-security/date price history — manual entries plus cached yfinance closes (docs/02). Behavior:
- Manual override takes precedence over yfinance for its effective date.
- The valuation layer reports **two independent freshness facts**: market-date age from `prices.date`, and provider-fetch age from nullable `prices.fetched_at`. The market date never masquerades as a sub-day fetch time.
- **Stale** = newest usable price (override or yfinance) older than 7 calendar days; the indicator renders source + market-date age, e.g. `yfinance · today` / `manual · Oct 15 (stale)`. Exactly 7 calendar days is fresh; only age > 7 is stale. Fetch age never affects `price_age_days` or `price_stale`.
- `fetched_at` is stamped only when a yfinance provider response actually arrives and `_cache_closes` persists its result; timestamp capture happens at the provider-response seam, never in cache-only reads. The ContextVar is reset before each provider attempt and before guarded/cache-only refresh paths, so a prior successful fetch in the same request context cannot lend its timestamp to a later failed, skipped, or privacy-blocked call. Writes are bounded to inserted rows and the newest/displayed row (including a no-op today refresh) to avoid historical churn on page-load fan-out. Manual rows are never stamped. Migration 0057 adds nullable `prices.fetched_at` with no backfill. Manual API writes clear it; demo seed and split-repair-derived rows stay NULL. `NULL` means no fetch evidence and clients omit fetch-age copy entirely.
- `ensure_recent_price(session, security, today, *, force=False, probe_dead=False, intraday_update=False)` is the single seam that may fetch. `force=False` is used by `GET /securities/{id}/overview`; `force=True` is used by the automatic market-hours BackgroundTask and skips the freshness window. `valuation.compute_positions` is cache-only (M184), with explicit refresh still available. All request modes stop at a close already dated `today`, bounding a forced refresh to one network call per security per calendar day; `intraday_update=True` (M187, §Scheduled price refresh) is the ONE opt-in that may advance a close dated today, and only the scheduled worker passes it.

## SSE cached-price streaming

Server-Sent Events (SSE) for cached price updates: `GET /securities/stream/prices?security_ids=&token=` streams the newest cached close on or before today every 5 seconds. Auth via query parameter (JWT token, required because `EventSource` doesn't support custom headers). The stream is a cache-only transport; its connection state is shown as `Stream connected`, while the quote date and stale age remain the freshness indicators.

- **nginx**: must forward SSE without buffering — `proxy_buffering off` and `X-Accel-Buffering: no` header. Because the compatibility JWT is query-borne, proxy access logs must use request method + `$uri` and omit the query string (never `$request`/`$args`); uvicorn access logging is disabled in the API container.
- **Frontend**: `usePriceStream` hook manages `EventSource` connection, returns `{status, lastEventAt}`. `SSEStatusIndicator` renders a green pulsing dot + `Stream connected` / yellow `Connecting…` / red `Connection error` / gray `Offline`; `lastEventAt` is message receipt time, not quote freshness.
- **Used on**: Investments page (header indicator), Security detail page (header indicator + backfill polling).
- **Fail closed on stream refresh or privacy change.** The hook waits for
  `restoreSession()` to settle. It schedules a reconnect only when restoration
  succeeds and the returned user is not in privacy mode; `null` or
  `privacy_mode=true` leaves status `disconnected` and does not rebuild an
  EventSource. A pending restoration is cancelled by hook cleanup, and callbacks
  from superseded EventSources are ignored. This prevents a 3-second
  refresh/reconnect loop after the refresh cookie is gone or privacy mode is
  enabled (the existing request wrapper clears/redirects on ordinary API 401s;
  SSE reconnects only the transport while the component remains mounted).

## Market-hours optimization

Background price refresh (`ensure_recent_price(force=True)` + `ensure_fundamentals`) on the positions endpoint runs only when `_markets_are_open()` is true: **weekdays 09:30–20:00 `America/New_York`**, read from the real tz database (`ZoneInfo`) rather than a fixed UTC offset so it tracks the DST change instead of drifting an hour either side of it.

The window is tighter than a plain "is it daytime" check on purpose. A forced refresh outside it cannot produce a bar dated *today* — weekends have no TSX/NYSE/NASDAQ session at all, and before 09:30 yfinance has not printed today's bar — so the call could only re-return the previous close, which is already cached. Because the positions fan-out runs once per positions request, that wasted call would otherwise repeat every request across the whole pre-open window.

Manual "Refresh prices" (`POST /portfolio/refresh`) is user-initiated and does not consult the gate, so it works anytime. Before the bar exists it simply finds nothing new, which is the honest answer: there is no newer data to fetch. It runs `queue_backfill` — the same 365-day `backfill_range` as `POST /securities/{id}/refresh` — so both refresh paths share one contract (docs/18).

## Scheduled price refresh (M187) — the day's bar keeps moving

The fan-out above is purely request-driven, and it deliberately stops at a close already dated `today` (§Manual price overrides: the once-per-day rule). The consequence: a held security's bar freezes at whatever the FIRST market-hours page load of the day happened to see — nobody loads positions until 15:00 and the day's close is a 15:00 snapshot. M187 adds the other half: an in-process worker (`services/price_refresh.py`) that ADVANCES today's close through the same seam at fixed wall-clock times of the trading session.

**The opt-in is the whole safety.** `ensure_recent_price` gained one explicit, default-False parameter:

```python
ensure_recent_price(session, security, today, *, force=False, probe_dead=False, intraday_update=False)
```

- With `intraday_update=False` (every request path: the positions fan-out, `compute_positions`, `security_overview`, `POST /portfolio/refresh`'s tasks) the behaviour is bit-identical to pre-M187 — a close dated today is never re-fetched, so a page load still costs at most one fetch per security per calendar day. `tests/test_m187_price_refresh.py` pins this on both request shapes plus `compute_positions` cache-only, with a positive control that only the opt-in reaches the seam.
- Only the scheduled worker passes `intraday_update=True`, always with `force=True` (freshness for a pre-today close) and `probe_dead=False`.
- The advance itself is `_cache_closes(..., intraday_update=False, today=None)`'s job: with the opt-in it may update the existing row for `today` **only when `source=PriceSource.yfinance`**; every other date keeps the insert-only rule and a `source=manual` row is never touched on any path (docs/02, models/prices.py: human data wins permanently — pinned by `test_a_manual_close_dated_today_is_never_touched_by_any_tick`, which asserts the fetch DID happen and the human row still won).

**The schedule is clock-based, not interval-based.** `PRICE_REFRESH_TIMES` (default `09:35,12:00,15:00,16:15`) is a comma-separated list of `HH:MM` wall-clock ticks in `America/New_York` — ≥4 per trading day, one just after the open (before 09:30 yfinance has not printed today's bar, so an earlier tick could only return the previous close) and one after the 16:00 close (the final bar). `price_refresh.tick_times()` parses it (a malformed entry is dropped with a warning; a schedule with NOTHING valid raises, which the loop logs and retries every 60 s — running no ticks at all must be loud). `price_refresh.next_tick(now, times, last_fired=…)` returns the next un-fired tick as an aware ET wall-clock datetime, skipping any candidate whose minute has fully elapsed (missed while down / already fired — `last_fired` makes a same-tick double-fire impossible) and any candidate `markets_are_open()` would reject (weekend, or a configured time outside the session — such a tick cannot produce a bar dated today, so the loop sleeps straight through instead of waking for a guaranteed no-op; a Saturday morning lands on Monday's first tick). `markets_are_open(now)` is the ONE implementation of the 09:30–20:00 ET window, shared with `api.portfolio._markets_are_open` (which delegates to it) — real tz database, DST-correct (docs/07 §Market-hours optimization).

**Worker shape.** Exactly the M59 template (`benchmark_refresh.py`): `run_once(session, *, now=None) -> PriceRefreshStats` / `worker_loop` / `start_worker` / `stop_worker`, the provider call under `asyncio.to_thread`, one session per tick (`_open_session`, SQLModel's `Session`, `expire_on_commit=False`), and per-security `try/except` — one symbol's outage logs a warning and the rest still get their attempt; the loop outlives any tick. The loop sleeps until `next_tick` (capped at 1 h per slice, re-aimed each wake, so drift is impossible and an overnight/weekend wait cannot park it on a stale target). Wired into `main.lifespan` alongside the other four workers; `PRICE_REFRESH_ENABLED` (default true, forced false in `tests/conftest.py`) makes `start_worker()` return `None`. `PriceRefreshStats`: `refreshed` (attempted), `updated` (the cached close actually moved), `failed`, `privacy_skipped`, `dead_skipped`, `skipped="markets_closed"`.

**Scope of the tick: held + watched, ONE selection.** `price_refresh.securities_to_refresh(db, user_id, include_dead=False)` — moved out of `api/portfolio.py` so the request fan-out (`GET /portfolio/positions`, `POST /portfolio/refresh`) and the worker share the exact same selection (committed-or-manual trades only, docs/03 staging model; watchlist rows; stable order by id; `dead_symbol` filtered unless the caller counts it). The worker sweeps every tenant's selection into a distinct-by-id set: a security held or watched by several users is refreshed once, keyed on its OWNER's row.

**Privacy is per-user here, unlike the FX/benchmark workers.** `fx_rates` and `benchmark_prices` are SHARED caches with no `user_id`, which is why those ticks skip entirely while ANY tenant has privacy mode on. `prices` carries `user_id` on every row, so this worker skips only the privacy-mode OWNER's securities (recorded in `stats.privacy_skipped`, symbols never requested — docs/11) and refreshes everyone else's. The gate keys on the security's OWNER, not on whoever holds it: a watchlist row naming a privacy tenant's security is still skipped, and a privacy tenant watching a public security does not block it (pinned in `test_a_privacy_mode_owners_symbols_are_never_requested`). Each context also gets `market_data.bind_privacy_lock(owner.privacy_mode)` bound before the call, so the seam's own `_guard_external_fetch` would refuse the fetch even if a future caller forgot the check.

**Dead symbols (M141) and sessions (M155).** `probe_dead` is never passed — `force`/`intraday_update` are freshness knobs, not gate bypasses — so a `dead_symbol` row is counted in `stats.dead_skipped` and never probed. `ensure_recent_price` releases the session before its provider wait internally (M161), so the sweep adds no second transaction-holding path; `tests/test_provider_wait_ledger.py` classifies `price_refresh.run_once` as `release@app/services/market_data.py::ensure_recent_price`.

**Non-goals.** docs/18's SSE streaming stays cache-only; the FX/benchmark/dividend workers are untouched; no new runtime dependency.

### Verification

`backend/tests/test_m187_price_refresh.py` (17), all against real Postgres with the yfinance seam stubbed at `fetch_latest_close`:

- the parsed default is ≥4 ticks, none before 09:30, the last ≥16:00 and inside the 20:00 window, each inside `markets_are_open`;
- `next_tick` walks 09:35 → 12:00 → 15:00 → 16:15 → next day, skips the weekend (Friday evening and Saturday morning both land on Monday 09:35), waits for tomorrow when startup is past the last tick, and never re-fires `last_fired`; a malformed tick is dropped, an empty schedule raises;
- a yfinance close dated today is ADVANCED by a later tick and again by a third (row moves 5.00 → 6.25 → 6.40, `source` stays `yfinance`, `stats.updated` reports it);
- a manual close dated today survives the tick with `source=manual` and its value intact even though the provider answered;
- weekend / pre-open / post-window ticks set `stats.skipped="markets_closed"` with zero recorded provider calls;
- a privacy-mode owner's symbol is never requested while the other tenant's row advances; a dead symbol is not probed;
- one symbol's provider failure lands in `stats.failed` while the other still advances;
- request-path regression: `ensure_recent_price` (plain and `force=True`) makes zero fetches for a close dated today, the positions fan-out calls it with `force=True` and NOTHING else (`seen == [{"force": True}]`), `GET /securities/{id}/overview` and `compute_positions` open no network call with a today row present.
