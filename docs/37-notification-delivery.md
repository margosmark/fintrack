# 37 — Notification delivery channels: email + webhook (M59)

Outbound delivery for the M51 in-app inbox: two transports (SMTP email,
generic signed webhook) behind one interface, an auditable outbox with
retry/dead-letter, and a privacy-mode resolution that is pinned HERE, before
any code exists.

Source: `NEXT_WAVE_PLAN.md` §Wave 2 out-of-scope note ("email/webhook delivery
— future milestone"), promoted to M59. Doc number 37, not 36: `docs/36` is
reserved by `NEXT_WAVE_PLAN.md` for M52 (cash-flow calendar).

Companion specs: `docs/35` (inbox union, item ids, gating), `docs/08` (route
index + error codes), `docs/02` (tables), `docs/11` (egress inventory).

## Context

M51 made notifications real *in-app*: three generator-backed `alert_type`s
(`budget_threshold`, `bill_due`, `tax_loss`) produce a cursor-paginated inbox
union (docs/35). There is **no send path anywhere in `backend/app`** — no SMTP,
no webhook — and docs/35 decision 7 deferred it because outbound delivery
conflicts with privacy mode.

Two facts make the conflict concrete:

1. **Delivery leaks timing to a third party.** Every send reveals that *some*
   notification happened, at the moment it happened, to whoever operates the
   SMTP relay or the webhook URL.
2. **A naive payload leaks content.** The inbox item's `title`
   (`Bill overdue: Mapleview Rent`), `body` (`Spent 337.00 of 250.00 CAD …`),
   and `amount_cad` name budgets, bills, securities and exact dollar figures.

M59 resolves both, in this document, and only then builds the transports.

## Decisions

1. **The outbound payload is minimal and identical in SHAPE for every type.**
   Exactly five data fields leave the machine (below). The inbox `title`,
   `body` and `amount_cad` **never leave** — details stay in-app. The email
   subject is a byte-identical constant for every notification, so a mailbox
   observer learns only that a notification occurred, never which kind.
2. **Privacy mode = in-app only, by default, forever, unless the user opts in
   per channel.** A channel is sendable for a `privacy_mode=true` user only
   when that channel row carries `privacy_opt_in_at` — a timestamp written only
   by an explicit `PATCH` that sets `privacy_opt_in: true`. Enabling a channel
   while privacy mode is on *without* passing the opt-in in the same request is
   `409 privacy_opt_in_required`. There is no inherited path: a user who turned
   a channel on before enabling privacy mode has no `privacy_opt_in_at`, so the
   flip to privacy mode stops delivery with no action by us and no edit to
   `users.py` / `auth.py`.
3. **Delivery is opt-in at two levels, and never on by default.** A channel
   row seeds `enabled = false`; `notification_settings.delivery_enabled`
   seeds as a copy of `enabled` (legacy five `false`, generator three `true`).
   Nothing leaves the machine until a user turns a channel on, so the privacy
   default holds for every user and the SMTP/webhook config can be absent on a
   healthy install.
4. **One outbox, one worker, one code path.** Delivery never happens inline in
   a request. A scan enqueues `notification_deliveries` rows (one per inbox
   item per channel); an in-process worker dispatches them with retry/backoff
   and dead-letters them. The inbox is untouched by any of this: a failed
   delivery changes nothing in `spending_alerts`, `bill_reminders`, the union,
   or read state (acceptance: "failed delivery leaves inbox intact").
5. **Transports sit behind one interface.** `EmailTransport` (stdlib
   `smtplib`) and `WebhookTransport` (`httpx` + HMAC) implement the same
   `send()` contract and return the same outcome enum. SMTP credentials and
   the webhook signing master key exist **only in env/settings** — the database
   stores no secret (the export payload must stay secret-free, docs/11).
6. **Webhook authenticity = HMAC-SHA256 over `timestamp.body` with a
   per-user derived key.** `key = HMAC-SHA256(master, str(user_id))`, so no
   per-user secret is stored either; the receiver validates with the key it was
   given out of band. The timestamp rides in a header and is part of the signed
   bytes, so a captured body cannot be replayed under a fresh timestamp.
7. **The five legacy M31 types stay generator-less** (docs/35, "no hiding").
   They gain a `delivery_enabled` column like everything else, but there is no
   event for them to deliver, so the column is inert. Not pretended, not stubbed.
8. **Delivery follows the inbox gate, and the inbox gate wins.** `enabled=false`
   for a type stops *both* the inbox item and its delivery (acceptance), while
   `delivery_enabled` is an independent second switch: a type can be visible
   in-app and not delivered.
9. **`POST /notifications/deliveries/run` is caller-scoped**, not an operator
   hammer: it runs scan+dispatch for the authenticated user only. The worker
   runs it for everyone on a timer. Dead-letter *visibility* is
   `GET /notifications/deliveries` (pinned endpoint, below); the MCP server
   proxies REST routes, so it surfaces there too with no MCP change.
10. **Doc-number and migration collision — resolved.** `docs/36` is reserved
    for M52; this card uses `docs/37`. Migration
    `0030_notification_delivery` was written against `0028_add_inbox_reads`
    while sibling M56 was adding `0029_backup_artifacts` off the same parent
    (a two-head race, flagged as a hotspot). **M56 landed first, so this card
    re-parented `0030` onto `0029_backup_artifacts`** — the rule being "the
    second lander re-parents", never both leaving a fork.

## What is sent (exact payload contract)

Five data fields, nothing else, on both transports:

| field | value | never sent instead |
| --- | --- | --- |
| `alert_type` | `budget_threshold` \| `bill_due` \| `tax_loss` | — |
| `severity` | `critical` \| `warning` \| `info` (inbox item's) | — |
| `occurred_at` | inbox item's `occurred_at`, ISO-8601 UTC | — |
| `item_key` | inbox item `id` verbatim (`bill_due:7`, …) | — |
| `link` | inbox item's app-relative path (`/budgets`, `/bills/{id}`, `/taxes`) | — |

**Explicitly never transmitted on any transport:** `title`, `body`,
`amount_cad`, `read`/`read_at`, budget names, bill names, account names,
symbols, dollar figures of any kind, any token or credential, and any row from
`notification_settings.config`.

Per type, the payload differs only in the `alert_type`/`link` values (the
inbox's own mapping, docs/35):

| `alert_type` | `link` | email body line `Type:` |
| --- | --- | --- |
| `budget_threshold` | `/budgets` | `Type: budget_threshold` |
| `bill_due` | `/bills/{id}` (M116 — the due bill drills into its OWN detail page: matched payments, rolling average, period chart) | `Type: bill_due` |
| `tax_loss` | `/taxes` | `Type: tax_loss` |

`severity`, `occurred_at`, `item_key`, `link` come from the specific inbox item
and are **snapshotted onto the outbox row at enqueue**, so every retry of a
delivery re-sends the same bytes even if the underlying bill/budget has changed
in the meantime (and so dispatch never has to re-derive the union).

### Email (transport `email`)

Plain text only, UTF-8, no HTML part, no attachments.

```
Subject: fin_tracker: you have a new notification
```

The subject is a constant — identical bytes for all three types and all
severities.

```
You have a new notification in fin_tracker.

Type: {alert_type}
Severity: {severity}
Occurred: {occurred_at}

Open fin_tracker: {NOTIFY_APP_BASE_URL}{link}
```

- `From:` `NOTIFY_SMTP_FROM`; `To:` the channel row's `target`.
- No `Date`, `Message-ID`, `List-Unsubscribe` or tracking header is added by
  us — `smtplib` sends exactly the headers `EmailMessage` carries (Subject,
  From, To, MIME-Version, Content-Type).
- `NOTIFY_APP_BASE_URL` default `http://localhost:5173` (docs/09 dev origin).

### Webhook (transport `webhook`)

`POST {target}`, `Content-Type: application/json`, body:

```jsonc
{
  "v": 1,
  "event": "notification",
  "delivery_id": 42,                 // notification_deliveries.id
  "alert_type": "bill_due",
  "severity": "warning",
  "occurred_at": "2026-09-28T14:03:22+00:00",
  "item_key": "bill_due:7",
  "link": "/bills"
}
```

Headers:

| header | value |
| --- | --- |
| `X-FinTrack-Event` | `notification` |
| `X-FinTrack-Delivery` | `str(delivery_id)` |
| `X-FinTrack-Timestamp` | UNIX seconds at send time |
| `X-FinTrack-Signature` | `sha256=<hex HMAC-SHA256>` over `f"{timestamp}.{raw_body}"` |

Signing key: `HMAC_SHA256(NOTIFY_WEBHOOK_SECRET or JWT_SECRET,
str(user_id))` as raw bytes — per user, derived, never stored. Receiver
verification: recompute over the raw body with the same timestamp and compare
with `hmac.compare_digest`, then reject timestamps older than its own skew
window (we do not enforce skew server-side).

Responses: any `2xx` is success. `4xx` (except `408`, `429`) is **fatal** —
the receiver rejected the payload, retrying cannot fix it. `5xx`, `408`, `429`
and every transport error (DNS, refused, timeout) are **retryable**.

### Webhook target policy (SSRF)

Validated on write *and* at send time, reusing the address policy already in
`app/llm/egress.py` (`_resolve`, `_denied`, `_ALLOWED_SCHEMES`) with this
card's own error codes:

| condition | result |
| --- | --- |
| not `http`/`https`, or no hostname | `422 invalid_webhook_url` |
| hostname resolves to a denied address (RFC1918, link-local, CGNAT, multicast, unspecified) and `NOTIFY_WEBHOOK_ALLOW_PRIVATE=false` | `403 webhook_target_forbidden` |
| loopback (`127.0.0.1`, `::1`) | always allowed — the self-hosted sink case |
| unresolvable hostname | allowed at write time, fails at send time (retryable) |

The last row is the fix this review made. The check read
`if addrs and any(_denied(addr) ...)`, where falsy `addrs` means "DNS did not
answer" — so the `and` made an **unverifiable** host pass, and the send then
reached `httpx.post`, which performs its own second lookup the policy never
sees. A hostname public at validation and private at delivery reached an
internal address. `app/llm/egress.py` already had both halves (`at_send` fails
closed); this seam reused its primitives but not its rule, and now calls it
with `at_send=True`.

An unverifiable host gets its OWN code, `403 webhook_target_unverifiable`, not
`webhook_target_forbidden`, because the two must not retry alike: a denied
address is a permanent policy refusal (`fatal`), while an unresolvable hostname
is a transient DNS condition and stays **`retryable`** — the same outcome it
produced before the check, when it fell through to `httpx.post` and surfaced as
`webhook_unreachable`. Refusing the request must not permanently abandon a
notification over a brief DNS outage. Tests:
`backend/tests/test_webhook_ssrf_policy.py`.

Privacy mode does **not** further restrict the target: an opted-in privacy
user's webhook destination is their own explicit choice (Decision 2). The
policy above is the SSRF guard and applies to everyone.

## Schema

Migration `0030_notification_delivery` (revises `0029_backup_artifacts`).

```sql
-- 1. per-type delivery toggle (column added to the M31 table)
ALTER TABLE notification_settings
  ADD COLUMN delivery_enabled BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE notification_settings SET delivery_enabled = enabled;  -- seed as a copy

-- 2. transports
CREATE TABLE notification_channels (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    channel           VARCHAR(20) NOT NULL,          -- email | webhook
    enabled           BOOLEAN NOT NULL DEFAULT FALSE,
    target            VARCHAR(320),                  -- recipient address | webhook URL; NULL until set
    privacy_opt_in_at TIMESTAMP WITH TIME ZONE,      -- set ONLY by an explicit opt-in PATCH
    created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_notif_channel_user_kind UNIQUE (user_id, channel)
);
CREATE INDEX ix_notification_channels_user_id ON notification_channels (user_id);

-- 3. outbox
CREATE TABLE notification_deliveries (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    channel         VARCHAR(20) NOT NULL,
    alert_type      VARCHAR(50) NOT NULL,
    item_key        VARCHAR(120) NOT NULL,
    -- payload snapshot taken at enqueue, so a retry re-sends byte-identical
    -- content even if the source row has since changed:
    severity        VARCHAR(20) NOT NULL,
    occurred_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    link            VARCHAR(50) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_error      TEXT,
    last_error_code VARCHAR(50),
    sent_at         TIMESTAMP WITH TIME ZONE,
    dead_at         TIMESTAMP WITH TIME ZONE,
    retracted_at    TIMESTAMP WITH TIME ZONE,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_delivery_active
    ON notification_deliveries (user_id, channel, item_key)
    WHERE retracted_at IS NULL;
CREATE INDEX ix_notification_deliveries_user_id ON notification_deliveries (user_id);
CREATE INDEX ix_notification_deliveries_due
    ON notification_deliveries (next_attempt_at) WHERE status = 'pending';

-- 4. attempt log (one row per real send attempt)
CREATE TABLE notification_delivery_attempts (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),   -- denormalised: export scoping + delete cascade
    delivery_id  INTEGER NOT NULL REFERENCES notification_deliveries(id),
    attempt      INTEGER NOT NULL,                        -- 1-based send sequence
    outcome      VARCHAR(20) NOT NULL,                    -- sent | retryable | fatal
    error_code   VARCHAR(50),
    http_status  INTEGER,
    detail       TEXT,                                    -- sanitized, <=500 chars, never a payload
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX ix_notif_delivery_attempts_delivery_id ON notification_delivery_attempts (delivery_id);
CREATE INDEX ix_notif_delivery_attempts_user_id ON notification_delivery_attempts (user_id);
```

Models: `app/models/notifications.py::NotificationChannel`,
`::NotificationDelivery`, `::NotificationDeliveryAttempt`, plus
`NotificationSetting.delivery_enabled`; all exported from
`app/models/__init__.py` (the export registry is metadata-driven, so they
appear in `GET /export/full` automatically and are caller-scoped by
`user_id` — docs/11 step 2 keeps holding).

Statuses (plain `str(20)`, no enum — same convention as `alert_type`):

| status | meaning | terminal |
| --- | --- | --- |
| `pending` | queued, `next_attempt_at` decides when | no |
| `sent` | a transport reported success | yes |
| `dead` | attempts exhausted or a fatal error; retryable by a human | yes |
| `cancelled` | eligibility lost (channel/type turned off, privacy flipped) or the inbox item vanished | yes |

`retracted_at` is orthogonal to `status`: it marks "this inbox item is no
longer in the union", which both blocks a re-send and lets a later reappearance
create a fresh row (the partial UNIQUE is what makes that possible).

### Deduplication and re-arm

One active row per `(user_id, channel, item_key)` — enforced by
`uq_delivery_active`. On every scan, active rows whose `item_key` is absent
from the user's current gated union are retracted (`retracted_at = now()`); a
`pending` row retracted that way is also `cancelled`, so a delivery is never
sent for an item that has already left the inbox. Consequences, stated rather
than hidden:

- `budget_threshold` items carry a fresh `spending_alerts.id` each day → one
  delivery per alert row.
- `bill_due` has a stable id for as long as the bill sits in its reminder
  window → delivered once per window; changing the due date changes
  `occurred_at` but not `item_key`, so it does **not** re-deliver until the
  item leaves and re-enters the union.
- `tax_loss` has a stable `(security, account)` id while the position stays
  open → delivered once per open position, not once per day. It re-arms only
  after the position stops qualifying and later qualifies again.

## Eligibility (the one predicate)

A pending row for user `u`, channel row `c`, type `t` may be sent iff:

```
c.enabled
AND (NOT u.privacy_mode OR c.privacy_opt_in_at IS NOT NULL)
AND settings(u, t).enabled            -- missing row => true  (docs/35 rule)
AND settings(u, t).delivery_enabled   -- missing row => false (delivery is opt-in)
AND the inbox union still contains the row's item_key
AND target is set and transport-configured
```

Checked twice: at enqueue (nothing is queued for an ineligible combination) and
again at dispatch (privacy mode can flip between the two). A dispatch-time
failure of the predicate writes **no attempt row** — no send was attempted —
and sets `status='cancelled'`, `last_error_code='delivery_not_eligible'`.

Explicit user actions cancel eagerly in the same transaction, so the queue
reflects the UI immediately: disabling a channel cancels that channel's
pending rows; setting `delivery_enabled=false` (or `enabled=false`) for a type
cancels that type's pending rows across channels. Cancellation is terminal —
re-enabling does not resurrect the dropped rows; the inbox still holds the
items.

## Retry, backoff, dead-letter

- First attempt: `next_attempt_at = created_at`, so the worker's next tick
  sends it (the manual `run` endpoint sends it immediately).
- `_MAX_ATTEMPTS = 5`. After a **retryable** outcome the row stays `pending`
  with `next_attempt_at = now + _BACKOFF_SECONDS[attempts - 1]`, where
  `_BACKOFF_SECONDS = (30, 60, 120, 240)` → 30s, 60s, 120s, 240s. The 5th
  failed attempt sets `status='dead'`, `dead_at=now()`.
- A **fatal** outcome sets `status='dead'` on the first attempt (attempts=1).
- Every real attempt appends one `notification_delivery_attempts` row with
  `outcome ∈ {sent, retryable, fatal}`, `http_status`, `error_code`, and a
  `detail` that is `safe_target()`-sanitized (no userinfo, no query string) and
  truncated to 500 chars. The payload is never logged.
- `POST /notifications/deliveries/{id}/retry` re-arms a `dead` row:
  `status='pending'`, `attempts=0`, `next_attempt_at=now()`, `dead_at=null`,
  `last_error*=null`. History is preserved — existing attempt rows stay.

Per-transport outcome classification:

| transport | success | retryable | fatal |
| --- | --- | --- | --- |
| email | `sendmail` returns without raising | `SMTPResponseException` 4xx, `SMTPServerDisconnected`, `OSError`, `TimeoutError`, any other `SMTPException` | `SMTPResponseException` 5xx, `SMTPRecipientsRefused`, not-configured |
| webhook | `2xx` | `408`, `429`, `5xx`, connect/DNS/timeout errors | every other `4xx`, malformed URL at send time |

`error_code` values written on the delivery row: `smtp_unavailable`,
`smtp_error`, `smtp_recipients_refused`, `smtp_not_configured`,
`webhook_http_error`, `webhook_unreachable`, `delivery_not_eligible`.

## The worker

`NOTIFY_WORKER_ENABLED` (default **true**) starts one asyncio task in
`lifespan`; `conftest.py` sets it to `false` so the suite drives `run_once`
explicitly. Each tick: `await asyncio.to_thread(run_once, ...)` — an
`httpx`/SMTP call must not block the event loop — then sleeps
`NOTIFY_WORKER_INTERVAL_SECONDS` (default 60). Exceptions are logged and
swallowed; the loop never dies, never retries faster than its interval, and is
cancelled on shutdown. The tick opens its own `Session` from
`deps.get_engine()` (the `_price_stream` pattern), never a request session.

`run_once(db, *, user_id=None) -> dict` is the single entry point (worker,
`POST /notifications/deliveries/run`, and every test call it):

1. **Scan** — for each user with at least one channel (`user_id` when given):
   `app.api.notifications._collect(db, user_id)` (the exact gated union the
   inbox serves, docs/35 — this also runs `_check_alerts`, so budget audit rows
   keep being written), retract vanished keys, enqueue missing eligible ones.
2. **Dispatch** — up to 50 rows with `status='pending'`, `retracted_at IS NULL`
   and `next_attempt_at <= now()`, ordered by `next_attempt_at`.

Returns `{"scanned_users", "enqueued", "retracted", "attempted", "sent",
"retrying", "dead", "cancelled"}`.

## API

All on the existing `notifications` router (prefix `/api/v1/notifications`),
bearer auth via `get_current_user`, so **every write is `403
demo_account_restricted` for the demo tenant** and reads stay open. Routes are
declared before the pre-existing `PATCH /{setting_id}` so no path shadowing
occurs.

### `GET /api/v1/notifications/channels`

Auto-seeds both rows for the caller on first call (`enabled=false`,
`email.target = users.email`, `webhook.target = null`) — the same idempotent
`_ensure_defaults` idea docs/35 pins for settings.

```jsonc
// 200 — bare list, like GET /notifications (not paginated: exactly 2 rows)
[
  {
    "id": 3, "user_id": 7, "channel": "email",
    "enabled": false, "target": "me@example.com",
    "privacy_opt_in": false, "configured": true,
    "created_at": "…", "updated_at": "…"
  },
  {
    "id": 4, "user_id": 7, "channel": "webhook",
    "enabled": false, "target": null,
    "privacy_opt_in": false, "configured": false,
    "created_at": "…", "updated_at": "…"
  }
]
```

`configured` = would enabling be accepted right now (email: `NOTIFY_SMTP_HOST`
and `NOTIFY_SMTP_FROM` set and `target` present; webhook: `target` present).
`privacy_opt_in` = `privacy_opt_in_at IS NOT NULL`.

Errors: `401 invalid_token`.

### `PATCH /api/v1/notifications/channels/{channel_id}`

Body: `{enabled?, target?, privacy_opt_in?}` (all optional, `extra="forbid"`).
`target` for `email` must be a valid address (`EmailStr`), for `webhook` a
URL passing the policy above. `privacy_opt_in: false` clears the opt-in.

```jsonc
// 200 — the updated row, same shape as above
```

| situation | result |
| --- | --- |
| unknown / foreign `channel_id` | `404 not_found` |
| `enabled: true`, channel `email`, no `NOTIFY_SMTP_HOST`/`NOTIFY_SMTP_FROM` or no `target` | `409 channel_not_configured` |
| `enabled: true`, channel `webhook`, `target` null | `409 channel_not_configured` |
| `enabled: true` **in the request** while `privacy_mode=true` and neither the payload nor the stored row carries the opt-in | `409 privacy_opt_in_required` |
| webhook `target` malformed | `422 invalid_webhook_url` |
| webhook `target` resolves to a denied address | `403 webhook_target_forbidden` |
| `target` not a valid email address (channel `email`) | `422 validation_error` |
| `enabled: false` | cancels that channel's pending rows (acceptance: toggle off stops delivery) |
| demo tenant | `403 demo_account_restricted` |

Errors: `401 invalid_token`, `403 demo_account_restricted`,
`403 webhook_target_forbidden`, `404 not_found`, `409 channel_not_configured`,
`409 privacy_opt_in_required`, `422 validation_error`,
`422 invalid_webhook_url`.

### `PATCH /api/v1/notifications/{setting_id}` (extended)

Body gains `delivery_enabled: bool | None` alongside `enabled`/`config`:

```jsonc
{ "enabled": true, "delivery_enabled": false }
```

`delivery_enabled: false` (or `enabled: false`) cancels that type's pending
rows across channels. Unknown/foreign id → `404 not_found`.

### `GET /api/v1/notifications/deliveries`

Query: `limit` (1–200, default 50), `offset` (0–1,000,000, default 0),
`status` (optional, one of `pending|sent|dead|cancelled`). Past the offset
ceiling → `422 validation_error`: this route declares its own query params, so
it repeats the shared `deps.MAX_OFFSET` bound itself (docs/08). Offset
pagination is correct here (unlike docs/35's union) because the outbox is a
real table.

```jsonc
// 200
{
  "items": [
    {
      "id": 9, "channel": "webhook", "alert_type": "bill_due",
      "item_key": "bill_due:7", "status": "sent", "attempts": 1,
      "next_attempt_at": "2026-09-28T14:05:02.118043+00:00",
      "last_error": null, "last_error_code": null,
      "sent_at": "2026-09-28T14:05:02.400000+00:00",
      "dead_at": null, "retracted": false,
      "created_at": "…", "updated_at": "…",
      "attempt_log": [
        { "attempt": 1, "outcome": "sent", "error_code": null,
          "http_status": 200, "detail": null, "created_at": "…" }
      ]
    }
  ],
  "total": 7, "limit": 50, "offset": 0
}
```

`attempt_log` is that delivery's rows from
`notification_delivery_attempts` in `attempt` order (empty for a row that has
never been dispatched). This is the auditable dead-letter surface.

Errors: `401 invalid_token`, `422 validation_error`
(`limit`/`offset`/`status` out of range).

### `POST /api/v1/notifications/deliveries/{delivery_id}/retry`

No body. Re-arms a `dead` row (shape above).

```jsonc
// 200 — the delivery with status "pending", attempts 0, empty attempt_log
```

| situation | result |
| --- | --- |
| unknown / foreign id | `404 not_found` |
| status is `pending`, `sent` or `cancelled` | `409 delivery_not_retryable` |
| demo tenant | `403 demo_account_restricted` |

Errors: `401 invalid_token`, `403 demo_account_restricted`, `404 not_found`,
`409 delivery_not_retryable`.

### `POST /api/v1/notifications/deliveries/run`

No body. Runs `run_once(db, user_id=current_user.id)` synchronously — scan,
enqueue, dispatch — for the caller only.

```jsonc
// 200
{ "scanned_users": 1, "enqueued": 2, "retracted": 0,
  "attempted": 2, "sent": 1, "retrying": 1, "dead": 0, "cancelled": 0 }
```

`retrying` counts retryable outcomes (rows left `pending` with a bumped
`attempts`); `dead` counts rows that just transitioned to `dead`.

Errors: `401 invalid_token`, `403 demo_account_restricted`.

### Error envelope (all endpoints)

Standard `{detail, code}` via `app.core.errors.api_error` (docs/08):

| code | HTTP | when |
| --- | --- | --- |
| `invalid_token` | 401 | missing / malformed / expired token |
| `demo_account_restricted` | 403 | demo tenant on any PATCH/POST |
| `not_found` | 404 | unknown or foreign channel/delivery id |
| `channel_not_configured` | 409 | enabling a channel whose transport is not configured |
| `privacy_opt_in_required` | 409 | enabling a channel under privacy mode without the opt-in |
| `delivery_not_retryable` | 409 | retry on a row that is not `dead` |
| `validation_error` | 422 | FastAPI/pydantic validation: `limit`, `offset`, `status`, email `target` |
| `invalid_webhook_url` | 422 | webhook target is not `http(s)://host` |
| `webhook_target_forbidden` | 403 | webhook target fails the address policy |
| `webhook_target_unverifiable` | 403 | webhook target did not resolve at send time (retryable) |

## Env / settings (config.py + .env.example)

| key | default | meaning |
| --- | --- | --- |
| `NOTIFY_SMTP_HOST` | `""` | SMTP relay; empty ⇒ email channel cannot be enabled |
| `NOTIFY_SMTP_PORT` | `587` | |
| `NOTIFY_SMTP_USER` | `""` | optional auth |
| `NOTIFY_SMTP_PASSWORD` | `""` | env only — never written to the DB |
| `NOTIFY_SMTP_STARTTLS` | `true` | |
| `NOTIFY_SMTP_FROM` | `""` | envelope sender; required alongside the host |
| `NOTIFY_WEBHOOK_SECRET` | `""` | HMAC master key; empty ⇒ falls back to `JWT_SECRET` (rotation implication: rotating `JWT_SECRET` invalidates issued webhook keys) |
| `NOTIFY_APP_BASE_URL` | `http://localhost:5173` | base for the email link |
| `NOTIFY_WEBHOOK_ALLOW_PRIVATE` | `false` | operator opt-in for RFC1918 webhook targets |
| `NOTIFY_WORKER_ENABLED` | `true` | start the in-process worker |
| `NOTIFY_WORKER_INTERVAL_SECONDS` | `60` | tick period |

Code constants (single source, not env): `_MAX_ATTEMPTS = 5`,
`_BACKOFF_SECONDS = (30, 60, 120, 240)`, `_DISPATCH_BATCH = 50`,
`_HTTP_TIMEOUT_SECONDS = 10.0`.

## Tenancy

`user_id` on all three new tables and on every query; channel/delivery ids are
re-resolved against `current_user.id` before any read or write (foreign id ⇒
`404 not_found`, never a 403 that confirms existence). The worker iterates
users from `notification_channels`, so a tenant with no channel is never
touched. `notification_delivery_attempts.user_id` is denormalised so
`GET /export/full` scopes it (docs/11) and `delete_user_rows` can cascade it —
it is appended to `_USER_OWNED_MODELS` *before* `NotificationDelivery`.

## Reconciliation (same commit as the code)

| doc | change |
| --- | --- |
| `docs/37` (this file) | written BEFORE the implementation |
| `docs/08` | Notifications section: `delivery_enabled`, the four new routes, the `channels`/`deliveries` payload shapes, and the nine new error codes in the master list |
| `docs/35` | decision 7 + "Out of scope" now point here instead of claiming no send path exists; the "no send path anywhere" context line is marked as pre-M59 |
| `docs/02` | `notification_settings.delivery_enabled`; `notification_channels`, `notification_deliveries`, `notification_delivery_attempts` tables + their indexes/uniques; `notification_settings` tenancy note |
| `docs/01` | the external-network seam list gains `services/notify_delivery.py` |
| `docs/11` | egress inventory gains the SMTP relay and webhook destinations, with the privacy-mode rule |
| `docs/10` | M59 milestone line |
| `docs/24` | feature index line for the delivery routes |
| `.env.example` | the eleven keys above |
| `NEXT_WAVE_PLAN.md` | the Wave 2 "future milestone" note now names M59/docs/37 |
| `AGENTS.md` §2 | **NOT edited by this card** — amending AGENTS.md is consent-gated; the pending one-line amendment ("external network only in `fx.py`, `market_data.py` and `notify_delivery.py`") is listed in the task report for a human to apply |

## Touch points

| file | change |
| --- | --- |
| `backend/alembic/versions/0030_notification_delivery.py` | migration |
| `backend/app/models/notifications.py` | 3 models + `delivery_enabled` |
| `backend/app/models/__init__.py` | exports |
| `backend/app/schemas/notifications.py` | channel / delivery / run schemas, `NotificationSettingUpdate.delivery_enabled` |
| `backend/app/services/notify_delivery.py` | **new** — transports, payload, eligibility, outbox, `run_once` |
| `backend/app/api/notifications.py` | 4 routes + `delivery_enabled` handling + channel cancel side effects |
| `backend/app/main.py` | worker task in `lifespan` |
| `backend/app/core/config.py` | settings above |
| `backend/app/api/users.py` | `_USER_OWNED_MODELS` += the 3 models |
| `backend/tests/conftest.py` | `NOTIFY_WORKER_ENABLED=false` |
| `backend/tests/test_notification_delivery.py` | **new** — the verification below |
| `backend/tests/test_m29_delete_guards.py` | seed + assert the 3 tables in the delete cascade |
| docs listed above, `.env.example` | reconciliation |

Not touched (other cards own them): `auth.py`, `app/api/vault.py`,
calendar/M52 files, `frontend/**` (M59 is backend-only — delivery has no UI
surface yet; a UI card can build on these routes).

## Verification

Backend (real Postgres, isolated `fintrack_m59_test`, worker disabled so every
step is deterministic):

1. **Settings seeding.** Fresh user `PATCH /notifications/{id}` on a generator
   type with `{"delivery_enabled": false}` → 200 and the field round-trips in
   `GET /notifications`; legacy five still seed `enabled: false` /
   `delivery_enabled: false`, generator three `enabled: true` /
   `delivery_enabled: true`.
2. **Channel seeding.** `GET /notifications/channels` → 2 rows, both
   `enabled: false`, `email.target == users.email`, `webhook.configured ==
   false`; a second call returns the same ids.
3. **Privacy default = in-app only.** User with `privacy_mode=true`:
   `PATCH /channels/{id} {"enabled": true}` → `409 privacy_opt_in_required`
   and `enabled` stays false; `{"enabled": true, "privacy_opt_in": true}` →
   200. Seed an inbox item, run `run_once` → **zero** rows in
   `notification_deliveries` for that user and the webhook sink receives
   **zero** requests. Flip `privacy_mode` off and on again with the opt-in
   still set → delivery resumes (sticky explicit opt-in, Decision 2).
   Non-privacy user: enabling a channel is enough, no opt-in required.
4. **Toggle off stops inbox AND delivery.** Enable the webhook channel + a
   local sink, `delivery_enabled: true` for `bill_due`, seed a bill, `run_once`
   → 1 `sent` delivery, sink got 1 POST; `PATCH` the setting
   `{"enabled": false}` → `GET /notifications/inbox` contains **0** `bill_due`
   items AND a further `run_once` creates **0** new deliveries; with pending
   rows present before the toggle, they are `cancelled` afterwards.
5. **Signed payload with timestamp.** The sink asserts: `Content-Type`
   `application/json`, `X-FinTrack-Event: notification`, integer
   `X-FinTrack-Timestamp`, `X-FinTrack-Signature` matching
   `sha256=` + `hmac(HMAC_SHA256(secret, str(user_id)), f"{ts}.{body}")`
   recomputed from the raw bytes, body keys **exactly**
   `{v,event,delivery_id,alert_type,severity,occurred_at,item_key,link}`, and
   `body` contains neither the bill's name nor its amount.
6. **Minimal email.** SMTP is exercised against a real socket-level SMTP sink
   in-process: subject is the constant, body has the five fields, and neither
   the seeded amount (`337.00`) nor the seeded payee appears anywhere in the
   message. SMTP-unconfigured enable → `409 channel_not_configured`.
7. **Retry → dead-letter → retry endpoint.** Sink returns 500 →
   `run_once` #1 leaves the row `pending`, `attempts=1`,
   `next_attempt_at ≈ now+30s`, attempt row `outcome=retryable`,
   `http_status=500`; forcing `next_attempt_at` into the past and repeating 4
   more times → `status=dead`, `dead_at` set, 5 attempt rows;
   `GET /notifications/deliveries?status=dead` shows it with `attempt_log`;
   `POST .../retry` → 200 `pending`/`attempts=0` with the log preserved;
   a second `retry` on a `pending` row → `409 delivery_not_retryable`.
   Sink returning 404 → dead on attempt 1 (fatal, no retry storm).
8. **Failed delivery leaves the inbox intact.** After the 500-retry scenario,
   `GET /notifications/inbox` returns the same items, `total`, `unread_count`
   and `read` states as before the failures, and `spending_alerts` row counts
   are unchanged by dispatching.
9. **Tenancy.** User B passes user A's delivery id to `GET`-scoped list /
   `POST .../retry` → `404 not_found`; B's `run_once` never touches A's rows.
10. **Demo.** `POST /notifications/deliveries/run` and both `PATCH`es →
    `403 demo_account_restricted`; `GET` routes stay 200.
11. **Delete/export seams.** `DELETE /users/me` with seeded channel, delivery
    and attempt rows → 204 and every table empty (delete-guards test);
    `GET /export/full` contains the three new tables, caller-scoped, with no
    `NOTIFY_SMTP_PASSWORD`/secret column anywhere (export registry test).
12. **SSRF.** `target: "http://192.168.1.10/hook"` → `403
    webhook_target_forbidden`; `target: "ftp://x"` → `422
    invalid_webhook_url`; `target: "http://127.0.0.1:PORT/hook"` → accepted.
13. **Gates.** `uv run ruff format --check && uv run ruff check`,
    `uv run pytest -q` (full suite, isolated DB, zero deselections),
    `npx tsc --noEmit`, `npx eslint . --max-warnings 0`,
    `NODE_ENV=test npx vitest run`, `npm run build`, secret scan.

Running stack (AGENTS §4 steps 4–5): `docker compose up -d --build api`,
confirm `alembic_version == 0030_notification_delivery`, then through
`http://localhost:8080` register a throwaway user → enable a webhook channel
pointing at a local sink → seed a bill → `POST /notifications/deliveries/run`
→ assert the sink's signature verifies and the delivery row reads `sent`,
then delete the throwaway user.
