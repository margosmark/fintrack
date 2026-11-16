# 35 — Notifications that fire + in-app inbox (M51)

Backend + small frontend. Turns the decorative notification surface (M31) into
settings types that are actually produced by a generator, and adds a tenancy-
strict, cursor-paginated in-app inbox that unions the three real sources.

Source: `NEXT_WAVE_PLAN.md` §Wave 2, M51.

## Context

`notification_settings` (M31, migration 0018) carries exactly five `alert_type`
values — `large_transaction`, `low_balance`, `dividend_received`,
`price_alert`, `monthly_summary`. **None of them has a generator and no code
reads `enabled`** except default-seeding in `api/users.py` and the Settings
toggle itself. Meanwhile three real generators already exist and are absent
from the enum:

| real generator | table / function | existing endpoint |
| --- | --- | --- |
| budget threshold | `spending_alerts` rows written by `api/spending_alerts.py::_check_alerts` | `GET /budgets/alerts`, `POST /budgets/alerts/{id}/acknowledge` |
| bill due | `bill_reminders` rows (derived due window) | `GET /bills` |
| tax loss | `app/services/tax_alerts.py::compute_tax_loss_alerts` (computed, no rows) | `GET /taxes/alerts` |

M51 added **no send path** (no SMTP, no webhook) — that statement was true
until M59: delivery now exists as a strictly opt-in transport behind
`notification_channels` + `delivery_enabled`, documented in `docs/37` (nothing
is sent by default, and privacy mode stays in-app only unless the user opts in
per channel).

### Plain statement of what is NOT wired (no hiding)

The five legacy types **still have no generator and no reader of `enabled`**
after this milestone. Toggling `large_transaction`, `low_balance`,
`dividend_received`, `price_alert` or `monthly_summary` changes nothing except
the stored `enabled` column; they produce no inbox items, no banner and no
badge, because nothing in the backend ever writes such an event. They remain
listed in `GET /notifications` as M31 left them (default **disabled**). Adding
generators for them is a future milestone, not part of M51.

## Decisions

1. **Three new settings types, generator-backed.** `budget_threshold`,
   `bill_due`, `tax_loss`. These are the only types the inbox surfaces.
2. **The three new types default to `enabled = true`; the five legacy types
   keep their `enabled = false` default.** A user who has never opened
   Settings should see the alerts that actually fire, and a "critical alert
   banner" that is off by default would be dead UI.
3. **The surface is gated, not the data.** `GET /notifications/inbox` always
   runs budget generation (`_check_alerts`) *before* it consults the toggles,
   so `spending_alerts` audit rows keep being written while `budget_threshold`
   is OFF. Toggling only changes what is returned. `bill_due` and `tax_loss`
   are pure reads of rows/computations that already exist, so their sources are
   skipped entirely when their toggle is off (nothing is lost — they write no
   rows).
4. **Read state is its own table.** New `notification_inbox_reads`
   (`user_id`, `item_key`, `read_at`), one row per read item. It is independent
   of `spending_alerts.acknowledged`, which continues to gate only the legacy
   `GET /budgets/alerts` response (that endpoint has no frontend consumer).
   Reading an inbox item does not acknowledge the alert and vice versa; docs/26
   records the split.
5. **Inbox items are a bounded union assembled per request.** Each source is
   naturally capped (one row per budget per day, one row per bill in its
   reminder window, one row per open taxable position), so the union is built
   in memory and sliced by a keyset cursor. No new materialised inbox table.
6. **Cursor pagination, house envelope.** Request `?limit=&cursor=`; response
   keeps the house `{items, total, limit, ...}` shape used by every paginated
   endpoint in docs/08 and replaces `offset` with `cursor`/`next_cursor`,
   because a numeric offset over a three-source union is unstable (the union
   is re-derived on every request). `limit` keeps the docs/08 contract
   (default 50, max 200, `422 validation_error` outside it).
7. **Email / webhook delivery was out of scope for M51** — no SMTP
   infrastructure and it conflicts with privacy mode. Not built, not stubbed
   *here*. **Delivered by M59 in `docs/37`** with that conflict resolved:
   delivery is off by default at both levels (`notification_channels.enabled`
   and `notification_settings.delivery_enabled`), the payload never carries
   `title`/`body`/`amount_cad`, and a `privacy_mode` user needs an explicit
   per-channel `privacy_opt_in_at` before anything leaves the machine.
