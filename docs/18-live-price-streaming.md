# 18 — Cached Price Streaming (SSE) + Market Hours

Cached price updates pushed to the Investments and Security Detail views over
Server-Sent Events (SSE), plus the market-hours gate that decides when the
backend proactively refreshes cached prices. Implemented in milestone **M21**.
No new model, no migration. See plan `local://doc-reconciliation-plan.md` §3.

## Context

Polling `GET /securities/{id}/overview` on a timer wastes requests and repeats
cached data. The app instead opens one long-lived SSE connection that emits a
JSON price array every 5 seconds for the securities the user is looking at. The
backend enforces a privacy lockdown (docs/11 step 6) and a rough market-hours
window (docs/07) so that off-hours and privacy-mode clients never trigger
external yfinance calls from the streaming or position endpoints.

Decisions (locked):
- **SSE, not WebSocket.** One-directional server→client push; `EventSource` is a
  lightweight built-in client. The application hook explicitly closes and
  rebuilds it after errors so refreshed tokens are used.
- **Token via query param.** `EventSource` cannot set the `Authorization`
  header, so the JWT is accepted in the `token` query param (mirrors the
  bearer header).
- **Cache-only prices on the stream.** Each tick reads the newest cached close
  on or before today from the `prices` table. Future-dated rows are excluded.
  It never
  triggers a fresh yfinance fetch — remediation M47 (docs/29 F5) restored this
  after the generator had drifted into live `fetch_latest_close` calls. The
  emitted payload includes `security_id`, `symbol`, `price`, `date`, `source`,
  `age_days`, and `stale`, so the client can update both the cached value and
  its freshness without treating message receipt time as a quote date. The
  same staleness rule as the overview is used: `age_days > 7` is stale.
- **Rough market-hours window.** 6 AM–8 PM ET covers pre/post market and
  timezone spread; the endpoint surface is intentionally coarse.

## Approach

### Backend — SSE price stream

`app/api/securities.py` — route `GET /stream/prices` (router prefix
`/securities`, mounted at `/api/v1` in `main.py`, so the full path is
`/api/v1/securities/stream/prices`):

```python
@router.get("/stream/prices")
async def stream_prices(
    security_ids: str = Query(..., description="Comma-separated security IDs"),
    current_user: Annotated[User, Depends(get_current_user_with_query_token)] = None,
):
    try:
        ids = [int(x.strip()) for x in security_ids.split(",") if x.strip()]
    except ValueError:
        raise api_error(
            422,
            "security_ids must contain integer IDs",
            "validation_error",
        ) from None
    return StreamingResponse(
        price_stream_generator(ids, current_user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
```

- **Query params**
  - `security_ids` — **required**, comma-separated list of integer security IDs
    (e.g. `1,2,3`). Empty string or an empty list is accepted and produces a
    stream with no events; any non-integer token returns `422 validation_error`
    from the route.
  - `token` — **optional** JWT. Mirrors the `Authorization: Bearer` header for
    SSE (see Auth below).
- **Auth:** `get_current_user_with_query_token` (`app/api/deps.py`) accepts the
  user from the bearer header *or* the `token` query param via
  `_decode_user_id(credentials, token)`. Missing/expired/invalid JWT →
  `401 invalid_token`. On success it binds the request-scoped privacy lock for
  both `fx` and `market_data` services.
- **Response:** `StreamingResponse`, `media_type="text/event-stream"`, headers
  `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`.
- **Event payload:** every ~5 s the generator emits

  ```
  data: [{
    "security_id": 1,
    "symbol": "AAPL",
    "price": "192.45",
    "date": "2026-09-21",
    "source": "yfinance",
    "age_days": 2,
    "stale": false
  }, ...]
  ```

  i.e. `f"data: {json.dumps(price_updates)}\n\n"`. `price` is the cached
  latest close serialized as a Decimal-as-string; the remaining fields identify
  the quote date and its freshness. Only securities the user actually owns
  (`Security.user_id == current_user.id`) are included; the fetch silently
  skips any others. Securities with **no** cached close are omitted from that
  tick's array (the array may be empty on a given tick).

### Privacy mode

`app/api/_price_stream.py::price_stream_generator` checks the request-scoped
`_privacy_lock` **first**:

```python
if _privacy_lock.get():
    logger.info("Closing price stream: privacy_mode blocks external fetches")
    return
```

