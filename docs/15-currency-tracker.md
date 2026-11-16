# 15 — Currency Tracker

Read-only board of CAD exchange rates against a curated set of major global
currencies, with a recent-trend sparkline per currency. No user configuration or
persistence: the board is derived entirely from the shared Bank of Canada
`fx_rates` cache (docs/07). See plan `local://currency-tracker-plan.md`.

## Context

Users want a quick glance at how CAD stacks up against major world currencies
without opening the Investments exposure view. The data already exists in the
BoC `fx_rates` cache (base=foreign, quote=CAD, rate = CAD per 1 foreign unit).
The tracker is a thin read-only projection of that cache over a fixed, curated
currency list — no new model, no migration, no writes.

Decisions (locked with user):
- **Curated static list.** Seventeen major currencies; CAD intentionally excluded
  (its rate would be 1.00). The list is a backend constant; it is not
  user-editable. Membership is a **rule**, not a taste: a base is listed only
  when Bank of Canada publishes a live `FX{base}CAD` daily series for it
  (docs/07 §Curated board membership). DKK was the 18th currency and was
  removed in M151 — BoC has published no DKK series since 2017 — so the board
  shows 17 rows and the refresh never calls a series that cannot answer.
- **Display convention: CAD per 1 foreign unit.** This matches BoC storage
  exactly, so no inversion is performed. The UI renders `1 USD = 1.37 CAD`.
- **Placement.** A new "Tools" sidebar group containing a single "Currencies"
  item, linking to `/currencies`.
- **Visibility is a per-user feature flag.** The Tools section + `/currencies`
  route are gated by the `currency_tracker_enabled` user preference
  (docs/02), toggleable in Settings; default ON. With it OFF the section is
  hidden from the sidebar and `/currencies` redirects to `/dashboard`.

## Approach

### Backend

`app/services/fx.py`
- `CURATED_FX_BASES: tuple[str, ...]` — the fixed list of base currencies
  (USD, EUR, GBP, JPY, CHF, CNY, AUD, NZD, HKD, SGD, INR, MXN, BRL, ZAR, KRW,
  NOK, SEK — 17 bases). Lives next to the other Valet constants, together
  with the M151 comment recording **why** DKK is not in it and what was
  probed to prove it (docs/07 §Curated board membership). Never add a base
  without live-checking `FX{base}CAD` first.
- `privacy_locked() -> bool` — public accessor for the existing `_privacy_lock`
  ContextVar (set per-request in `deps.get_current_user`). No new state.

`app/schemas/fx.py` — appended board shapes:
```python
class FxBoardPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    date: date
    rate: Decimal

class FxBoardItem(BaseModel):
    base: str
    latest_rate: Decimal | None
    as_of: date | None
    series: list[FxBoardPoint]

class FxBoardRead(BaseModel):
    currencies: list[FxBoardItem]
```
`rate` / `latest_rate` serialize as strings (Decimal → str), matching
`FxRateRead.rate`.

`app/api/fx.py` — new `GET /fx/board` (router prefix `/fx`, so the full path is
`/api/v1/fx/board`):
- Auth: `get_current_user` (tenant-scoped read; `fx_rates` is shared but the
  endpoint is still guarded).
- `days: int = Query(ge=1, le=365)` defaults to 90.
- For each base in `CURATED_FX_BASES`, calls `fx_service.get_rates_between(db,
  base, "CAD", start, today, allow_fetch=False)` — **cache-only**. This was
  `allow_fetch=not fx_service.privacy_locked()` when the board shipped; it was
  hardened to a plain cache read (the docs/12 "a page render never fetches"
  principle), and since M150 the cache is kept current by the scheduled refresh
  (docs/07 §Scheduled refresh) instead of by the reader. Rows come back
  ordered by date ascending.
- `latest_rate` / `as_of` come from the last row; `series` maps every row to an
  `FxBoardPoint`.
- **Graceful degrade:** a currency with no cached rate returns
  `latest_rate=None`, `as_of=None`, `series=[]`. The endpoint never raises 5xx
  and never fetches, so privacy mode has nothing to block here — per-currency
  misses become nulls (docs/07 discipline) and the M150 chip in the header
  names what is missing.
- No new model / no migration.

### Frontend

`frontend/src/api/fx.ts` — `FxBoard` / `FxBoardItem` / `FxBoardPoint`
interfaces + `getFxBoard(days = 90)` hitting `/fx/board?days=${days}`.

`frontend/src/pages/Currencies.tsx` (new) — mirrors `Investments.tsx` page
structure (`useQuery` + `Card` + loading/empty/error). Loading → `SkeletonRows`;
error → `text-danger` with `apiErrorMessage`; empty → `EmptyState` (Coins).
One `Card` per currency showing the display name, `1 {base} = {latest_rate}
CAD` (or `—` when null), the `as_of` date in muted text, and a `Sparkline` of
`series.map(s => Number(s.rate))`. Display names/symbols live only in a local
`CURRENCY_META` map (UI concern) — it mirrors the curated set (so no DKK
entry since M151) but is not load-bearing: a base it does not know falls back
to its raw code via `meta?.name ?? item.base`. Only semantic tokens are used.

