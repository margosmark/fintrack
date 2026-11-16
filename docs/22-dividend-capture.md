# 22 — Dividend Capture (extras)

Capture dividends/return-of-capital payments into the investment ledger using
yfinance dividend history as the source of truth, then let the user record them
(optionally as a DRIP) with one call. Complements the M14 analytics in
`docs/12` (which only *reads* the income trades this feature *writes*).

Two backend endpoints (`backend/app/api/trades.py`):

- `GET  /api/v1/trades/dividend-suggestions` — pending dividends from yfinance,
  scoped to currently-open positions, excluding dates already recorded.
- `POST /api/v1/trades/record-dividend` — atomically write 1–3 rows
  (income trade + optional DRIP buy + ledger transaction) and rebuild ACB.

The manual recording data model is the existing `trades` table (action enum
extended with the three cash-only income actions) plus a `transactions` row for
ledger visibility. M184 additionally persists raw provider events and refresh
state in `dividend_history` and `dividend_history_refresh` (docs/02 and docs/07)
so the suggestions read path is cache-first; no computed suggestion or total is
stored.

## Context

Users hold dividend-paying securities but the app never told them *which*
payments are still unrecorded, nor made recording a DRIP trivial. yfinance
already exposes a reliable per-symbol dividend history. We derive *pending*
suggestions by intersecting that history with the user's point-in-time share
balance (so a dividend paid while they held 0 shares, or already recorded,
never shows up), and provide a single atomic record call that also keeps ACB
pools correct after a DRIP buy.

Decisions (locked with user):
- **Income actions are cash-only trades.** `dividend | distribution |
  return_of_capital` carry `quantity = 0` and the gross cash in `price_native`
  (per `docs/02` convention). They are no-ops in the position running balance
  and are excluded from valuation, but feed `docs/12` analytics.
- **Point-in-time holdings.** Suggestions use the signed running balance of
  `trades.quantity` as of each dividend date (`trade_date <= div_date`), so the
  suggested `quantity` is exactly the shares held, not the current net.
- **DRIP is optional and atomic.** Recording with `drip=true` additionally
  writes a `drip` buy trade and still records the full gross cash transaction,
  so the cash leg is never lost if fractional math leaves a remainder.
- **Privacy mode blocks refresh, not cached suggestions.**
  `fetch_dividend_history` sits behind the single yfinance guard
  (`market_data._guard_external_fetch`), so the refresh worker sends no packet
  under `privacy_mode`; `GET /trades/dividend-suggestions` continues to read
  its already-cached raw events and reports unavailable state in its metadata.

## Approach

### Models

`backend/app/models/trades.py` — `Trade.action` is the `TradeAction` enum
(`docs/02`). The three income actions are:

```python
TradeAction.dividend
TradeAction.distribution
TradeAction.return_of_capital
```

Income trade invariants (enforced at write in `record_dividend`, mirrored by
`_normalize`): `quantity = 0`, `price_native = gross cash`, `commission = 0`.

`backend/app/models/transactions.py` — the ledger `Transaction` created by
recording carries `cad_amount` with **default `None`** (it is *not* computed by
this endpoint; see Discrepancies). `currency` is the account's currency;
`dedup_hash` is a sha256 of `account_id|date|gross(2dp)|raw_description`.

### Backend

Router prefix is `/trades` (so full paths are `/api/v1/trades/...`),
`backend/app/api/trades.py`.

#### `GET /api/v1/trades/dividend-suggestions`

```
auth: get_current_user
query: account_id: int | None  (Query; when set, restricts to that account)
response_model: list[DividendSuggestion]
```

Algorithm:
1. Load every user trade as `(account_id, security_id, date, quantity)`,
   optionally filtered by `account_id`, ordered by date.
2. Build a per-`(account, security)` timeline of signed deltas and the current
   net. Open position = `net > 0`. If none, return `[]`.
3. Load the securities for those open positions.
4. Build the `recorded` set of `(account_id, security_id, date.isoformat())`
   triples from existing income trades (`dividend | distribution |
   return_of_capital`) — these dates are skipped.
5. Read tenant-scoped raw provider events from `dividend_history`; enqueue a
   bounded refresh when due, but never wait for yfinance in this request. For
   each cached history entry `{date, amount}`:
   - skip if `(account, security, date)` already recorded;
   - compute `held` = cumulative signed balance where `trade_date <= div_date`
     (trades on the dividend date are included);
   - skip if `held <= 0`;
   - `per_share = Decimal(amount)`, `gross = per_share * held`;
   - append a `DividendSuggestion`.