When the user's `privacy_mode` is on, the generator returns immediately. **No
events are emitted and the stream closes with no data — no error code is
returned.** The client avoids opening the stream when the authenticated user
has privacy mode enabled, leaving the indicator Offline; it does not refresh the
session or reconnect in a loop. No yfinance call is ever made from the endpoint
(docs/11 step 6).

### Market-hours gate

`app/api/portfolio.py::def _markets_are_open() -> bool`:

```python
now = datetime.now(ZoneInfo("America/New_York"))
if now.weekday() >= 5:            # Saturday / Sunday — no session
    return False
minutes = now.hour * 60 + now.minute
return 9 * 60 + 30 <= minutes < 20 * 60
```

Weekdays 09:30–20:00 `America/New_York`, read from the real tz database so the
gate tracks DST instead of drifting an hour either side of it (the previous
`6 <= (now.hour - 4) % 24 < 20` estimate was pinned to a fixed offset). This
gate drives proactive background refresh:

> M187 added the OTHER half of this: an in-process worker that advances
> TODAY's closes on wall-clock session ticks (`09:35,12:00,15:00,16:15` ET)
> — the fan-out below still stops at a close dated today. The stream itself
> stays cache-only; the fetch schedule is docs/07 §Scheduled price refresh.

- `GET /api/v1/portfolio/positions` (optional `account_id` query param):
  computes positions via `valuation.compute_positions`, then **only when
  `_markets_are_open()`** enqueues `_run_ensure_recent_price(force=True)` +
  `_run_ensure_fundamentals` BackgroundTasks for every DISTINCT security held
  (`Trade.security_id`) or watched (`WatchlistItem.security_id`). Outside the
  window it skips the network calls entirely. The window is tighter than
  "daytime" because a forced refresh cannot produce a bar dated *today* before
  09:30 or on a weekend — it could only re-return the cached previous close,
  once per positions request (docs/07).
- `POST /api/v1/portfolio/refresh` → `202 {"queued": n}`: **always** enqueues a
  `queue_backfill` task (the 365-day `backfill_range`) plus the fundamentals
  task for held∪watched securities, regardless of market hours. All price tasks
  are queued ahead of the fundamentals ones, because positions render from
  prices and `Ticker.info` is the slow seam. This is the user-facing
  "Refresh prices" button and works anytime.

`_run_ensure_recent_price` / `_run_ensure_fundamentals` open their own Session
and call `market_data.ensure_recent_price` / `ensure_fundamentals`; failures
degrade silently (the security keeps its cached prices). Under privacy mode the
service-layer guard blocks the fetch and nothing is emitted.

**Why the button runs `backfill_range`, not `ensure_recent_price`.** The two
used to share one freshness window (`PRICE_STALE_DAYS = 7`) as a *fetch* gate,
so a close 1–7 days old was handed straight back and the whole fan-out was a
silent no-op — a week at a time — while `POST /securities/{id}/refresh` on the
same button-shaped action ran a real 365-day backfill. Same word, two
contracts, and the investments page got the losing one. The window now belongs
to the read paths only (`ensure_recent_price(force=False)`), the automatic
fan-out passes `force=True`, and the button runs the per-security refresh's
own `queue_backfill` — one `.history()` call either way, since a wider window
only widens the payload. Both paths stop at a close already dated `today`
(docs/07).

### Services

- `app/api/_price_stream.py::price_stream_generator(security_ids, user_id)`
  — `POLL_INTERVAL_SECONDS = 5`. Privacy lock → return (close). Otherwise loops
  forever: opens an owned `SQLModelSession`, loads owned securities, reads the
  newest cached close for each from the `prices` table, appends
  `{security_id, symbol, price, date, source, age_days, stale}`, and yields
  `data: <json>` when non-empty. Wraps the loop body in `except Exception`
  (logs a warning, never kills the stream), then
  `await asyncio.sleep(POLL_INTERVAL_SECONDS)`.
- `app/api/securities.py::stream_prices` — route handler; parses `security_ids`,
  returns the `StreamingResponse` (above).
- `app/api/portfolio.py` — `_markets_are_open`, `_run_ensure_recent_price(..., force)`,
  `_run_ensure_fundamentals`, `refresh_prices` (202 `{"queued": n}`; prices queued
  before fundamentals, via `securities.queue_backfill`), `get_positions`
  (market-hours background refresh, `force=True`).