`frontend/src/App.tsx` — route `{ path: "currencies", element: <Currencies /> }`
inside the authenticated `AppShell` children.

`frontend/src/components/layout/AppShell.tsx` — new `TOOLS_ITEMS` const +
"Tools" sidebar group (expanded) mirroring `NAV_ITEMS`, plus a collapsed-mode
`NavLink` to `/currencies` (Coins icon) mirroring the collapsed Accounts link.

## The FX staleness chip (M150)

The board is cache-only, which made "how old is this?" the one question the
page could not answer — while the same stale cache silently priced every USD
cash leg behind it (docs/07 §Scheduled refresh). The page header now carries
`components/ui/FxStalenessChip.tsx` (docs/09 §FX staleness chip):

- fed by `GET /fx/status`, which reports the book's age for the currencies the
  tenant actually holds (`trades.currency`, `accounts.currency`,
  `securities.listing_currency`) — not the 17 curated board rows, so a tenant
  with no foreign money gets **no chip at all**, and a tenant holding only a
  currency the board cannot price (DKK, docs/07 §Curated board membership)
  still gets `FX rates missing` rather than silence;
- `FX · <as_of> · <N>d` when current, a pulsing danger `FX rates stale · Nd`
  past the 7-day carry-forward window, and `FX rates missing` when a pair has
  no row at all;
- it renders nothing while loading and nothing on an error — a status chip is
  never worth breaking the page for.

## Touch points

- `backend/app/services/fx.py` — `CURATED_FX_BASES`, `privacy_locked()`.
- `backend/app/schemas/fx.py` — `FxBoardPoint`, `FxBoardItem`, `FxBoardRead`.
- `backend/app/api/fx.py` — `GET /fx/board`.
- `frontend/src/api/fx.ts` — `getFxBoard` + types.
- `frontend/src/pages/Currencies.tsx` — new page.
- `frontend/src/App.tsx` — route.
- `frontend/src/components/layout/AppShell.tsx` — "Tools" group.
- **M150** — `backend/app/services/fx_refresh.py` (the scheduled refresh),
  `backend/app/services/fx_status.py` + `GET /fx/status` (the read behind the
  chip), `frontend/src/components/ui/FxStalenessChip.tsx` (mounted in
  `Currencies.tsx`'s header) and `frontend/tests/m150FxStaleness.test.tsx`.
- **M151** — `CURATED_FX_BASES` loses `DKK` (docs/07 §Curated board
  membership), `CURRENCY_META` loses its DKK entry, and the pins live in
  `backend/tests/test_fx_board.py` + the M151 block at the bottom of
  `backend/tests/test_m150_fx_refresh.py` + an M151 case in
  `frontend/tests/m150FxStaleness.test.tsx`.

## Verification

- Backend: `tests/test_fx_board.py` seeds `FxRate(base="USD", quote="CAD", ...)
  boc_valet` and a EUR row, `GET /api/v1/fx/board` as demo user → USD
  `latest_rate == "1.37"`, non-empty `series`, EUR present, CAD absent. Privacy
  path: force `privacy_locked()` True with no cached rows → that base's
  `latest_rate is None` and `series == []` (no 403/5xx). Window: `days=30`
  returns series whose dates fall within the last 30 days.
- **M151**: `tests/test_fx_board.py::test_the_curated_set_is_exactly_the_live_valet_catalog_m151`
  pins `CURATED_FX_BASES` as a 17-element **literal** (so the constant and the
  assertion cannot be edited together) and `…_bo_cannot_price_m151` proves a
  cached/manual DKK row does not resurrect an 18th board row;
  `tests/test_m150_fx_refresh.py` pins that `pairs_behind` and `run_once`
  never name DKK on an empty cache, that a DKK-only tenant gets
  `FX rates missing` from `GET /fx/status`, and that `GET`/`POST /fx` still
  price DKK through a manual gap-fill.
- Frontend: `npx tsc --noEmit` + `npx eslint . --max-warnings 0` clean.
- End-to-end (running stack): open `/currencies`; expect a "Tools" group with
  "Currencies"; rows show `1 USD = 1.37 CAD` with sparklines. The board never
  fetches: rates arrive from the M150 scheduled refresh (or a manual
  `POST /fx`), so a freshly-seeded install shows `—` until the first tick
  lands — and the header chip says exactly that instead of leaving it silent.
- Regression: existing `GET /fx` and `POST /fx` tests still pass (board is
  additive).