8. **Demo stays read-only.** The inbox POSTs inherit the `get_current_user`
   demo guard, so `POST` is `403 demo_account_restricted` for the demo tenant
   and `GET` stays open. No exemption is added to `DEMO_MUTATION_EXEMPT_PATHS`
   (the guard lives in `app/api/deps.py`, where the standing exemption list is
   enumerated in docs/20: `POST /acb/rebuild`, `POST /auth/demo/reset` (M50),
   `POST /auth/mcp-tokens` + the `DELETE /auth/mcp-tokens/{id}` prefix (M81)).
   M51 added none of them; docs/20 also counts `POST /auth/demo`, the
   exemption that sits outside the dependency entirely).

## Settings types

`alert_type` is a plain `str(50)` column (docs/02) — widening the set is a
value change, not a migration.

| `alert_type` | default `enabled` | generator | inbox `kind` |
| --- | --- | --- | --- |
| `budget_threshold` | **true** | `api/spending_alerts.py::_check_alerts` → `spending_alerts` | `budget_threshold` |
| `bill_due` | **true** | none needed — derived from `bill_reminders` | `bill_due` |
| `tax_loss` | **true** | `services/tax_alerts.py::compute_tax_loss_alerts` (pure) | `tax_loss` |
| `large_transaction` | false | **none — no generator** | — |
| `low_balance` | false | **none — no generator** | — |
| `dividend_received` | false | **none — no generator** | — |
| `price_alert` | false | **none — no generator** | — |
| `monthly_summary` | false | **none — no generator** | — |

`GET /notifications` seeds any missing rows for the caller on every call (the
existing `_ensure_defaults` behaviour, now over all eight types), so an
existing user gets the three new rows on their first `GET /notifications` or
their first `GET /notifications/inbox`.

## Schema

New table, migration `0028_add_inbox_reads` (revises `0027_vault_envelopes`):

```sql
CREATE TABLE notification_inbox_reads (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    item_key    VARCHAR(120) NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    read_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_inbox_read_user_key UNIQUE (user_id, item_key)
);
CREATE INDEX ix_notification_inbox_reads_user_id ON notification_inbox_reads (user_id);
```

- `item_key` holds the inbox item `id` verbatim (format below). The unique
  constraint is what makes "mark read" idempotent.
- Model: `app/models/notifications.py::NotificationInboxRead`, exported from
  `app/models/__init__.py`.
- No other table changes. `notification_settings.alert_type` gains three
  allowed values by convention only.

## Inbox item id

Opaque to clients, stable across requests, URL-path safe (RFC 3986 `pchar`
includes `:`):

```
budget_threshold:<spending_alerts.id>      e.g.  budget_threshold:42
bill_due:<bill_reminders.id>               e.g.  bill_due:7
tax_loss:<securities.id>:<accounts.id>     e.g.  tax_loss:12:3
```

Format `<kind> ":" <rest>`; `rest` is digits for `budget_threshold`/`bill_due`
and `<digits>:<digits>` for `tax_loss`. `POST /notifications/inbox/{id}/read`
parses it to resolve the source row **for tenancy only**.

## Sources (exact derivation rules)

`today` = `app/services/txn_filters.today_et()` (America/Toronto), the same
"today" `_check_alerts` and docs/26 use.

### 1. `budget_threshold`

1. Run `app.api.spending_alerts._check_alerts(db, user_id)` — **unconditionally,
   before any toggle is read** (Decision 3). This is the generation step that
   keeps writing `spending_alerts` audit rows while the surface is hidden.
2. Take this user's `spending_alerts` rows with
   `created_at >= datetime.combine(today, time.min, tzinfo=America/Toronto)`.
   `_check_alerts`'s daily dedup writes at most one row per budget per ET day,
   so this is exactly "today's current budget status", ≤ one item per budget.

| field | value |
| --- | --- |
| `kind` | `budget_threshold` |
| `id` | `budget_threshold:{spending_alerts.id}` |
| `severity` | `spending_alerts.alert_type` (`critical` \| `warning`) |
| `occurred_at` | `spending_alerts.created_at` (ISO-8601 UTC) |
| `title` | `Budget alert: {budget.name}` |
| `body` | `Spent {spent} of {budget_amount} CAD ({actual_pct}% of limit; threshold {threshold_pct}%)` — the two percents are rendered **whole, ROUND_HALF_UP** (`notifications.py::_whole_pct`), i.e. `168.5` → `169`, so the body and the Budgets card's `Math.round(pct_used)` label read identically (M61 #3) |
| `link` | `/budgets` |
| `amount_cad` | `str(spent)` (Decimal-string) |