- `app/services/market_data.py` — `_privacy_lock: ContextVar[bool]` (request-
  scoped, bound in `deps.get_current_user*`); `fetch_latest_close` (cached
  latest close, ≤ `LATEST_LOOKBACK_DAYS = 21`); `ensure_recent_price(..., force)`,
  which skips the `PRICE_STALE_DAYS` window only when forced and always stops
  at a close already dated `today`; `ensure_fundamentals` (all external fetches
  route through `_guard_external_fetch`, which raises under privacy — see
  docs/11).
- `app/api/deps.py::get_current_user_with_query_token` — bearer header *or*
  `token` query param; binds the privacy lock; raises `401 invalid_token`;
  **closes its DB session before returning** so a long-lived EventSource never
  pins a pooled connection `idle in transaction` for the stream's lifetime
  (FastAPI tears down yield-dependencies only when the response finishes —
  regression test: `test_query_token_auth_releases_db_session_before_streaming`).

### Frontend

- `hooks/usePriceStream.ts::usePriceStream(securityIds: number[])`:
  - Serializes the ids to a stable key `[...securityIds].sort().join(",")`
    (copies before sorting — `Array.sort` mutates in place). Empty key →
    status `disconnected`, no connection.
  - Opens `createPriceStream(ids)` in an `EventSource`; closes any prior
    connection first. `onopen` → `connected`; `onmessage` →
  - `JSON.parse(event.data)` as `PriceUpdate[]`, sets `lastEventAt = new Date()`,
    and for each update patches `["security-overview", update.security_id]`
    plus the shared `["watchlist"]` cache. The hook updates each cached price,
    quote date, source, age, and stale state from the event; it never derives
    freshness from `lastEventAt`. `lastEventAt` means message receipt time, not
    price freshness (M47: `PriceUpdate.price` is a string, used verbatim).
  - **Positions quote patch (M126).** The same update ALSO patches the cached
    positions rows for that `security_id` — `price`, `price_date`,
    `price_source`, `price_age_days`, `price_stale` only — through
    `setQueriesData({queryKey: ["positions"]})`, which matches every key in the
    family (`["positions"]`, `["positions", accountId]`), so ONE patch covers
    every surface that renders positions (docs/09 §Live-price display
    convention). The updater returns the SAME object when nothing moved, so a
    tick that did not change a held row re-renders nothing.
    Market value, book cost and every total are NEVER patched: they remain
    server-owned Decimal data, recomputed on the positions cadence — and the
    patch stamps `livePatchedAt` on the payload, which the next SERVER payload
    drops (a refetch replaces the object), so a table can say "totals update on
    next refresh" from the cache's own state instead of a timer.
  - **M130's portfolio header reads the same patch.** The card at the top of
    `/investments` re-evaluates its now-value and day Δ while `livePatchedAt`
    rides the payload, from the server's own primitives (`quantity`, the patched
    `price`, `previous_close_native`, `fx_rate_to_cad`) and labels both `live`
    (docs/09 §Portfolio header, §Live prices M130 carve-out). Cash is not
    priced, the pinned `day_change_as_of` is never re-derived, and the card's
    range sentence belongs to `GET /portfolio/value-history` — a response with
    no quote in it, which the stream therefore never touches.
  - **Invalidation runs AFTER the cache write, on a changed payload only.**
    React Query treats any cache write as a `success` state transition, which
    clears `isInvalidated` — patching after the invalidation would silently
    cancel the refetch the gate exists to schedule. So: signature compare →
    patch → invalidate (`["positions"]`, `["exposure"]`). The invalidation is
    still signature-gated (a repeated unchanged payload costs nothing), the
    first tick only records the baseline, and neither half recomputes money in
    the browser.
  - **History is fetched, not streamed.** `GET /prices/history`
    (`hooks/usePriceHistory.ts`) reads the cached `prices` rows for a window —
    one request per (security, range), never a refetch on stream tick. The
    stream moves the QUOTE; the chart moves only when the user asks it to.
  - **Changed-payload invalidation (M117).** The stream is a 5s cache poll that
    repeats an identical payload, so invalidating on every tick would turn a
    read-only transport into a positions refetch every 5s. The hook builds a
    `security_id:date:price` signature per tick and, only when it differs from
    the previous tick's signature, calls `invalidateQueries(["positions"])` and
    `["exposure"]` — a server re-read, never a client-side recompute. The first
    tick of a connection only records the baseline, so mounting a page does not
    trigger a refetch storm. The signature is reset whenever the effect re-runs
    (new id set / reconnect).
    `onerror` → `error` and the hook **closes** the EventSource, waits for
    `restoreSession()` to settle, then rebuilds from the current `tokens.access`
    after the backoff only when restoration succeeds; a failed restoration
    leaves the indicator `Offline` (M47, docs/29 F5b). The browser's built-in
    auto-reconnect would replay the originally captured
    URL, including a token that has since expired (15-min access tokens) →
    endless 401 loop. Status cycles error → connecting → connected.
  - Returns `{ status: StreamStatus, lastEventAt: Date | null }` where
    `StreamStatus = "connecting" | "connected" | "error" | "disconnected"`.