No custom error codes; returns an empty list when there are no open positions
or no cached history. `X-Dividend-History-State` communicates pending,
unavailable, successful-empty, stale, (M186) hard-quote-miss and **never-answered (`missing`)**
source state, the provider (`source`, always `yfinance`), the age of the oldest
answer (`age_days`) — the same three questions the reports' `provenance` block
answers (docs/07 §Dividend-view provenance).

`DividendSuggestion` (`backend/app/schemas/trades.py`):

| field         | type   | meaning                                                  |
|---------------|--------|----------------------------------------------------------|
| `security_id` | int    | security the payment is for                              |
| `account_id`  | int    | account the shares were held in                          |
| `symbol`      | str    | security symbol                                          |
| `date`        | date   | ex-dividend date (yfinance `isoformat`)                  |
| `amount`      | Decimal| per-share dividend, in the security's `listing_currency` |
| `quantity`    | str    | shares held on that date, 2dp (`"100.00"`)               |
| `gross_amount`| str    | `amount × quantity`, pre-computed, 2dp                   |
| `currency`    | str    | `listing_currency` of the security                       |

#### `POST /api/v1/trades/record-dividend`

```
auth: get_current_user
status_code: 201
response_model: DividendRecordResult
body: DividendRecordInput
```

`DividendRecordInput` (`backend/app/schemas/trades.py`):

| field          | type              | default              | meaning                                                    |
|----------------|-------------------|----------------------|------------------------------------------------------------|
| `account_id`   | int               | — (required)         | target investment account                                 |
| `security_id`  | int               | — (required)         | paying security                                            |
| `date`         | date              | — (required)         | payment date                                               |
| `gross_amount` | Decimal           | — (required)         | total dividend in native currency                          |
| `action`       | TradeAction       | `dividend`           | `dividend \| distribution \| return_of_capital`             |
| `drip`         | bool              | `False`              | also write a DRIP buy trade                                |
| `drip_price`   | Decimal \| None   | `None`               | required if `drip=True`, must be `> 0`                     |
| `drip_commission` | Decimal        | `0`                  | commission on the DRIP buy                                 |
| `fractional`   | bool              | `True`               | `False` → floor DRIP qty to whole shares                   |
| `fx_rate_used` | Decimal \| None   | `None`               | optional explicit FX rate stored on the trade              |

`model_config = ConfigDict(extra="forbid")` — any unknown field is rejected
with **422** `validation_error`.

Validation / write order (all inside one `db.commit`):
1. `account = db.get(Account, account_id)`; `404 not_found` if missing or not
   owned by the user.
2. `_investment_account_or_422(account)` → **422 `account_not_investment`**
   unless `account.type in INVESTMENT_ACCOUNT_TYPES`.
3. `security = db.get(Security, security_id)`; `404 not_found` if missing or
   not owned.
4. If `drip and (drip_price is None or drip_price <= 0)` →
   **422 `invalid_drip_price`**.
5. Create rows:
   - **(1) Income trade** — `action = payload.action`, `quantity = 0`,
     `price_native = gross_amount`, `commission = 0`, `fx_rate_used`.
   - **(2) DRIP buy** (only if `drip`): `available = gross_amount -
     drip_commission`; `drip_quantity = available / drip_price` (floored to
     whole shares when `fractional=False`); a `Trade` with `action = drip`,
     `quantity = drip_quantity`, `price_native = drip_price`,
     `commission = drip_commission`, `fx_rate_used`.
   - **(3) Ledger transaction** — `raw_description = f"{action} {symbol}"`,
     `dedup_hash = sha256(f"{account_id}|{date}|{gross(2dp)}|{raw_desc}")`,
     `amount_native = gross_amount`, `currency = account.currency`,
     `normalized_payee = normalized_payee(raw_desc)`. `cad_amount` is **not**
     set here (stays `None`; see Discrepancies).
6. `rebuild_user_pools(db, user.id)` (reflects the DRIP buy in ACB cost base),
   then `db.commit()`.

Returns `DividendRecordResult`:

| field            | type      | meaning                                  |
|------------------|-----------|------------------------------------------|
| `trade_id`       | int       | id of the income trade (row 1)          |
| `drip_trade_id`  | int \| None | id of the DRIP buy trade, or `null`   |
| `transaction_id` | int       | id of the ledger transaction (row 3)    |

Error envelope codes raised by this endpoint:

| code                        | status | trigger                                                |
|-----------------------------|--------|--------------------------------------------------------|
| `not_found`                 | 404    | account or security missing / not owned by user        |
| `account_not_investment`    | 422    | target account is not an investment-capable type       |
| `invalid_drip_price`        | 422    | `drip=True` with missing/`<=0` `drip_price`            |
| `validation_error`          | 422    | body fails schema (e.g. `extra="forbid"` unknown field)|
| `privacy_mode_blocks_external_api` | 403 | only on the *suggestions* GET under `privacy_mode`  |