### 2. `bill_due`

This user's `bill_reminders` where `enabled = true`, `next_due_date` parses as
`YYYY-MM-DD`, and `next_due_date <= today + remind_days_before` days. Unparseable
or disabled rows are skipped (never an error — docs/25 pins `invalid_due_date`
at write time).

| field | value |
| --- | --- |
| `kind` | `bill_due` |
| `id` | `bill_due:{bill_reminders.id}` |
| `severity` | `critical` if `next_due_date <= today`, else `warning` |
| `occurred_at` | UTC midnight of `next_due_date - remind_days_before` days (when the reminder window opened) |
| `title` | `Bill overdue: {name}` if overdue, `Bill due today: {name}` if due today, else `Bill due soon: {name}` |
| `body` | `Due {next_due_date}` plus ` — {amount} CAD` when `amount` is not null |
| `link` | `/bills/{bill_reminders.id}` (M116 — the bill's own drill-down, docs/25 §M116 B2; was `/bills` before) |
| `amount_cad` | `str(amount)` or `null` |

### 3. `tax_loss`

`services/tax_alerts.compute_tax_loss_alerts(db, user_id, today)` — one item
per returned (security, taxable account) position.

| field | value |
| --- | --- |
| `kind` | `tax_loss` |
| `id` | `tax_loss:{security_id}:{account_id}` |
| `severity` | `warning` if `superficial_loss_risk`, else `info` |
| `occurred_at` | UTC midnight of `today` |
| `title` | `Tax-loss opportunity: {symbol}` |
| `body` | `Unrealized loss {unrealized_loss_cad} CAD in {account_name} (estimated tax benefit {estimated_tax_benefit_cad} CAD)` plus ` — superficial-loss risk` when the flag is set |
| `link` | `/taxes` |
| `amount_cad` | `str(unrealized_loss_cad)` |

### Gating

After generation, each source is included **only** when the caller's
`notification_settings.enabled` is true for its `alert_type`. A missing
settings row is treated as `enabled = true`. The five legacy types have no
source and therefore no `kind`.

## Ordering and cursor

- Sort key: `(occurred_at DESC, id DESC)` — total order, stable within a day.
- Cursor = unpadded base64url of the JSON object
  `{"v": 1, "at": "<occurred_at ISO-8601>", "k": "<item id>"}`.
- Decode rules — any failure raises
  `api_error(422, ..., "invalid_cursor")`: not valid base64url, not valid JSON,
  not a JSON object, `v != 1`, `at` not parseable by `datetime.fromisoformat`,
  or `k` not a string. **Never a bare 500.**
- The page is the first `limit` items strictly after the cursor position in
  sort order. `next_cursor` = cursor of the last returned item when more items
  remain, else `null`.
- Known limitation, stated not hidden: `tax_loss` items carry the current day
  as `occurred_at`, so a page boundary that falls across an ET midnight can
  shift their position. Cursors remain well-formed; the client simply
  refetches.

## API

All three live on the existing `notifications` router
(`app/api/notifications.py`, prefix `/api/v1/notifications`), bearer auth via
`get_current_user`.

### `GET /api/v1/notifications/inbox`

Query: `limit` (int, 1–200, default 50), `cursor` (string, optional).

```jsonc
// 200
{
  "items": [
    {
      "id": "budget_threshold:42",
      "kind": "budget_threshold",          // budget_threshold | bill_due | tax_loss
      "severity": "critical",               // critical | warning | info
      "title": "Budget alert: Dining",
      "body": "Spent 337.00 of 250.00 CAD (134.80% of limit; threshold 100.00%)",
      "link": "/budgets",
      "amount_cad": "337.00",               // Decimal-string | null
      "occurred_at": "2026-09-28T14:03:22.118043+00:00",
      "read": false,
      "read_at": null                        // ISO-8601 UTC | null
    }
  ],
  "total": 4,            // items in the whole gated inbox (ignores limit/cursor)
  "unread_count": 3,     // items with read=false across the whole gated inbox
  "limit": 50,
  "cursor": null,        // echo of the request cursor
  "next_cursor": null    // opaque cursor for the next page, else null
}
```

Errors: `401 invalid_token`, `422 validation_error` (`limit` out of range —
FastAPI's envelope), `422 invalid_cursor`.

`unread_count` and `total` are always computed over the full gated population,
not the returned page, so `?limit=1` is enough for a badge.

### `POST /api/v1/notifications/inbox/{item_id}/read`

Path: `item_id` = the inbox `id` above. No body.

```jsonc
// 200 — idempotent: a repeat call returns the original read_at
{ "id": "bill_due:7", "read": true, "read_at": "2026-09-28T15:00:00.000000+00:00" }
```

Resolution is **tenancy-only**, then an insert on `(user_id, item_key)`
(`ON CONFLICT DO NOTHING`):

| situation | result |
| --- | --- |
| unknown / malformed `item_id` | `404 not_found` |
| well-formed but owned by another user | `404 not_found` |
| owned by caller, kind currently toggled OFF | `200` — read state is data, it is recorded even for a hidden item (Decision 3) |
| demo tenant | `403 demo_account_restricted` |

Errors: `401 invalid_token`, `403 demo_account_restricted`, `404 not_found`.

### `POST /api/v1/notifications/inbox/read-all`

No body. Marks read every item currently in the **gated** inbox (items hidden
by an OFF toggle keep their existing state).

```jsonc
// 200
{ "marked": 3, "unread_count": 0 }
```

Errors: `401 invalid_token`, `403 demo_account_restricted`.

### Error envelope (all endpoints)

Standard `{detail, code}` via `app.core.errors.api_error` (docs/08):

| code | HTTP | when |
| --- | --- | --- |
| `invalid_token` | 401 | missing / malformed / expired token |
| `validation_error` | 422 | `limit` outside 1–200 (FastAPI `RequestValidationError` handler) |
| `invalid_cursor` | 422 | cursor fails any decode rule above |
| `not_found` | 404 | inbox `id` unknown, malformed, or not owned by the caller |
| `demo_account_restricted` | 403 | demo tenant on either POST |

## Tenancy

Every query filters `NotificationSetting.user_id`, `SpendingAlert.user_id`,
`BillReminder.user_id`, `NotificationInboxRead.user_id` (the union is built
from the caller's own rows only), and the read endpoints re-resolve the target
row against `current_user.id` before writing. `fx_rates`-style shared tables
are not involved.

## Frontend

1. **`frontend/src/api/notifications.ts`** — `NotificationInboxItem`,
   `NotificationInboxPage`, `getNotificationInbox(limit)`,
   `markNotificationRead(id)`, `markAllNotificationsRead()`.
2. **`AppShell.tsx` (badge)** — a Bell button in the sidebar footer with an
   unread-count pill (`unread_count`, hidden at 0, capped at `9+`), and a
   dropdown listing the current inbox with per-item "mark read" and a
   "Mark all read" action. Backed by the shared React Query key
   `["notifications","inbox"]` (`staleTime` 30s); no new route is wired
   (`App.tsx` is another card's file). Errors surface through the existing
   toast (`ApiError.code`), so the demo tenant shows
   `demo_account_restricted` on mark-read — consistent with every other write
   button in the app.
3. **Dashboard critical-alert banner** (`pages/Dashboard.tsx`) — rendered above
   the KPI strip when the inbox contains an **unread** `budget_threshold` item
   with `severity == "critical"`. Because the server already excludes
   `budget_threshold` when its toggle is OFF, the banner disappears with no
   client-side settings read: **toggle OFF → no banner and no inbox item**,
   toggle ON → both return. The banner carries a "Mark read" action.
4. **`components/settings/NotificationsSection.tsx`** — labels + descriptions
   for the three new types, and an explicit caption that the five legacy types
   have no generator yet (Decision: no hiding).

Docs touched: `docs/35` (this file), `docs/08` (Notifications rows + error
codes + pagination note), `docs/02` (new table + `alert_type` set),
`docs/24` (M31 type list, M41 banner line), `docs/26` (bell/read-state split).
`docs/09` is untouched (it has no notification/bell/banner content to
reconcile) and `docs/10` already points the whole M48–M52 wave at
`NEXT_WAVE_PLAN.md`.

## Out of scope

- ~~Email / webhook / push delivery~~ — **now delivered by M59, `docs/37`**
  (this card did not build it; the privacy conflict it cites is what docs/37
  resolves).
- Generators for the five legacy `alert_type` values.
- A dedicated `/inbox` page or route (`App.tsx` is owned by the M49 card).
- Changing `spending_alerts.acknowledged` semantics.

## Touch points

| file | change |
| --- | --- |
| `backend/app/models/notifications.py` | `NotificationInboxRead` model |
| `backend/app/models/__init__.py` | export |
| `backend/alembic/versions/0028_add_inbox_reads.py` | new migration |
| `backend/app/schemas/notifications.py` | inbox page / item / read schemas |
| `backend/app/api/notifications.py` | 8 default types, 3 new endpoints, union + cursor logic |
| `backend/app/api/users.py` | `_USER_OWNED_MODELS` += `NotificationInboxRead` — the new table FKs `users` with no `ON DELETE`, so account deletion would orphan/400 without it |
| `backend/tests/test_notifications.py` | extended coverage |
| `backend/tests/test_m29_delete_guards.py` | seed + assert `NotificationInboxRead` in the delete-account cascade |
| `frontend/src/api/notifications.ts` | inbox client |
| `frontend/src/components/layout/AppShell.tsx` | bell + badge + panel |
| `frontend/src/pages/Dashboard.tsx` | critical-alert banner |
| `frontend/src/components/settings/NotificationsSection.tsx` | labels |
| `docs/35`, `docs/08`, `docs/02`, `docs/24`, `docs/26` | specs |

Not touched (other cards own them): `App.tsx`, `auth.py`, `demo_seed.py`,
`docs/20`, `deps.py` demo exemption list.

## Verification

Backend (real Postgres, isolated DB):

1. **Settings seeding.** `GET /notifications` on a fresh user → 8 rows; the
   five legacy ones `enabled: false`, `budget_threshold` / `bill_due` /
   `tax_loss` `enabled: true`; a second call still returns 8.
2. **Toggle OFF hides the surface but not the data.** Seed a category + a
   `budgets` row (amount 200) + a manual transaction of −337 CAD in the
   current month in that category → `PATCH /notifications/{id}` set
   `budget_threshold` `enabled: false` → `GET /notifications/inbox` contains
   **0** items with `kind == "budget_threshold"` **and**
   `SELECT count(*) FROM spending_alerts WHERE user_id = <u>` **≥ 1** (the
   generation step still ran).
3. **Toggle ON restores it.** Same `PATCH` back to `true` →
   `GET /notifications/inbox` returns the `budget_threshold` item with the
   pinned shape (`id`, `severity`, `occurred_at`, `read: false`).
4. **Tenancy.** User A's `budget_threshold:<A-row-id>` id passed by user B to
   `POST /notifications/inbox/{id}/read` → `404` `{"code": "not_found"}`;
   `GET` by B never contains A's items.
5. **Cursor.** Seed > `limit` items, walk with `next_cursor` until `null`:
   union of pages == `total`, no duplicates; `?cursor=!!!not-base64!!!` →
   `422` `{"code": "invalid_cursor"}` (not 500); `?limit=0` → `422`
   `{"code": "validation_error"}`.
6. **Read + read-all.** `POST .../{id}/read` → `read: true`, `read_at` set;
   repeat → same `read_at` (idempotent). `POST .../read-all` →
   `{"marked": n, "unread_count": 0}` and `GET` shows every item `read: true`
   with `unread_count: 0`.
7. **Demo.** With `SEED_DEMO_ACCOUNT` on: `GET /notifications/inbox` → `200`;
   `POST /notifications/inbox/read-all` → `403`
   `{"code": "demo_account_restricted"}`.
8. **Bill source.** Create a bill due in 2 days with
   `remind_days_before: 3` → inbox has `severity: "warning"`; a bill with
   `next_due_date` yesterday → `severity: "critical"` and
   `title` starting `Bill overdue:`; `enabled: false` on the bill → no item.
9. **Gates.** `uv run ruff format --check && uv run ruff check`,
   `uv run pytest -q` (isolated DB), `npx tsc --noEmit`,
   `npx eslint . --max-warnings 0`, `NODE_ENV=test npx vitest run`,
   `npm run build`.

Frontend:

10. `GET /notifications/inbox` in a browser session → AppShell Bell shows the
    unread pill; Dashboard renders the critical banner for a critical budget
    item; flipping `budget_threshold` OFF in Settings → reload → no banner and
    the inbox panel shows no `budget_threshold` items; back ON → both return.
11. **Masked session** (docs/46 §D4, F-1): with the display mask ON the same
    banner renders the same line with the figures masked —
    `Budget alert: Groceries — Spent ••••• of ••••• CAD (209% of limit;
    threshold 100%)` — the sentence, the category name and the percentages
    readable and no dollar figure anywhere on `/dashboard`; the inbox panel
    masks its body the same way. Unmasked, both print the server prose
    verbatim (`tests/dashboardBudgetBannerMask.test.tsx`).