- `frontend/src/api/securities.ts::createPriceStream(securityIds: number[])`:
  - `token = tokens.access` (the in-memory token helper from `api/client.ts`;
    M47 corrected this line — the token is NOT in localStorage); URL =
    `/api/v1/securities/stream/prices?security_ids=<ids>&token=<encodeURIComponent(token)>`
    (falls back to no `token` segment when absent). Re-read at every rebuild so
    a refreshed token lands in the new URL.
- `components/ui/SSEStatusIndicator.tsx::SSEStatusIndicator({ status, lastEventAt, pricesStale })`:
  - Maps status → dot color + label:
    `connected` → `bg-gain` "Stream connected" (pulsing dot),
    `connecting` → `bg-warn` "Connecting…",
    `error` → `bg-danger` "Connection error",
    `disconnected` → `bg-muted` "Offline".
  - `pricesStale` (M65, m61 #7): true when any displayed row's price age is
    >24h (`price_age_days > 0` — day-granular dates, so a >24h age can never
    be dated today). The indicator then shows `bg-warn` "prices stale"
    instead of the connected label; `error` still wins so the red
    transport-failure state is never hidden.
  - The indicator describes the cache-only SSE connection; it does not claim
    that the displayed price is current. Freshness is shown by the cached
    price date and stale-age badge — and, when any row is older than 24h, by
    the indicator's own amber "prices stale" state (M65).
  - `title` shows `Last stream message: <time>` (or "No stream message").
- `pages/Investments.tsx`: header renders
  `<SSEStatusIndicator status lastEventAt pricesStale>` + `<RefreshPricesButton>`;
  `securityIds` derived from the user's held∪watched securities; `pricesStale`
  = any positions ∪ watchlist row with `price_age_days > 0`.
  - `RefreshPricesButton` (M117) invalidates `["positions"]`, `["exposure"]`
    and `["watchlist"]` on a **2s/6s/12s backoff** (`REFRESH_INVALIDATE_BACKOFF_MS`)
    instead of a single 2s tick. `POST /portfolio/refresh` answers `202` and the
    work runs in FastAPI `BackgroundTasks` *after* the response — one yfinance
    call per held/watched security, sequential — so a lone 2s invalidate reads
    the DB long before the last row lands. Timers are held in a ref and cleared
    on unmount. The SSE stream's changed-payload invalidation is the other half;
    the ladder covers the stream being unavailable (privacy mode, hidden tab).
- `pages/SecurityDetail.tsx`: `usePriceStream([id])`; passes `streamStatus` /
  `lastEventAt` into the header and price card; shows
  `· stream message <time>` when `status === "connected"`. The price card
  still renders the cached quote's `as of <date>` and stale-age badge from
  the overview payload. Also has a **backfill watch**: `refetchInterval`
  polls `getSecurityOverview` every 5 s for at most 3 minutes
  (`pollUntil = Date.now() + 3 * 60_000`) while `cached_days === 0`, so a
  just-added security's chart populates once the background fetch lands.
- **nginx:** must not buffer the SSE response. The backend already sets
  `X-Accel-Buffering: no`; the proxy config must honor it (no
  `proxy_buffering on` for this path) so events flush immediately.
- **Request logs:** the EventSource compatibility JWT is query-borne. Proxy
  access logs must record the request method and `$uri` without the query string
  (never `$request`/`$args`); uvicorn access logging is disabled in the API
  container, and the production web config is a deployment verification gate.

### Error envelope (this feature)

| code | HTTP | when |
|------|------|------|
| `invalid_token` | 401 | Missing/expired/invalid JWT (header or `token` query param) in `get_current_user_with_query_token`. |
| `validation_error` | 422 | `security_ids` is missing, or a non-empty token cannot be parsed as an integer. Empty `security_ids` is valid and yields a stream with no events. |

Privacy mode emits **no** error — the stream closes immediately with no events.

## Touch points

- `backend/app/api/_price_stream.py` — `price_stream_generator`, `POLL_INTERVAL_SECONDS`.
- `backend/app/api/securities.py` — `GET /stream/prices` (`stream_prices`).
- `backend/app/api/portfolio.py` — `_markets_are_open`, `_run_ensure_recent_price(..., force)`,
  `_run_ensure_fundamentals`, `refresh_prices`, `get_positions`.
- `backend/app/api/deps.py` — `get_current_user_with_query_token`.
- `backend/app/services/market_data.py` — `newest_price` (newest
  `Price.date <= today`, shared by the stream and valuation queries), plus
  `_privacy_lock`, `fetch_latest_close`, `ensure_recent_price`,
  `ensure_fundamentals`, `_guard_external_fetch`.
- `frontend/src/hooks/usePriceStream.ts` — `usePriceStream`, `StreamStatus`.
- `frontend/src/api/securities.ts` — `createPriceStream`, `PriceUpdate`.
- `frontend/src/components/ui/SSEStatusIndicator.tsx` — `SSEStatusIndicator`.
- `frontend/src/pages/Investments.tsx` — SSE header + refresh button.
- `frontend/src/pages/SecurityDetail.tsx` — SSE status + 3-min backfill watch.

## Verification

- **Endpoint existence** — confirmed in code:
  `grep -rn "stream/prices" backend/app/api` → `securities.py` `@router.get("/stream/prices")`.
  `grep -rn "portfolio/refresh\|def _markets_are_open" backend/app/api` →
  `portfolio.py` `refresh_prices` + `_markets_are_open`.
- **Auth path** — `grep -rn "get_current_user_with_query_token" backend/app/api/deps.py`
  confirms the bearer-or-query-token resolver and the `401 invalid_token` raise.
- **Privacy close** — `grep -rn "_privacy_lock.get()" backend/app/api/_price_stream.py`
  returns the early `return` (no events, no error).
- **Market-hours** — `_markets_are_open` reads `datetime.now(_ET)` with
  `_ET = ZoneInfo("America/New_York")`, returns `False` for `weekday() >= 5`
  and otherwise `9*60+30 <= minutes < 20*60`; `get_positions` calls
  `_run_ensure_recent_price(force=True)/_run_ensure_fundamentals` only inside
  `if _markets_are_open()`. Pinned by
  `tests/test_m117_price_refresh.py::test_markets_are_open_window` (8 clock
  cases) plus `test_markets_are_open_uses_the_real_et_clock` (DST).
- **Refresh contract** — `POST /portfolio/refresh` records a `backfill_range`
  per held∪watched security with a 365-day span, price tasks before
  fundamentals; `GET /portfolio/positions` records `force=True` on the
  background `ensure_recent_price` and `force=False` on the inline read path.
  Pinned by `tests/test_m117_price_refresh.py` and the updated W11 fan-out test
  (`tests/test_w11_securities.py::test_refresh_queues_held_union_watched`).
- **Error codes** — `grep -rn 'api_error(401, "Invalid or expired access token", "invalid_token")' backend/app/api`
  returns ≥1 hit (raised in both `get_current_user` and
  `get_current_user_with_query_token`). `validation_error` is the standard
  FastAPI 422 for missing `security_ids`; non-integer tokens use the same
  `validation_error` envelope from `stream_prices`.
- **Frontend** — `grep -rn "usePriceStream\|SSEStatusIndicator" frontend/src/pages`
  confirms Investments (header) and SecurityDetail (header + price card + 3-min
  backfill `pollUntil`) usages; `createPriceStream` builds the
  `?security_ids=&token=` URL from the in-memory `tokens.access` helper.
- **Running stack (smoke):** open `/investments` while markets are open → a
  green pulsing `Stream connected` dot, or the amber `prices stale` state when
  any row's price age is >24h (seeded manual quotes; `vitest
  tests/priceStaleStream.test.tsx` pins both branches); open a security detail → `· stream
  message <time>` appears while the cached price's `as of <date>` and stale-age
  badge remain independently accurate. With `privacy_mode` on, the stream closes
  immediately (no connected state, no yfinance calls). Outside weekdays
  09:30–20:00 ET, no yfinance fetches appear in backend logs from
  `/portfolio/positions`, but `Refresh prices` (`POST /portfolio/refresh`) still
  returns `202 {"queued": n}` and refreshes anytime.
- **Refresh actually refreshes (M117):** with a held security whose newest
  close predates today, click `Refresh prices` → within the 12s backoff tick
  `GET /portfolio/positions` reports `price_age_days = 0` for it, the SSE
  payload's `date` changes, and the amber `prices stale` label clears to
  `Stream connected` once every displayed row is dated today.