### Services

`backend/app/services/market_data.py`:
- `fetch_dividend_history(symbol: str) -> list[dict] | None` — yfinance
  `Ticker(symbol).dividends`; returns `[{date: "YYYY-MM-DD", amount: str}]`
  sorted **newest-first**, each `amount` per-share in the security's listing
  currency (quantized to 6dp). An empty dividend frame becomes `[]` only after
  `probe_quote` positively confirms a real listing; Yahoo uses the same empty
  frame for `Quote not found`, which returns `None`, preserves cached events
  and advances dead-symbol bookkeeping. Network/parse failures also return
  `None`. Calls `_guard_external_fetch()` first, so it raises
  `privacy_mode_blocks_external_api` (403) under privacy mode.

`backend/app/services/dividends.py`:
- `dividend_report(session, user_id, year=None)` — M14 aggregator over the
  income actions (`docs/12` §Dividends). Cache-only CAD conversion: rows whose
  `listing_currency → reporting_currency` rate is missing on the trade date are
  counted in `unresolved_fx` and excluded from all CAD sums. `year=None` picks
  the most recent year that has rows (else current year).

`backend/app/tax/acb.py`:
- `rebuild_user_pools(db, user_id)` — called after a recorded dividend/DRIP so
  ACB pools stay correct.

### Frontend

`frontend/src/api/trades.ts`:
```ts
interface DividendSuggestion {
  security_id: number; account_id: number; symbol: string; date: string;
  amount: string; quantity: string; gross_amount: string; currency: string;
}
interface DividendRecordInput {
  account_id: number; security_id: number; date: string; gross_amount: string;
  action?: "dividend" | "distribution" | "return_of_capital";
  drip?: boolean; drip_price?: string; drip_commission?: string;
  fractional?: boolean; fx_rate_used?: string | null;
}
interface DividendRecordResult {
  trade_id: number; drip_trade_id: number | null; transaction_id: number;
}
getDividendSuggestions(accountId?: number): Promise<DividendSuggestion[]>
recordDividend(input: DividendRecordInput): Promise<DividendRecordResult>
```

`frontend/src/pages/Investments.tsx`:
- `DividendSuggestionsSection` — `useQuery(["dividend-suggestions", accountId])`
  → `getDividendSuggestions(accountId)`; rendered only when there is at least
  one investment account (`investmentAccounts.length > 0`). Per-account column
  is hidden when called with a fixed `accountId`. Dismiss/dismiss-all persist a
  key set in `localStorage["fintrack:dismissed_dividends.<scope>"]` (payload
  key = `${account_id}-${security_id}-${date}`; `<scope>` is
  `tenantScope(user)` — `lib/tenantStorage.ts`, security follow-up F-6: the
  set holds row identifiers, so it is scoped per tenant), scoped per account.
  Above the notice it renders `DividendProvenanceStrip` over
  `provenanceFromHistory(response.history)` (docs/07 §Dividend-view
  provenance): one market-data badge — provider, the day the history last
  answered, its age, `(stale)`/`(missing)` — plus the existing prose counts,
  so the section states its source whether or not it has suggestions to show.
- `DividendSuggestionRow` — "Record" calls `recordDividend({ account_id,
  security_id, date, gross_amount, action: "dividend", drip, drip_price,
  drip_commission, fractional })`. On success invalidates
  `dividend-suggestions`, `trades`, `positions`, `exposure`; on error toasts.

`frontend/src/pages/SecurityDetail.tsx`:
- `RecordDividendCard({ securityId, symbol })` — account `<select>` of
  investment accounts, date (defaults today), gross amount (native), DRIP +
  fractional checkboxes, DRIP price + commission. `canRecord = accountId !== ""
  && grossAmount !== "" && Number(grossAmount) > 0`. Calls `recordDividend`
  with `action: "dividend"`; invalidates `security-overview`, `trades`,
  `positions`.

`frontend/src/components/reports/DividendsPanel.tsx`:
- Yearly income overview driven by `getDividendReport` (`/reports/dividends`,
  `docs/12`); the `year` selector passes `undefined` for "Latest" (server picks
  the most recent year with rows). Supports `?format=csv` download. This panel
  only *reads* the income trades that `record-dividend` writes.
- Renders `DividendProvenanceStrip` over the payload's `provenance` (docs/07
  §Dividend-view provenance) directly under the panel title — above the empty
  state too, because "nothing recorded" and "no source ever answered" are
  different facts and only the strip separates them.

## Touch points

- `backend/app/api/trades.py` — `GET /dividend-suggestions`,
  `POST /record-dividend`, `_investment_account_or_422`, `_normalize`.
- `backend/app/schemas/trades.py` — `DividendSuggestion`, `DividendRecordInput`
  (`extra="forbid"`), `DividendRecordResult`.
- `backend/app/models/trades.py` — `TradeAction` income members.
- `backend/app/models/transactions.py` — `Transaction.cad_amount` default `None`.
- `backend/app/services/market_data.py` — `fetch_dividend_history`,
  `_guard_external_fetch`.
- `backend/app/services/dividends.py` — `dividend_report` (M14, `docs/12`).
- `backend/app/tax/acb.py` — `rebuild_user_pools`.
- `frontend/src/api/trades.ts` — types + `getDividendSuggestions` /
  `recordDividend`.
- `frontend/src/pages/Investments.tsx` — `DividendSuggestionsSection`,
  `DividendSuggestionRow`.
- `frontend/src/pages/SecurityDetail.tsx` — `RecordDividendCard`.
- `frontend/src/components/reports/DividendsPanel.tsx` — report reader.

## Verification

**Suggestions — holdings math.**
- Seed: account A (investment), buy 100 AAPL on 2024-01-01; yfinance
  `fetch_dividend_history("AAPL")` returns `[{date:"2024-03-15", amount:"0.25"}]`
  (newest-first). `GET /api/v1/trades/dividend-suggestions` → exactly one
  `DividendSuggestion{ security_id, account_id: A, symbol:"AAPL",
  date:"2024-03-15", amount:"0.25", quantity:"100.00",
  gross_amount:"25.00", currency:<AAPL.listing_currency> }`.
- With `account_id=B` (no AAPL) → `[]`.
- After recording that suggestion (below), repeat the GET → the
  `2024-03-15`/AAPL/A triple is gone (in `recorded`).
- Point-in-time: sell 100 AAPL on 2024-02-01, buy 40 on 2024-03-10, dividend
  `2024-03-15` → `quantity` reflects the balance held on 03-15 (= 40), not 100.

**Record — no DRIP.**
- `POST /api/v1/trades/record-dividend` `{account_id:A, security_id, date,
  gross_amount:"25.00"}` → 201 `{trade_id, drip_trade_id:null,
  transaction_id}`. DB: one `Trade` with `action=dividend`, `quantity=0`,
  `price_native=25.00`; one `Transaction` with `amount_native=25.00`,
  `currency=account.currency`, `cad_amount IS NULL`.

**Record — DRIP.**
- Same body with `drip:true, drip_price:"1.00", drip_commission:"0"` and
  `gross_amount:"25.00"` → `drip_trade_id` is non-null; extra `Trade`
  `action=drip`, `quantity=25` (`25.00/1.00`, fractional), `price_native=1.00`.
  With `fractional:false` and `gross="25.00", drip_price="3.00"` →
  `drip_quantity = 8` (floored), cash remainder stays in the transaction.

**Errors.**
- `drip:true` without `drip_price` → 422 `invalid_drip_price`.
- `account_id` of a `checking` account → 422 `account_not_investment`.
- unknown `account_id` → 404 `not_found`.
- body with an unknown field (e.g. `"foo":1`) → 422 `validation_error`
  (`extra="forbid"`).

**Privacy.**
- With the requesting user's `privacy_mode=true`,
  `GET /api/v1/trades/dividend-suggestions` → 403
  `privacy_mode_blocks_external_api` (the yfinance guard is not caught).
  `POST /record-dividend` is manual and unaffected.

**Frontend.**
- Investments page shows the "Dividend Suggestions" card only when ≥1
  investment account exists; Record creates the rows and the card refreshes
  (suggestion disappears); Dismiss/Dismiss-all persist in localStorage per
  account. Security detail shows "Record Dividend — {symbol}" with account/
  date/gross/DRIP fields; after recording, the security overview and positions
  reflect the new income trade.

## Discrepancies from plan

- **Dividend ledger `cad_amount` is NOT set from cached fx.** The plan's
  contract text stated the created `Transaction` carries "cash transaction
  (`cad_amount` from cached fx)". The actual code
  (`record_dividend`, `backend/app/api/trades.py:347-358`) sets
  `amount_native`, `currency`, `raw_description`, `normalized_payee`, and
  `dedup_hash` only — `cad_amount` is left at its model default `None`
  (`backend/app/models/transactions.py:34`). Documented above as the real
  behavior; the plan wording is inaccurate, not the code.
- All other plan claims for `docs/22` were verified against current code:
  endpoint paths/methods, `DividendSuggestion`/`DividendRecordInput`/
  `DividendRecordResult` field names+types, `extra="forbid"`, the three
  raised error codes (`not_found`, `account_not_investment`,
  `invalid_drip_price`), `fetch_dividend_history` contract, and the frontend
  touch points.
