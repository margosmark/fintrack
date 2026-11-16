# 43 — Household sharing (M90): owner + partner, read-scoped approved projection

> **Numbering deviation (PM review point D1).** The M90 card asked for
> `docs/42-household-sharing.md`. `docs/42` is already taken by
> `docs/42-own-the-edge-security.md` (M87, shipped on `origin/main` in
> `4540a72`), and M87's roadmap entry, `docs/08`, `docs/09`, `docs/29` and
> `docs/38` all cite `docs/42` for that content — **14 files in total**
> (`grep -rl "docs/42"`: 5 spec docs + 9 code/test/asset files). Renumbering
> M87's citations is forbidden (the `docs/13` Guardrails rule "milestones are
> never renumbered or merged"), so this spec takes the next free slot:
> **`docs/43-household-sharing.md`**. No milestone number changes — this is
> still **M90**.

## Context

Household sharing is the top remaining table-stakes gap against Monarch
(blanket multi-user) and Lunch Money (per-invite partner access with scoped
roles). It was deferred from Wave 5 precisely because it touches the tenancy
invariant (AGENTS §2: `user_id` on every domain table and every query), and it
is the first feature in this codebase where **a second authenticated user
reads a first user's rows**. That is the whole difficulty, and it is why this
card is spec-first: the contract below is written before any code, per AGENTS
§7/§9, and implementation does not start until the PM has approved it in-line
on the card.

What makes this more than "an extra reader" is what we will NOT build:

- The partner does not get a session over the owner's tenant. The partner
  signs in as themselves; every partner read is one explicit JOIN away from
  the owner's rows, and every row it can reach is on an allowlist the owner
  chose.
- The partner gets no write path, no key material, no unlock lever, and no
  aggregate that secretly includes accounts the owner did not share.
- Privacy posture carries over unchanged: no ad pixels, no third party, no
  new egress, E2EE-aware (docs/11, docs/33 wording rule: no claim stronger
  than the code).

**Differentiator (the sentence this feature exists for):** the partner sees an
**approved projection** — owner-picked accounts, read-only, consent-gated,
revocable at any moment — never the raw ledger.

## Approach

### 1. Roles — exactly two, exactly one link

| role | column | what it may do |
| --- | --- | --- |
| **owner** | `sharing_links.user_id` | the data subject; controls invite, scope, revoke; reads the audit log |
| **partner** | `sharing_links.partner_user_id` | reads the projection; nothing else |

- v1 caps at **2 users per household**: one owner, one partner. No family
  hierarchy, no guests, no roles beyond these two, no "viewer/editor"
  vocabulary.
- A user may be an **owner on one link and a partner on another** (two
  different rows) — those are independent relationships. What is capped is
  *active seats*: **at most one `active` link per `user_id` and at most one
  `active` link per `partner_user_id`**, both enforced by partial unique
  indexes in the database (§3), not by application checks that can race.
- **Kilter wave wording (t_09492311).** The Kilter adoption card's scope line
  "roles: full vs view-only" maps onto exactly these two seats: **owner =
  full** (their own tenant, plus invite/scope/revoke/audit over the link) and
  **partner = view-only** (the projection, plus its own two writes — accept
  and leave). There is no third role and no per-invite role choice; granting
  an invitee write access is the "any write-sharing" exclusion in §12, which
  still needs its own tenancy spec before it could be built.
- **D6 — the partner's leave route (M90 shipped none; the follow-up adds
  one).** The card pinned "partner is read-only, full stop, in v1", so M90
  deliberately omitted `POST /partner/leave` rather than arguing around it:
  the owner revokes, the partner asks. The ruled follow-up adds exactly one
  route, on the **sharing** router (never `/partner/`, which stays literally
  GET-only): `POST /sharing/leave`, revoke's semantics from the accept side
  (§5). It is the partner's second and last write in the system — accepting
  the invite and ending it — and the projection itself stays read-only by
  construction.

### 2. Scope model — per-account allowlist, default DENY (decided and pinned)

The card offered two readings: (A) whole-timeline aggregate (net worth,
budgets, reports read-only across everything) or (B) per-account allowlist
where unlisted accounts are invisible, not aggregate-blurred.

**Pinned: B, the allowlist. A is rejected.**

Why A loses: an aggregate computed over the whole ledger necessarily includes
the accounts the owner did not pick, so "unlisted" would still be *present* in
every total — that is exactly the "aggregate-blurred" leak the card forbids,
and it would make the owner's opt-out meaningless (a hidden $40k account still
moves the shared net worth).

The rule that follows, stated once and applied everywhere:

> **An account the owner did not allowlist contributes literally nothing to
> the partner's view — not a rounded number, not a delta, not a bucket, not a
> boolean.** Its rows never leave the server, and no server-computed figure
> that mixes it with visible accounts is ever served.

- **Default = `account_ids: []`** (deny-by-default; the most conservative
  choice consistent with the house convention, docs/13 "Ambiguity handling").
  An empty allowlist is legal at every stage of the lifecycle and means *the
  partner sees nothing*. The invite form shows
  "0 accounts selected — this person will see nothing until you choose" and
  the owner may still send: the allowlist is theirs to re-pick with one
  `PATCH` once the link is live, and before acceptance it is set right here,
  at creation (a pending invite's row is addressed by id, §5 — M98).
- **"Explicit, not partial":** the owner picks concrete `accounts.id` values.
  There is no "all accounts" toggle that silently re-scopes later — adding an
  account is always a deliberate `PATCH` the owner makes.
- The allowlist is enforced **at row delivery**, not at aggregation: the
  partner's page computes net worth, budgets progress and spending exactly the
  way the owner's page does (client-side, docs/33 S2), from the rows it holds.
  Rows it does not hold cannot enter any number it prints. This is why
  **M90 adds no `/partner/reports/*` endpoint** — one engine, and the
  allowlist sits where it cannot be bypassed.

**What the partner sees (the whole table):**

| visible (only if allowlisted / owner-level metadata) | never in any partner payload |
| --- | --- |
| allowlisted accounts (`AccountPublic`, incl. `cash_balance`, `investment_value`, `total_value`) | every non-allowlisted account and all of its rows |
| their committed transactions (`TransactionPublic`) | the partner's own data mixed in (the projection is never the caller's tenant) |
| the owner's categories (`id, name, parent_id, is_system`) | owner credentials, tokens, sessions, `llm_*`, `hashed_password` |
| budget **definitions** (`id, category_id, amount, period`, plus M110's `carry` setting and its `carried_in` figure) — **D3** | server-computed `spent` / `remaining` / `pct_used` |
| owner identity: `user_id`, `name`, `email` | `vault_envelopes`, `wrapped_blob`, DEK/KEK/index key, passphrase |
| owner's `privacy_mode` flag (for honest UI copy) | `sync_agreements`, `mcp_tokens`, imports, rules, trades detail, tax data |
| | any number derived from a hidden account (see the rule above) |

**D3 — budget definitions are in, server-side progress is out.** A budget row
is an owner-set *target*, not a fact derived from hidden accounts, so sharing
the definition leaks nothing; but `GET /budgets`'s `spent` is computed by
`services/budgets.py::budget_spent` over the owner's whole ledger, which would
either read hidden accounts (leak) or assert a partial number the server
computed (misleading). The partner route therefore serves **definitions
only** and the client derives progress with
`frontend/src/lib/s3Engine.ts::budgetsProgress` over rows it actually holds —
the same discard-and-recompute the owner's own UI has done since M48-S3. The
partner UI must label those figures "shared accounts only".

### 3. Data model (docs/02 additions — reconciled in the implementation commit)

Two new tables, both carrying `user_id` = **owner** so AGENTS §2 holds
literally and both join `_USER_OWNED_MODELS` (a deleted account and the demo
reset take them with it; `tests/test_demo_seed.py::test_every_user_owned_table_is_classified_for_demo_reset`
names any table that forgets to).

#### `sharing_links` (migration `0035_sharing_links`, `down_revision="0034_mcp_policy_audit"`)

| column | type | notes |
| --- | --- | --- |
| `id` | `SERIAL` PK | |
| `user_id` | `INT NOT NULL`, FK `users.id` | **the owner**; `index=True` |
| `partner_user_id` | `INT NULL`, FK `users.id`, `ondelete="CASCADE"` | NULL while `pending`; set at accept. CASCADE = a deleted partner drops the link, while the owner's audit rows survive (they hold no FK to the partner — the `mcp_audit.mcp_token_id` rule, docs/40 §3) |
| `email` | `VARCHAR(320) NOT NULL` | invite target, stored `strip().lower()`; plaintext by design — it is the owner's own record of whom they invited |
| `token_hash` | `VARCHAR(64) NOT NULL`, **UNIQUE** | sha256 hex of the raw invite token. The raw token exists in exactly two places: the `201` body and the invite email. Never nulled — keeping the hash lets a replay be answered deterministically (`409 sharing_invite_used`) instead of looking like an unknown token |
| `status` | `SharingLinkStatus` StrEnum via `sa.Enum(native_enum=False)`, `VARCHAR(16) NOT NULL DEFAULT 'pending'`, CHECK `ck_sharing_links_status` | `pending \| active \| revoked \| expired \| cancelled` |
| `account_ids` | `JSON NOT NULL`, default `[]` | the owner's allowlist of `accounts.id`, order-preserving, de-duplicated. **JSON, not a junction table** — same reasoning as `sync_agreements.account_refs` (docs/39): read/written only through the sharing router, and tenancy + export scoping ride on `user_id` |
| `expires_at` | `TIMESTAMPTZ NOT NULL` | `created_at + 7 days`. Informational once `active` |
| `accepted_at` | `TIMESTAMPTZ NULL` | |
| `revoked_at` | `TIMESTAMPTZ NULL` | set when `status = "revoked"` |
| `created_at` / `updated_at` | `TimestampMixin` | |

Indexes: `ix_sharing_links_user (user_id)`, `ix_sharing_links_partner_status
(partner_user_id, status)` (the partner's per-request lookup — one indexed
point read, no cache),
`ix_sharing_links_user_created (user_id, created_at DESC)` (owner history),
partial unique `uq_sharing_links_active_owner (user_id) WHERE status='active'`,
partial unique `uq_sharing_links_active_partner (partner_user_id) WHERE status='active'`.

Invariants (a test asserts each):

- **I1** every read of this table carries a `user_id` **or** `partner_user_id`
  predicate — there is no unscoped query of this table anywhere.
- **I2** at most one `active` row per owner and per partner (DB-enforced; the
  accept race is resolved by the database, the loser gets `409
  sharing_partner_occupied` — the `recovery_codes` conditional-write precedent,
  docs/02).
- **I3** `status='active' ⇔ partner_user_id IS NOT NULL AND accepted_at IS NOT NULL`.
- **I4** `status='pending' ⇒ partner_user_id IS NULL AND accepted_at IS NULL`.
- **I5** `revoked`, `cancelled`, `expired` are terminal; a new relationship is
  always a new row.
- **I6** `account_ids` entries are validated to be the owner's own accounts at
  write time; ids that dangle later (account deleted) are **skipped at read
  time, never staged into** — the `sync_agreements.account_refs` rule.

#### `sharing_audit` (same migration) — the M80 pattern, docs/40 §3

| column | type | notes |
| --- | --- | --- |
| `id` | `SERIAL` PK | |
| `user_id` | `INT NOT NULL`, FK `users.id`, indexed | **the owner** — the data subject, so export and account deletion cover the receipts |
| `actor_user_id` | `INT NULL`, **no FK** | who acted: the owner's id, or the partner's on `invite_accept` and `leave`. No FK so deleting that account never deletes a receipt — exactly `mcp_audit.mcp_token_id`'s reasoning. **D7:** this is the one column `mcp_audit` does not have, because there the actor *is* the subject and here they are two different people |
| `invite_id` | `INT NULL`, **no FK** | the `sharing_links.id` the event concerns (link rows die with their users; receipts must not) |
| `event` | `VARCHAR(64) NOT NULL` | `invite_create \| invite_cancel \| invite_accept \| scope_update \| revoke \| leave` |
| `args_hash` | `VARCHAR(64) NOT NULL` | sha256 hex of `json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)` — the invite body carries the invitee's email, so it is hashed, never stored. Formula deliberately identical to `mcp_audit.args_hash` (**D8:** an unsalted sha256 over a low-entropy argument is dictionary-testable; that is already true of `mcp_audit`'s receipts over `auth_login`. HMAC under `JWT_SECRET` would be strictly better and is a one-line change if the PM wants it) |
| `response_code` | `INT NOT NULL` | the status actually returned for a request that reached the handler — handler-level refusals included (the `mcp_audit` derivation rule); a pre-handler refusal leaves no row at all |
| `duration_ms` | `INT NOT NULL` | handler wall time |
| `created_at` / `updated_at` | `TimestampMixin` | append-only rows |

Index: `ix_sharing_audit_user_created (user_id, created_at DESC)`.

Write rules (pinned):

- **Best effort, never on the critical path**: an audit insert that fails is
  logged at WARNING and the response stands — an unreachable receipt sink must
  not fail a user's invite (docs/40 §3's rule, in-process instead of over HTTP).
- **One receipt per sharing-router *mutating* request that reaches its
  handler** (`invite_create`, `invite_cancel`, `invite_accept`,
  `scope_update`, `revoke`, `leave`), success and handler-level refusal alike, with
  the final `response_code` — a refused `POST /sharing/invites` still leaves
  `event=invite_create, response_code=409`. The receipt is committed with the
  state change when it succeeds, and in its own short transaction when the
  request was refused (so a rolled-back invite still leaves a trace).
  **Receipts are written at the handler layer: a pre-handler refusal writes
  no receipt** — the rate limiter's `429`, the demo guard's `403
  demo_account_restricted` and the body-validation `422` are all answered
  before the handler exists to attribute the row to (`backend/app/api/sharing.py`'s
  module docstring states the same rule).
  The owner's three `GET`s and every partner `GET` write **no** receipt:
  the `event` vocabulary has no read value and read traffic would flood a
  consent log.- **Accept** writes a receipt only when `token_hash` resolves to a row — an
  unknown token resolves to no tenant, and `user_id` must be the owner, so
  there is nowhere to attribute it. Documented, not hidden.
- **Leave** follows the same rule: the `404 sharing_not_linked` refusal
  (no row with `partner_user_id = caller`) writes **no** receipt, because no
  owner can be named; every `200` writes one, and a repeated `leave` writes
  another (`response_code=200` again, exactly like a repeated `revoke`).
- **A receipt names the row the act concerned** (M98 F-2): `invite_id` is the
  row actually mutated — the `active` row for `scope_update` / `revoke`, the
  `active` partner row for `leave` — never a stale terminal row that happened
  to sort newest, so an auditor reading `event=leave` always finds the
  partnership that really ended. Owner-side `scope`/`revoke` refusals with no
  row to name (`404 sharing_not_linked`) keep their receipt with
  `invite_id = NULL`, like every other owner-side refusal.
- **Partner `GET /partner/*` reads write no receipt** (high-frequency page
  loads, no state change; the card requires invite/accept/revoke). The table
  shape already supports `event='partner_read'` if read receipts are wanted
  later — **PM review point**.

### 4. Consent lifecycle

```
  (no rows) --POST /sharing/invites--------------> pending
  pending    --POST /sharing/invites/accept------> active      (single-use)
  pending    --DELETE /sharing/invites/{id}------> cancelled   (terminal)
  pending    --TTL 7 days, still unaccepted-----> expired     (terminal)
  active     --POST /sharing/revoke--------------> revoked     (owner, terminal)
  active     --POST /sharing/leave---------------> revoked     (partner, terminal)
  revoked / cancelled / expired --(anything)-----> refused; a new link is a new row
```

| status | meaning | set by |
| --- | --- | --- |
| `pending` | invite created, token live for 7 days | `POST /sharing/invites` |
| `active` | consented; partner reads now resolve | `POST /sharing/invites/accept` (single-use) |
| `cancelled` | owner changed their mind before acceptance | `DELETE /sharing/invites/{id}` |
| `expired` | TTL passed unaccepted | time; materialized on the accept attempt. Display endpoints **compute** it from `expires_at` without writing |
| `revoked` | consent withdrawn, by either side | `POST /sharing/revoke` (owner) or `POST /sharing/leave` (partner) |

- **Single-use:** the token is consumed by the accept that flips the row to
  `active`; any later presentment is `409 sharing_invite_used`. The raw token
  is never returned again after the `201`.
- **Revocation is immediate on the next token-check, with M79's semantics
  (docs/39 §8):** revoke is a local, authoritative, idempotent status write
  (`200` the first time and `200` on every repeat) that sets `status='revoked'`
  + `revoked_at` and never depends on anything else succeeding. The partner
  holds **no token of the owner's** — they authenticate with their own ordinary
  session — so "the token-check" is literally the `WHERE partner_user_id = ?
  AND status = 'active'` read every partner request performs. There is nothing
  to cache-invalidate and nothing to wait for: the very next partner DATA
  request is `401 sharing_not_linked`, and `GET /partner/state` flips to
  `200 {linked: false}` (M92) — the same single uncached read either way.
- **Either side may end it, and both paths land on the same terminal
  state:** `revoke` (owner) and `leave` (partner) both write `status='revoked'`
  + `revoked_at`, so the ended link renders today's "Nobody has access" /
  "access was removed" path for both users with no second UI. The audit log
  is what tells them apart — `event=revoke` with `actor_user_id=owner`
  versus `event=leave` with `actor_user_id=partner`.- **Both users see the state** (§13 UI): the owner in Settings → Sharing
  (`GET /sharing`), the partner in Settings and on the shared page
  (`GET /partner/state` — `linked: true` while the link lives, `linked: false`
  once it is gone; the shared page's empty state still comes from the data
  routes' `401 sharing_not_linked`, §7).

### 5. Endpoints — owner (router `backend/app/api/sharing.py`, prefix `/api/v1/sharing`)

**Rows are addressed by STATE, never by recency (M98 F-1/F-2).**

- The **active row is addressed by state**: `GET /sharing`, `PATCH
  /sharing/scope` and `POST /sharing/revoke` resolve `user_id = owner AND
  status = 'active'` (unique by `uq_sharing_links_active_owner`, §3), and
  `POST /sharing/leave` resolves `partner_user_id = caller AND status =
  'active'` — literally the predicate every partner read re-resolves (§6).
- A **pending invite is addressed by its OWN id** (`DELETE
  /sharing/invites/{invite_id}`); its allowlist is set at creation
  (`POST /sharing/invites.account_ids`). Newest-first is a *listing* order
  (`GET /sharing/invites`, newest first), never a selector.
- With **no active row**: revoke and scope (and a partner's leave) answer
  **404 `sharing_not_linked`** — the byte-identical §7 family — while the
  M79 idempotent repeat of revoke/leave still answers **200** on the
  partnership that ended most recently (`revoked_at` newest). Never a `200`
  that leaves a live link in place, never a `409` a cancel cannot clear.

The old rule said the newest row, justified by "create refuses to make a row
while one is `active`, so the active row *is* the newest" (§5 check 5) — a
premise that only covers rows created AFTER the accept. Pending invites
created before it keep a higher `id` forever, and newest-first let one hide
the live partner from `GET /sharing`, absorb `PATCH /scope` (a `200` that
changed nothing the partner reads), pin `POST /revoke` at `409` permanently —
even after that row was cancelled — and make `leave` answer `200` as a no-op
while `/partner/state` stayed `linked: true`.

Every handler depends on `get_current_user` (demo write guard + the caller's
privacy locks apply unchanged) and `get_pagination` where paged. All request
bodies are `extra="forbid"`.

| Method/Path | Contract |
| --- | --- |
| `GET /sharing` | → **200 `SharingState`** for the owner's **`active` link when one exists**, otherwise their **latest row by `id`**, whatever its status; **no rows → `status: "none"`** with nulls/`[]`. Never 404 (state first: a newer `pending` invite must never hide the live partner — M98 F-1) |
| `POST /sharing/invites` | `{email, account_ids?: [int]}` → **201 `InviteCreated`**. Checks in order: (1) demo write guard → `403 demo_account_restricted`; (2) rate `5/min` → `429 rate_limited`; (3) `email == DEMO_EMAIL` (case-insensitive) → `403 demo_account_restricted`; (4) `email == owner's email` → `403 sharing_self_invite`; (5) an `active` link exists → `409 sharing_partner_exists`; (6) a `pending` invite with the same normalized email → `409 sharing_invite_exists`; (7) ≥ **5** outstanding `pending` invites → `400 sharing_invite_cap`; (8) any `account_ids` entry not owned by the owner → `404 sharing_account_missing`. **Never reveals whether the address has an account** — any address passing (3)–(8) answers `201` (the M87 enumeration-oracle rule, docs/42). Rate **5/min** |
| `GET /sharing/invites` | → **200** `{items: [InvitePublic], total, limit, offset}` (default pagination, newest first). **Never returns `token` / `accept_url`** |
| `DELETE /sharing/invites/{invite_id}` | → **204**, `pending` only (sets `cancelled` + audit `invite_cancel`). Unknown or another tenant's id → **404 `sharing_missing`**; already `active`/`cancelled`/`expired` → **409 `sharing_not_active`**. Rate **10/min** |
| `PATCH /sharing/scope` | `{account_ids: [int]}` → **200 `SharingState`** for the **`active`** row — the row every partner read re-resolves (M98 F-1: a scope write anywhere else reports `200` and changes nothing the partner sees). A `pending` invite's allowlist is set at creation (`POST /sharing/invites.account_ids`) and its row is addressed by id (cancel). **No `active` row → 404 `sharing_not_linked`** (a pending/ended invite is not a household to scope; never a silent `200`). Unknown/foreign account id → **404 `sharing_account_missing`** (uniform: no oracle about which tenant owns an id). Duplicates de-duplicated, first-seen order preserved. Rate **10/min** |
| `POST /sharing/revoke` | no body → **200 `SharingState`** with `status: "revoked"`, `revoked_at` set, always written to the **`active`** row (M98 F-1: newest-first pinned this at `409` forever while a newer `pending` row existed). **Idempotent**: `active` → `revoked` → the repeat answers **200** again on the partnership that ended most recently (`revoked_at` newest). **No `active` row and nothing ever ended → 404 `sharing_not_linked`** (a `pending`/`expired`/`cancelled` row is not a household — cancel the invite instead; never a `409` a cancel cannot clear). Rate **10/min** |
| `POST /sharing/leave` | no body → **200 `SharingState`** with `status: "revoked"`, `revoked_at` set — the **partner's** withdrawal (D6), resolving `partner_user_id = caller AND status = 'active'` (state, never newest — M98 F-2) so it mutates ONLY the row that grants access, and an owner seat the same user holds and every other tenant stay untouched. **Idempotent** (M79): `active` → `revoked` → **200** again on the partnership that ended most recently, one `leave` receipt per 200 **naming the row actually mutated**, with `actor_user_id = partner` under that row's owner. **No partnership for the caller → 404 `sharing_not_linked`**, body byte-identical to §6's envelope, and **no receipt** (no owner to name). Rate **10/min** (`SHARING_MUTATE_LIMIT`, the invitee's own limiter) |
| `GET /sharing/audit?limit=&offset=` | → **200** `{items: [SharingAuditPublic], total, limit, offset}`, `limit` 1–200, `offset ≥ 0`, newest first, **owner's own rows only** (`user_id == current_user.id` on every query — AGENTS §2; there is no cross-account read of this table anywhere) |

- **`email` validation (implemented, pinned):** a Pydantic `AfterValidator`
  calling `email_validator.validate_email` directly — `Owner@Example.COM`
  normalizes to lowercase, garbage → `422 validation_error`. Deliberately
  **not** pydantic's `EmailStr`: it wraps email-validator 2.x, which rejects
  `.local` domains before the handler runs, so `demo@fintracker.local`
  (§10's demo check and Verification case 3) would 422 as a validation
  error and never reach `403 demo_account_restricted`.

```json
// SharingState — 200 from GET /sharing, PATCH /sharing/scope, POST /sharing/revoke,
// and 200 from POST /sharing/invites/accept
{
  "status": "none | pending | active | revoked | expired | cancelled",
  "link_id": 3,                       // null when status == "none"
  "email": "partner@example.com",     // null when status == "none"
  "partner": {"user_id": 7, "name": "Princess", "email": "partner@example.com"},
                                      // null while pending / none
  "account_ids": [1, 4],
  "expires_at": "2026-10-09T14:00:00Z",   // null when status == "none"
  "created_at": "2026-10-02T14:00:00Z",
  "accepted_at": null,
  "revoked_at": null
}
```

```json
// InviteCreated — 201 from POST /sharing/invites. token + accept_url shown ONCE
{ "id": 3, "email": "partner@example.com", "status": "pending",
  "account_ids": [1, 4],
  "expires_at": "2026-10-09T14:00:00Z", "created_at": "2026-10-02T14:00:00Z",
  "token": "HF3x…43 chars urlsafe…", "accept_url": "https://<APP_ORIGIN>/sharing/accept?token=HF3x…",
  "email_sent": false }
```

`token` = `secrets.token_urlsafe(32)`; `token_hash` = sha256 hex of it.
`accept_url` is built from `APP_ORIGIN`. `email_sent` is `false` whenever the
mail did not go out — SMTP unconfigured **or** the owner's `privacy_mode` is on
(§9) — and is never an error: the `201` body is always a working invite.

```json
// InvitePublic — items of GET /sharing/invites (NO token, NO accept_url)
{ "id": 3, "email": "partner@example.com", "status": "pending",
  "account_ids": [1, 4], "expires_at": "…", "created_at": "…",
  "accepted_at": null, "revoked_at": null,
  "partner": {"user_id": 7, "name": "Princess"} }
```

```json
// SharingAuditPublic — items of GET /sharing/audit
{ "id": 12, "event": "invite_accept", "actor_user_id": 7, "invite_id": 3,
  "args_hash": "…64 hex…", "response_code": 200, "duration_ms": 4,
  "created_at": "…" }
```

**Accept (the invitee's first mutation).** `POST /api/v1/sharing/invites/accept`
lives in the **sharing** router, not the partner router, so the partner read
surface stays literally GET-only. It mutates **only** `sharing_links` — never a
domain table.

```json
// request  { "token": "HF3x…" }
// response 200 SharingState (status "active")
```

Checks in order — dependencies resolve before the handler, so the demo guard
(`get_current_user`) fires before the limiter wraps the endpoint: (1) demo write
guard → `403 demo_account_restricted` (a demo session can never accept);
(2) rate **10/min** → `429 rate_limited`; (3) unknown
token → **401 `sharing_invite_invalid`**; (4) the signed-in account's email ≠
the invite's normalized email → **401 `sharing_invite_invalid`** — *the same
code as (3)*, so a token holder learns nothing about which address was invited;
(5) invitee == owner → **403 `sharing_self_invite`** (defence in depth: create
already refuses it); (6) `expires_at < now()` → materialize `expired`, **401
`sharing_invite_expired`**; (7) `cancelled` → **409 `sharing_invite_cancelled`**;
(8) `active` (replay) → **409 `sharing_invite_used`**; (9) the invitee is
already the `active` partner of a *different* owner → **409
`sharing_partner_occupied`** (also the loser's answer when two accepts race —
the partial unique index decides). Success: `status='active'`,
`partner_user_id=session user id`, `accepted_at=now()`.

Email matching is `strip().lower()` on both sides, compared against
`users.email`. **There is no account-existence oracle anywhere in this flow**:
creating an invite for an address that has no account behaves exactly like one
that does.

**Leave (the invitee's second mutation, D6 follow-up).**
`POST /api/v1/sharing/leave` lives on the **sharing** router for the same
reason accept does: the partner read surface stays literally GET-only (§6).
It mutates **only** `sharing_links`, and only the row whose
`partner_user_id = current_user.id` — one indexed read on
`ix_sharing_links_partner_status`, never an unscoped read of the table (I1).
A user who is an owner on one row and a partner on another ends exactly the
partnership they are leaving.

Checks in order: (1) demo write guard → `403 demo_account_restricted`
(dependency, before the handler); (2) rate **10/min** → `429 rate_limited`
(`SHARING_MUTATE_LIMIT`, the same bucket `revoke`/`scope`/`cancel`/`accept`
share); (3) no row with `partner_user_id = caller` → **404
`sharing_not_linked`**, body byte-identical to §6's envelope — 404 rather
than 401 because a 401 from a mutating path would run the client's
refresh → /login dance on a perfectly valid session (§7), and because "no
link for you" is a missing resource, not an expired credential. Success:
`status='revoked'`, `revoked_at=now()`, receipt `event=leave` with
`actor_user_id = partner`, `user_id = owner`, `response_code=200`.

The response is a **`200 SharingState`** carrying the ended row, so the
partner's own client learns the new state in the same round trip; the very
next `GET /partner/state` is `200 {linked: false}` (M92 — the arm the
Settings row renders nothing from) while the data routes answer `401
sharing_not_linked`, and the owner's next `GET /sharing` reads
`status: "revoked"` — the two ended-state renderings that already exist
(§13).

### 6. Endpoints — partner (router `backend/app/api/partner.py`, prefix `/api/v1/partner`)

**Every route is `GET`.** That is a pinned invariant with a test
(`test_partner_router_is_get_only` walks `partner_router.routes`, so it covers
whatever is registered), not a comment. The partner's two mutations — `POST
/sharing/invites/accept` and `POST /sharing/leave` (both §5, on the
**sharing** router) — are why this surface needs no write route of its own.

M90's five routes are `state`, `accounts`, `transactions`, `categories`,
`budgets`. **M106 adds a sixth**, `GET /partner/transactions/{id}/events`
(below) — still a GET, still join-scoped, and the only route here that needs
an actor filter rather than an account one.

Shared resolver `_resolve_partner_link`: resolves the caller's single `active`
link (one indexed read on `ix_sharing_links_partner_status`) and yields
`(link, owner_id, scope)`; an absent link is `None`, never an exception. Two
dependencies sit on top of it:

- `get_partner_link` — the four **data** routes. No `active` link → **401
  `sharing_not_linked`** (covers never-linked, revoked, expired, cancelled,
  owner deleted — one uniform answer, so the response never distinguishes
  "you were revoked" from "you never had access").
- `get_partner_state_link` — `GET /partner/state` only (M92). The same read,
  with the absence left as `None`, so an authenticated caller with no link
  gets **200 `{linked: false}`**: a successful login asking about its own
  sharing state is not an auth failure. A caller with no valid session never
  reaches it — `get_current_user` answers `401 invalid_token` first.

Scope is re-read per request, which is what makes revocation and
scope changes take effect immediately.

**Resolution rule, byte-precise (M98 — the same rule §5 states for the owner
side): the active row is addressed by state, a pending invite by its id.**
Every partner read in this section, and the partner's `POST /sharing/leave`,
resolve `partner_user_id = caller AND status = 'active'`; the owner's `GET
/sharing`, `PATCH /sharing/scope` and `POST /sharing/revoke` resolve `user_id
= owner AND status = 'active'`; `DELETE /sharing/invites/{invite_id}` is the
only route that addresses a pending row, by its own id. Newest-first is a
listing order (`GET /sharing/invites`) and nothing else — recency never
selects the row a consent act writes.

| Method/Path | Contract |
| --- | --- |
| `GET /partner/state` | → **200** `{linked: true, owner: {user_id, name, email}, account_ids: [int], privacy_mode: bool, link_id: int, accepted_at: "…"}` while an `active` link exists; → **200** `{linked: false}` when the authenticated caller has none (**M92** — this route does NOT answer `401 sharing_not_linked`) |
| `GET /partner/accounts` | → **200 `PartnerAccountPage`** `{items: [PartnerAccountPublic], total, limit, offset}` — `Account.user_id == owner_id AND Account.id IN scope`, default pagination (`limit` 50/max 200). Rows are the owner's accounts through the projection serializer; the response is `items: []` when the scope is empty. **Privacy mode (§9):** `cash_balance`, `investment_value`, `total_value`, `starting_balance` are JSON `null` while `owner.privacy_mode` is on, real otherwise |
| `GET /partner/transactions` | `?limit=&offset=&account_id=&from=&to=` → **200 `PartnerTransactionPage`** (money nullable under the owner's privacy mode, §9). `Transaction.user_id == owner_id AND Transaction.account_id IN scope` **AND** the committed-only predicate `transactions._live_scope(owner_id)` (AGENTS §2 staging rule — a `pending_review` batch's rows are invisible to the partner exactly as they are to the owner). `account_id` outside the scope → **404 `sharing_account_missing`** (not a silent ignore: the client asked for a row it may not see). `from`/`to` gate `transactions.date` only, never `posting_date` (docs/02 date-field rule) **M107 (docs/44 §9):** built by the SAME `_transaction_page`, so `items` are **LEDGER rows** (children + ordinary rows, never a split parent) and the page gains `split_parents: list[PartnerSplitParentPublic]` — headers with `child_count`, ordered `date desc, id desc`, `[]` when the page has no children. **Scope invariant: a child is visible iff its parent is.** Children copy `account_id` and scope is account-level (`sharing_links.account_ids`), so this holds *by construction* — the same `account_id IN scope` predicate covers both sides, with no per-child allowlist, no child-level scope and no new partner route (M90's "one projection, one mask"). Under privacy mode the header's money is `null` too (§9); `child_count` survives |
| `GET /partner/categories` | → **200 `CategoryPage`** `{items: [CategoryPublic], total, limit, offset}` — the owner's categories, unfiltered, byte-identical to `GET /categories`. A category name is owner-set metadata, not a fact derived from any account, so it cannot leak a hidden account |
| `GET /partner/budgets` | → **200 bare list** `[{id, user_id, category_id, amount, period, carry, carried_in, category_name}]` — the same bare-list shape as `GET /budgets`, with **`spent` / `remaining` / `pct_used` dropped** (D3); `amount` (the definition's target) is JSON `null` while the owner's privacy mode is on (§9), and the row keeps its identity. **M110:** `carry` is the owner's setting (structure, never masked) and `carried_in` is the rollover figure, produced by the SAME `services.budgets.carry_in` the owner's route uses (one producer, so the two surfaces can never disagree) and masked to JSON `null` alongside `amount` under privacy mode — a leftover reveals spending exactly as much as a limit does (§9) | Ordered by `Category.name`, same as `GET /budgets`. The "these figures are partial" marker is not on this payload: it lives on `GET /partner/state` (`account_ids`) and the scope banner the UI renders from it |
| `GET /partner/transactions/{transaction_id}/events` | **M106 (docs/02 §transaction_events).** `200 PartnerTransactionEventPage` `{items: [{id, tx_id, event_kind, actor, actor_label, at, payload_hash, summary, changed_fields}], total}` — one transaction's receipt log, newest first. **Two filters, in order:** (1) the ROW must be visible — `Transaction.user_id == owner_id AND Transaction.account_id IN scope AND _live_scope(owner_id, account_ids=scope)`, i.e. exactly what `GET /partner/transactions` already serves, so history can never reach past the ledger the partner may read; (2) the RECEIPT's `actor` must be in `{owner_id, partner_user_id}` — a bank-sync receipt is staged with `actor IS NULL` and is dropped here, so a partner sees what the two of them did and never what a machine did ("partner did X" + "owner did X", nothing else). Then `project_transaction_events` strips the `payload` entirely: a payload is the exact set of values a PATCH wrote (`amount_native` among them) and §9 nulls money for a partner, so shipping it would walk around the mask. Field NAMES survive as `changed_fields` — a name is structure, not a figure. `404 not_found` for a row outside the scope (same answer as an unknown id), `401 sharing_not_linked` without an active link |

Re-use, not re-implementation: the transaction query is the owner's list query
with `current_user.id` replaced by `owner_id` from the JOIN and
`account_id IN scope` conjoined — one query builder, so the staging rule, the
`date` rule and the pagination shape cannot drift between the two surfaces.
If extracting a shared builder is needed to do that, the extraction must leave
`GET /transactions`'s behaviour byte-identical.

**Tenancy rule (the one the card calls an escalation if broken):**

> Every partner query expresses ownership as
> **`<domain table>.user_id == link.user_id`, reached from
> `SharingLink.partner_user_id == current_user.id`** — a JOIN path. No partner
> query may filter a domain table by `current_user.id` (that is the partner's
> own tenant, wrong data) or by nothing at all (that is a cross-tenant read).
> No column-level workaround — no "shared_with" flag on domain rows, no
> denormalized partner id, no view over unscoped rows. If any query reads
> cross-tenant rows without the join, that is an **escalation**, not a design
> choice: stop, comment on the card, do not ship it.

**M159 — `scope=household` on the two report routes (docs/12 §Owner dimension,
docs/08 §Report routes).** The Owner lens no longer computes in the browser:
`GET /reports/spending?scope=household` and
`GET /reports/spending/categories?scope=household` sum the caller's own rows
PLUS the rows this same link projects, and the page renders that answer. The
scope resolves through the SAME resolver as `GET /partner/transactions`
(`_resolve_partner_link`'s read: `partner_user_id == caller AND status =
active`, the live-account allowlist, I6 dangling ids skipped), so every rule
above holds for it without amendment:

- **Nothing is caller-supplied.** The counterpart id and the account list come
  from the caller's own link row — there is no request field that can name a
  different user or an outside account, and a non-joined caller's household
  scope is their own rows (`backend/tests/test_reports_household.py::
  test_a_non_joined_caller_gets_zero_partner_rows`).
- **One-directional (§2).** The grant reads the OWNER's accounts to the
  PARTNER; an owner-seat caller therefore gains nothing from the scope —
  partners never write (§12), so the owner's household is their own rows.
- **Privacy mode (§9, D4) is a population-level mask.** A household total is
  the owner's money exactly as much as a row's amount is, and an aggregate has
  no field to null: a privacy-mode owner projects an EMPTY scope, so the
  partner's household is their own rows. The owner's own view is untouched.
- **Committed-only + LEDGER (docs/03, docs/44 §8)** ride the same predicate as
  the narrow scope — one loader, one contract, no per-side drift.

**M179 — the scope reaches the Categories tab's second panel.** The Owner lens
is a TAB-level lens, so `GET /reports/category-tag-matrix?scope=household` adds
the Category × Tag matrix to the same population (docs/12 §Owner dimension,
docs/08 §Report routes): before it, the tab drew a household donut above an
own-rows matrix, and the matrix vanished entirely under the lens. Every rule
above holds for it without amendment, because the widening happens in ONE place
— `services/reports.py::household_ownership()` returns the ownership predicate
(`own OR (counterpart AND account_id IN the caller's own live allowlist)`) that
`_committed_transactions`, both matrix aggregates and every future consumer
build from; the staging rule, the LEDGER split rule and each report's own math
stay shared. Two consequences worth naming:

- **Both tenants' namespace, both axes.** A projected row carries the OWNER's
  category AND tag ids (docs/43 §6), so the matrix layers the counterpart's tree
  over the caller's for rows and columns alike — otherwise a foreign category
  prints as `Uncategorized` and a foreign tag column renders id-less. The
  counterpart wins a bare-id conflict; ids only one side carries keep their own
  name.
- **Ranking and truncation follow the widening.** The top-8 tag cut, the
  `Other tags` absorber and the row order are computed over the HOUSEHOLD's tag
  spend, exactly as they are over the caller's own — the scope changes who is
  summed, never how the grid is built.

Routes deliberately left out of the scope are the ones whose population this
projection cannot deliver: **cash flow, net worth and the investment surfaces**
mix transaction rows with trade cash actions, balances, positions and price
rows, and there is no `/partner/trades` projection — they keep the own-only
`scope` singleton and REFUSE `household` rather than printing a narrower
population under a household caption (docs/12 §Owner dimension's disposition
table). The **budget models** are this document's own §12 anti-scope
("aggregated household budget models — v2+ if ever").

**Owner-lens expansion — the transaction-only routes.**
`GET /reports/insights?scope=household` and
`GET /reports/spending-comparison?scope=household` widen through the SAME
`household_ownership()` predicate: their ENTIRE population is committed
transactions — exactly what §6's projection delivers — so every rule above
holds for them without amendment (nothing caller-supplied, one-directional, a
privacy-mode owner projects nothing, committed-only + LEDGER shared, and the
counterpart's tree naming foreign category ids). Neither route has a UI lens
control, so the widening is a contract on the API and no screen changed; the
ten trade/price-backed report routes keep the singleton refusal.

### 7. Error envelope (docs/08 § Error envelope, reconciled in the implementation commit)

Every failure is `{"detail": <str>, "code": <str>}` via
`app.core.errors.api_error` — never a bare 500.

| code | status | when |
| --- | --- | --- |
| `sharing_self_invite` | 403 | create: invite address is the owner's own; accept: invitee would be the owner |
| `sharing_invite_exists` | 409 | a `pending` invite for that normalized address already exists |
| `sharing_partner_exists` | 409 | owner already holds an `active` link — create check (5), and its mirror on accept (sequential path; the RACED loser answers `sharing_invite_used`, below) |
| `sharing_invite_cap` | 400 | ≥5 outstanding `pending` invites (parity with `mcp_token_cap`'s 400) |
| `sharing_account_missing` | 404 | `account_ids` entry unknown **or** another tenant's (uniform — never distinguishes the two) |
| `sharing_missing` | 404 | unknown/foreign **invite id** on `DELETE /sharing/invites/{id}` (owner-link routes use `sharing_not_linked`, below) |
| `sharing_not_active` | 409 | cancel a non-`pending` invite |
| `sharing_invite_invalid` | 401 | accept: unknown token **or** token not addressed to the signed-in account (one code for both) |
| `sharing_invite_expired` | 401 | accept after `expires_at` |
| `sharing_invite_used` | 409 | accept replay of an already-`active` invite (single-use); **also the OWNER-side accept-race loser** (M98 F-6): `uq_sharing_links_active_owner` refused the second accept because the owner's single seat was already consumed by the winning one — an owner conflict, so it is never answered with the partner-side code |
| `sharing_invite_cancelled` | 409 | accept of a `cancelled` invite |
| `sharing_partner_occupied` | 409 | invitee already the `active` partner of another owner — including the accept race loser **when the PARTNER seat (`uq_sharing_links_active_partner`) refuses** |
| `sharing_not_linked` | **401** on every `/partner/*` **DATA** GET without an `active` link (`/accounts`, `/transactions`, `/transactions/{id}/events` (M106), `/categories`, `/budgets`). `GET /partner/state` never returns it (M92): an authenticated caller with no link gets **200 `{linked: false}`**, and a caller with no valid session gets `401 invalid_token` from `get_current_user` before the resolver runs. **404** on `POST /sharing/leave` with no partnership for the caller, and on owner `PATCH /sharing/scope` / `POST /sharing/revoke` with no `active` row and nothing ended (M98 F-1/F-2 — same `detail` + `code` body in every case, the status differing only so a mutating path can never trigger the client's refresh → /login dance) |
| `demo_account_restricted` | 403 | *existing code*: demo-owner mutations (the demo write guard), inviting `demo@fintracker.local`, a demo session accepting |
| `rate_limited` | 429 | *existing code* |
| `validation_error` | 422 | *existing code*: malformed body / unknown field (`extra="forbid"`) / bad `limit` |
| `invalid_token` | 401 | *existing code*: no/expired bearer on any sharing route |

**Client 401 handling (pinned, easy to get wrong):** `frontend/src/api/client.ts`
turns an unexpected `401` into a refresh → replay → clear-tokens → `/login`
dance. `401 sharing_not_linked` is a **domain state, not an expired session** —
the partner's own session is perfectly valid. The `/partner/*` prefix and
`/sharing/invites/accept` must therefore sit in the `AUTH_401_SKIP_PATHS`
mechanism (a `AUTH_401_SKIP_PREFIXES = ("/partner/",)` tuple plus the exact
accept path, so the set cannot rot as routes are added) so the `401` reaches
the caller as an `ApiError` and the shared page renders the empty state
instead of logging the partner out. (M92: `GET /partner/state` no longer
produces that 401 for an unlinked caller — it answers `200 {linked: false}`
and `PartnerSharingRow` treats it as data, rendering nothing and leaving the
SharingSection invite CTA as the page's next step. The prefix stays, but the
error BODY is what decides (M98b F-5): `401 sharing_not_linked` still
resolves here as an `ApiError` with no dance — that caller's session is
valid — while a genuinely signed-out caller's `401 invalid_token` runs the
normal refresh → replay → clear-tokens → `/login` flow and lands on the login
page instead of a fail-closed panel that never refreshes or redirects; an
unreadable body counts as "not the case" and keeps the quiet skip.)
`POST /sharing/leave` is the reason it answers **404**, not 401: it is not
in that skip set, so a 401 there would have run the dance on a valid
session — the same envelope, a status that means "no link for you".
### 8. E2EE interaction (docs/33) — locked shape, and no unlock lever

Pinned facts, in order of what they force:

1. **The partner always renders the owner's rows in the locked shape.** The
   partner's client calls `resolvePayee` / `resolveAmount` /
   `resolveRawDescription` / `resolveAccountName`
   (`frontend/src/lib/e2ee.ts`) with **`dek = null`, unconditionally**. Any row
   carrying ciphertext therefore renders `🔒`; a row without ciphertext
   renders its plaintext — byte-for-byte what the owner's own browser renders
   while the vault is locked (docs/33 A16).
2. **The owner's lock state cannot be consulted, and is not.** Lock state is
   session memory in the owner's browser (docs/33 key-holding model) — the
   server has no lock flag to read. So the contract is the *stronger* form of
   the card's requirement: the partner sees the locked shape whether or not the
   owner is unlocked. Since the owner-unlocked view can only be ≥ the
   locked view, "partner ≤ owner-locked" holds in both owner states.
3. **The partner can never see decrypted values, and this is not an
   oversight.** Making that possible would mean handing the partner the
   owner's DEK or passphrase — the exact side channel the card forbids. So:
   **no partner payload contains `vault_envelopes`, `wrapped_blob`, a DEK, a
   KEK, an index key or a passphrase**, and `/vault/*` gains nothing from this
   feature: those routes are ordinary owner-scoped reads/writes whose every
   query filters `vault_envelopes.user_id == current_user.id`, so a partner's
   bearer can only ever create or read **their own** envelope, never the
   owner's (and the demo guard still answers `403 demo_account_restricted`
   there). **The partner
   cannot trigger a vault unlock on the owner's behalf: there is no
   server-side unlock action in this product at all** — unlock is a
   client-side passphrase → KEK → DEK derivation that never reaches the API.
   A test asserts no `/partner/*` response body contains a key-shaped field.
4. **Honest wire-level disclosure (docs/33 wording rule, stated not hidden).**
   The projection serves the *same* `TransactionPublic` / `AccountPublic`
   schemas the owner's browser receives, plaintext columns included
   (`normalized_payee`, `amount_native`, …) — those columns are mandatory and
   server-readable today (docs/33 "Server-readable plaintext TODAY"), which is
   why the client-side report engines can compute at all. `🔒` is therefore a
   **display rule, not a confidentiality boundary**, for the partner exactly as
   it already is for the owner. Strengthening that would mean dropping
   plaintext columns from API responses — docs/33's per-user strict mode, a
   different and much larger wave. M90 neither weakens nor overclaims the
   existing posture.
5. `users.encryption_required` gates the *owner's* writes (422 on create/
   update without ciphers, docs/33) and does not change any partner read. For
   an owner who has opted into encryption, the partner sees `🔒` on every
   cipher-bearing field until that owner turns encryption off — the honest
   consequence of not sharing keys, documented here rather than discovered in
   QA.

### 9. Privacy-mode interaction — egress lockdown + the partner amount mask (D4)

`privacy_mode` does two separate things here, and the spec keeps them apart:

- **An egress lockdown** (docs/11 steps 5–6, docs/09 Settings row), shipped
  with M7 and untouched by M90/M93: cloud LLM sealed
  (`409 privacy_mode_requires_local_llm`), BoC/yfinance/LLM calls sealed
  (`403 privacy_mode_blocks_external_api`), outbound notification/webhook
  channels requiring `privacy_opt_in_at` (`409 privacy_opt_in_required`),
  stored cloud `llm_base_url` cleared on enable.
- **An amount mask on the PARTNER surface** (Reading A, **implemented in
  M93 W2**): while the OWNER has privacy mode on, every monetary field in
  the `/partner/*` payloads is JSON `null`.

M90 pinned Reading B — "the owner's posture governs, and nothing that
privacy mode seals is unsealed by sharing; no figure is masked" — and said
so through the honesty test `test_privacy_mode_does_not_mask_amounts_today`,
written so that approving Reading A would show up as a diff instead of a
silent assumption. **That diff is this wave:** the test is replaced by
`test_privacy_mode_masks_every_partner_money_field`.

#### The masking rule

| Aspect | Behaviour |
| --- | --- |
| Governing flag | the OWNER's `users.privacy_mode`, read per request from the live owner row at link resolution (`_resolve_partner_link`) — no cache, no restart, effective on the next read. The partner's own flag never seals or unseals anything: the data subject decides |
| Masked | accounts `cash_balance`, `investment_value`, `total_value`, `starting_balance`; transactions `amount_native`, `cad_amount`; budgets the definition's target `amount` **and its M110 `carried_in`** (money, docs/43 §9 / docs/09 §Budget carry); **M107:** `PartnerTransactionPage.split_parents[].amount_native` / `cad_amount` (the group header) |
| Not masked | identity and structure — account names, ids, currencies, dates, payees, `raw_description`, category names, budget `period` **and budget `carry`** (the rollover is a setting, not a figure) — plus the E2EE ciphers (already opaque ciphertext). `GET /partner/state` and `GET /partner/categories` carry no money; **M107:** `split_parents[].child_count` / `is_split` / `parent_split_id` and the badge (`Split · N parts`) are structure, never figures |
| Where | `app/schemas/sharing.py` — `project_account_page`, `project_transaction_page`, `project_budget`, the projection serializers (M90 §Reading A already named the correct home). A partner route returns a projected page, never a bare `AccountPage` / `TransactionPage`, so a future partner route cannot ship without the mask |
| Never masked | the OWNER's own view. `GET /accounts`, `GET /transactions`, `GET /budgets` keep serving the real numbers they always did — privacy mode hides the figures from the partner, not from the account holder (masking the owner's own UI is out of scope: it is a separate feature with its own spec) |
| UI | the partner page labels the gap instead of blanking it (docs/09): banner, `🔒` in the figure slot, no budget bar drawn from figures that do not exist |

The five behaviours that stay exactly as M90 pinned them:

1. **The governing flag is the OWNER's `privacy_mode`**, read from the owner's
   `users` row at link resolution. The partner's own flag never seals or
   unseals the owner's data — the data subject decides. (Test: owner on +
   partner off ⇒ masked; owner off + partner on ⇒ ordinary projection.)
2. **Partner reads trigger zero egress, now and later.** v1 partner routes are
   row reads that call none of the three enumerated egress seams (AGENTS §2,
   docs/11 inventory). Belt-and-braces: `get_partner_link` binds
   `fx_service.bind_privacy_lock(owner.privacy_mode)`,
   `market_data_service.bind_privacy_lock(owner.privacy_mode)` and
   `bind_llm_privacy_lock(owner.privacy_mode)` for the request scope — the
   same three ContextVars `deps.get_current_user` binds for the *caller* — so
   any future partner route that reaches an egress seam is sealed by the
   owner's flag, not the caller's. A test with a spied transport asserts
   **zero outbound HTTP** across a full sweep of all five partner routes under
   owner privacy mode.
3. **The invite email obeys the owner's privacy mode.** `owner.privacy_mode`
   ⇒ the mail is not sent (`email_sent: false`); the one-time `accept_url` in
   the `201` body is the delivery channel — docs/11's "in-app only unless
   explicitly opted in", applied to outbound mail. SMTP unconfigured ⇒ the same
   `email_sent: false`. Neither is ever an error.
4. **No peek-through by construction:** every partner payload is built by the
   projection serializers above, so there is one place to look for a leak and
   no second path to forget. The owner-facing serializers (`AccountPublic`,
   `TransactionPublic`, `BudgetPublic`) are deliberately NOT touched — that is
   what keeps rule 5 and the owner's own view independent.
5. **The privacy-mode acceptance test states exactly what the partner sees**
   (`test_privacy_mode_masks_every_partner_money_field`): owner privacy on ⇒
   every money field `null` on all three money routes, a byte-scan of the
   whole sweep for the seeded amounts comes back empty, budget rows keep
   `id`/`category_name`/`period` with `amount: null`, `state.privacy_mode` is
   `true`, the owner's own endpoints are byte-unchanged, and privacy off puts
   every figure back. The sibling egress test asserts the same mask as a
   privacy on/off *difference* (`accounts`, `transactions`, `budgets` differ;
   `categories`, which carry no money, stays byte-identical).

D3 is unaffected: `GET /partner/budgets` never carried `spent` /
`remaining` / `pct_used` in either mode — those are still absent, and the
UI's "shared accounts only" progress still comes from the client over rows it
holds (docs/43 §13). Under privacy mode the client simply does not draw it,
because the target figure it would divide by is `null`.

### 10. Demo tenant (docs/20, docs/30)

- **A demo owner cannot invite**: `POST /sharing/invites` is a mutation, so
  the existing demo write guard answers `403 demo_account_restricted` with no
  new code. Same for `PATCH`, `DELETE`, `POST /revoke`.
- **The demo tenant cannot be invited**: `POST /sharing/invites` with
  `email == DEMO_EMAIL` → `403 demo_account_restricted` (an explicit check at
  create — the guard does not cover it, because the *caller* is not the demo).
- **A demo session cannot accept**: the accept route is a POST, so the guard
  refuses it; the explicit check sits behind it for defence in depth. The demo
  tenant can therefore never hold an `active` link in either seat, and
  `GET /partner/*` from the demo answers like anyone else: `200 {linked:
  false}` from `/partner/state` (M92), `401 sharing_not_linked` from the data
  routes. Both directions are asserted by a test.
- **Demo reset / delete-user**: `sharing_links` and `sharing_audit` carry
  `user_id` (owner) and join `_USER_OWNED_MODELS`, so `delete_user_rows` takes
  them (child-before-parent: both tables reference only `users`, so appending
  them at the end of the list is safe) and the demo reset wipes them with the
  tenant. `_RESET_EXTRA_MODELS` needs no entry.
- **The M10 seed creates no sharing row** — nothing new appears in
  `seed_fingerprint`.
- **Export:** both tables are derived from `SQLModel.metadata`, so
  `GET /export/full` picks them up with no registry edit (docs/11 step 2);
  neither carries a secret (`token_hash` is a dead capability once status ≠
  `pending`, and `args_hash` is a digest).

### 11. Rate limits & the invite-spam surface (docs/01 `app/core/ratelimit.py`)

New constants beside `IMPORT_UPLOAD_LIMIT` / `SYNC_MUTATE_LIMIT`:

```python
SHARING_INVITE_LIMIT = "5/minute"   # POST /sharing/invites — the spam avenue
SHARING_MUTATE_LIMIT = "10/minute"  # accept | cancel | scope | revoke | leave
```

Applied with `@human_paced_limit(...)` (the decorated route must declare
`request: Request`). Buckets are per (client, route function), so a path
parameter cannot multiply a client's limit (`key_style="endpoint"`).
`GET /sharing`, `GET /sharing/invites`, `GET /sharing/audit` and every
`GET /partner/*` route carry **no** limiter — parity with every other read in
the app. Beyond the per-minute ceiling, the cap surface is: **5 outstanding
invites per owner** (`400 sharing_invite_cap`) and **one active link per side**
(DB partial unique) — so a single owner cannot mint an unbounded pile of live
tokens even over hours.

Tests must opt in: `tests/conftest.py` sets `RATE_LIMIT_ENABLED=false`, so the
rate tests set `limiter.enabled = True` / `False` in a fixture exactly as
`backend/tests/test_import_dos.py:341-362` does.

### 12. Explicitly out of scope

- **The partner never sees the owner's forecast** (M109, docs/45 §Tenancy).
  `/forecast/*` authenticates with `get_current_user` — the CALLER's own tenant
  — so a linked partner can open `/forecast` and gets their OWN scenarios, not
  the owner's, and no forecast field appears in any `/partner/*` response or on
  `/shared`. This is the app's FIRST owner-model-only surface: sharing is a
  read-scoped projection of the LEDGER (§2), and a what-if is a decision the
  owner is about to make, not a fact about shared accounts. Pinned by
  `backend/tests/test_forecast.py::test_partner_surface_carries_no_forecast_data`
  rather than left to convention.
- **Multi-user beyond 2** (families, guests, roles, hierarchies).
- **Any write-sharing**: no shared budgeting, no shared categorization, no
  comments/chat on transactions, no invitations to *edit* — the partner's only
  writes in the whole system are accepting the invite addressed to them and
  leaving the link themselves, both on the sharing router (§1, §5).
- **Aggregated "household budget" models** (two tenants' data merged) — v2+ if
  ever, and it needs its own tenancy spec.
- **Amount masking on the OWNER's own surfaces** — the partner half of
  Reading A shipped in M93 W2 (§9); hiding the account holder's figures from
  their own browser is a different feature and still needs its own card.
- **MCP surface for sharing**: no new MCP tool reads the link table; an MCP
  token resolves to its minting user's own tenant, which is unchanged.
- **Notification/inbox events** for invite, accept or revoke (the audit log
  and the two state endpoints carry it in v1).
- **Sharing anything but accounts**: no per-category, per-tag or per-transaction
  fine-grained grant — the unit is the account.
- **Any partner write beyond the two that exist** — accept (M90) and the
  one-route `leave` (D6 follow-up, M93). No scope edits, no shared
  categorization, nothing that touches a domain row.
- **Read receipts on `/partner/*`** (§3).
- **A partner split route** (M107, docs/44 D13): the four `/transactions/{id}/split`
  routes are owner-only, so the partner gets **read-only grouping** — `split_parents`
  on the page they already have, expanded from that page's own children (§13). No
  create, no re-split, no merge through the seam, and no per-child scope rule.
### 13. UI (docs/09 row added in the implementation commit)

- **Owner — Settings → Sharing** (`frontend/src/components/settings/SharingSection.tsx`,
  mounted from `Settings.tsx` beside the existing sections): one card that
  renders `GET /sharing` — an empty state ("Nobody has access" — for `none`
  AND for ended rows, since `revoked`/`expired`/`cancelled` all mean nobody has
  access; only `pending`/`active` get cards), a pending
  card (address, expiry countdown, **Copy invite link** for an invite created in this
  session — the raw token exists in exactly two places (§5), so after a reload the
  card says the link was shown once rather than offering a dead copy — Cancel), or an active
  card (partner name + email, accepted date, the account multi-select that
  `PATCH`es scope live, **Revoke access** with a confirm). Below it, the
  collapsed audit list from `GET /sharing/audit`. Inviting shows the
  `email_sent` truth: "Invitation link copied — email was not sent (privacy
  mode / SMTP not configured)".
- **Invitee — `/sharing/accept?token=…`**: reads the token from the query
  string (never from `localStorage`) and states what they will be able to see —
  there is deliberately **no pre-accept preview endpoint** (the accept surface
  is the only thing that answers a token); the inviter's identity comes from
  `GET /partner/state` on the `/shared` landing that Confirm navigates to. It
  **preserves the token across sign-in/register** so the
  recipient can create the account the invite was addressed to first. Confirm
  → `POST /sharing/invites/accept` → land on `/shared`. Errors render the
  envelope verbatim (`401 sharing_invite_expired` ⇒ "This invitation has
  expired — ask for a new one"), never a redirect to `/login`.
- **Partner — `/shared`** (`frontend/src/pages/SharedHousehold.tsx`, new route
  in `App.tsx` + one sidebar entry): scope banner ("Shared accounts: 2 —
  figures exclude everything else"), owner identity from `/partner/state`,
  account balance cards, a transaction feed (paged fetch until `total`, the
  pattern the owner's pages already use), budget cards whose progress comes
  from `s3Engine.budgetsProgress` over fetched rows and are labelled "shared
  accounts only". Read-only by construction: there is no write control on the
  page because there is no write endpoint to call. Cipher fields render `🔒`
  (§8).
  **Splits (M107, docs/44 §9):** the feed groups rows through the owner's
  `SplitGroupRow` over `groupSplitRows(items, split_parents)` (docs/09 §Split
  transactions) — one header per statement carrying the parent's date / payee /
  total and the badge `Split · N parts`, with the children folded under it.
  Expansion is **local**: `/shared` renders the children already in the page
  instead of calling `GET /transactions/{id}/split`, because that is an OWNER
  route and the partner has no split route of their own (D13 — one projection,
  no new surface). A content filter can therefore show a partial group, and the
  header's `child_count` reports the true number of parts anyway. The header
  renders `🔒` for its amount under privacy mode like every other money slot.
  **Privacy mode (§9, M93 W2):** when `state.privacy_mode` is on the page
  labels the gap instead of blanking it — a banner under the scope banner
  reading **"Amounts hidden — the owner has privacy mode on, so balances and
  amounts are not shared with you. Names, dates and categories still show."**
  (the exact caption, `role="status"`), the account card's figure becomes `🔒`
  with `title="Hidden by the owner’s privacy mode"`, every feed row resolves
  to `🔒` (`resolveAmount` treats a null amount as the locked shape), and the
  budget section keeps its heading but shows "Budget amounts are hidden while
  the owner has privacy mode on." instead of cards — `loadSharedView` returns
  `budgetProgress: []` in that mode and does not fetch definitions it cannot
  use. Privacy off: byte-for-byte the pre-M93 rendering. The data routes' `401 sharing_not_linked` ⇒ "No shared household —
  access was removed" empty state (§7); `GET /partner/state`'s `200 {linked:
  false}` raises the same envelope client-side, so the empty state has one
  trigger either way (M92).
- **Partner — Settings**: one row from `GET /partner/state` — "You can see
  *Princess*'s data — 2 shared accounts, since 2026-10-02" — rendered only
  while `linked: true`; `linked: false` renders NOTHING there — it is data,
  not an error — so the owner-consent card above (the invite CTA) is what
  the page shows (M92). Beside the row sits **Leave** (D6 follow-up) behind
  the same two-step confirm the owner's "Revoke access" uses: the first
  press arms it ("Confirm leave" + "Stay", plus a line stating that access
  ends immediately and nothing changes on the owner's side), the second
  press `POST /sharing/leave`. Success toasts "You left the shared
  household" and the row disappears when the refetched `GET /partner/state`
  answers `200 {linked: false}` — rendered in place, never a redirect (their
  own session is fine).
### 14. Touch points

**This (spec) commit** — nothing else:

- `docs/43-household-sharing.md` (this file).
- `docs/10-roadmap.md` — the **M90** entry (§ Verification quotes it).

**Implementation commit(s)**, reconciled together per AGENTS §4 gate 0 and §9
(no partial docs):

- `backend/app/models/sharing.py` (`SharingLink`, `SharingAudit`) +
  `backend/app/schemas/sharing.py` + registered in `backend/app/models/__init__.py`.
- `backend/alembic/versions/0035_sharing_links.py` (`down_revision="0034_mcp_policy_audit"`;
  `alembic upgrade head` clean on a fresh DB).
- `backend/app/api/sharing.py` (owner + accept), `backend/app/api/partner.py`
  (M90's five GETs + M106's `/{id}/events`), both mounted in
  `backend/app/main.py`.
- `backend/app/api/transactions.py` — the §6 shared builder: `_transaction_page(...)`
  plus `_live_scope(owner_id, account_ids=…)` (owner calls pass `None` — the
  owner's SQL stays byte-identical) — and `backend/app/api/accounts.py` —
  `_committed_cash_subquery(account_ids=…)`. One implementation serves both
  surfaces so the staging / date / pagination rules cannot drift.
- `backend/app/services/notify_delivery.py` — `send_invite_email` (the SMTP
  call site, beside the notification mailer; the router holds only the
  owner-privacy gate and the call — no network code in a router).
- `backend/app/core/ratelimit.py` — the two constants.
- `backend/app/api/users.py` — append `SharingLink`, `SharingAudit` to
  `_USER_OWNED_MODELS` (they reference only `users`, so the end of the list is
  FK-safe).
- `frontend/src/api/client.ts` — `AUTH_401_SKIP_PREFIXES = ("/partner/",)` +
- **M107 addendum (docs/44 §9):** `backend/app/schemas/sharing.py` gains
  `PartnerSplitParentPublic` + `_project_split_parent` beside `_project_transaction`
  (the SAME mask), `backend/app/api/partner.py` keeps building the page through
  `_transaction_page`, and `frontend/src/pages/SharedHousehold.tsx` groups its feed
  with `SplitGroupRow` (local expansion, no partner split route).
  `/sharing/invites/accept` (§7).
- `frontend/src/components/settings/SharingSection.tsx`, `frontend/src/pages/SharedHousehold.tsx`,
  `frontend/src/pages/SharingAccept.tsx`, `frontend/src/App.tsx` (two routes),
  `frontend/src/api/sharing.ts` (typed client), `Settings.tsx` mount,
  `AppShell.tsx` sidebar entry.
- `frontend/src/pages/Login.tsx` / `Register.tsx` — route-state passthrough so
  `/sharing/accept?token=…` (RequireAuth's `state.from`) survives the
  login ↔ register hop.
- `docs/02` (two tables + indexes + ERD line), `docs/08` (both route tables +
  the error-code list), `docs/09` (Settings row + `/shared` + `/sharing/accept`),
  `docs/11` (the §9 privacy interaction), `docs/00` only if the feature list is
  being extended (it currently stops at M44 — precedent is to leave it).
- **Kilter wave (t_09492311):** `backend/tests/test_partner_role_enforcement.py`
  (new — the §2 role-enforcement and cross-household pins; test-only change, no
  product code) + the §1 role-mapping note and these two §2 rows.

## Verification (AGENTS §9: concrete input → expected observable output)

### 1. The E2E walkthrough (the card's acceptance, one test)

`backend/tests/test_household_sharing.py::test_full_walkthrough_invite_accept_scope_revoke`
— two real users, `owner` (accounts `A` shared, `B` hidden, each with
transactions) and `partner`:

1. `POST /sharing/invites {email: partner.email, account_ids: [A.id]}` as
   owner → **201**, `status: "pending"`, `token` present **once**, `expires_at`
   = `created_at + 7d`.
2. Partner (signed in as themselves) `POST /sharing/invites/accept {token}` →
   **200** `SharingState{status: "active", partner.user_id == partner.id,
   accepted_at != null}`.
3. `GET /partner/accounts` as partner → **200** with exactly `[A]`; `B`'s id
   appears nowhere in the body.
4. `GET /partner/transactions` → **200** with A's rows only, committed-only;
   the same request with `account_id=B.id` → **404 `sharing_account_missing`**.
5. `PATCH /sharing/scope {account_ids: []}` as owner → **200**; the partner's
   next `GET /partner/accounts` → **200 `{items: [], total: 0}`** (deny-by-default
   proven live).
6. `PATCH /sharing/scope {account_ids: [A.id]}` → **200**; accounts back.
7. `POST /sharing/revoke` as owner → **200** `status: "revoked"`, `revoked_at`
   set; `POST /sharing/revoke` again → **200** (idempotent, M79 semantics).
8. Partner's **next** `GET /partner/state` → **200 `{linked: false}`** (M92 —
   the link is gone and that is the whole answer), while the four data routes
   still answer **401 `sharing_not_linked`** with the clean envelope
   `{"detail": "…", "code": "sharing_not_linked"}` — and the partner's own
   session still works: `GET /users/me` → **200**.
9. `GET /sharing/audit` as owner → the receipts
   `invite_create(201)`, `invite_accept(200)`, `scope_update(200) ×2`,
   `revoke(200) ×2`, newest first, `actor_user_id` = owner except on
   `invite_accept`.

### 2. The named cases the card requires

| test | pins |
| --- | --- |
| `test_partner_sees_the_locked_shape_for_cipher_rows` (backend) + `frontend/tests/householdSharing.test.ts` → "partner resolves with dek=null" | an owner row with `payee_cipher`/`amount_cipher` set serializes **identically** for owner and partner (`TransactionPublic`), and the partner's resolve helpers return `🔒` — even when the partner's *own* vault is unlocked (their DEK must never be passed). Owner-unlocked and owner-locked both satisfy "partner ≤ owner-locked" |
| `test_no_key_material_in_any_partner_payload` | no `/partner/*` body contains `wrapped_blob`, `vault_envelopes`, or any field matching `/(dek\|kek\|wrapped\|passphrase\|index_key)/i`; and `GET /vault/envelopes?device_id=…` under the partner's own (valid) bearer returns only rows with `user_id = <partner id>` — the owner's envelope id, `device_id` and `wrapped_blob` appear nowhere (a valid session is not a 401 here; the invariant being pinned is scoping) |
| `test_owner_privacy_mode_seals_invite_email_and_partner_egress` (§9) | owner `privacy_mode=true`, partner `privacy_mode=false`: invite → `201` with `email_sent: false` **and the mailer spy asserted not called**; a full sweep of the five partner routes → 200s with **zero outbound HTTP** (spied transport); and since M93 W2 the sweep differs from its privacy-off twin exactly by the mask — `accounts`/`transactions`/`budgets` differ with the seeded amount gone from the privacy-on bytes, `categories` (no money) byte-identical, `state` differing only in `privacy_mode` |
| `test_privacy_mode_masks_every_partner_money_field` (M93 W2, replaces the old Reading B honesty test) | owner privacy on → every money field `null` on `/partner/{accounts,transactions,budgets}`; a byte-scan of the whole sweep for the seeded amounts (`-1234.56`, `500.00`) finds nothing; budget rows keep `id`/`category_name`/`period` with `amount: null`; `state.privacy_mode` is `true`; the **owner's own** `/accounts`, `/transactions`, `/budgets` still carry real numbers; privacy off puts every figure back. Frontend twin in `frontend/tests/householdSharing.test.ts` → "partner privacy mode": banner, `🔒` in every figure slot, no budget bars |
| `test_partner_carried_in_is_money_and_follows_the_privacy_mask` (M110, docs/09 §Budget carry) | an owner budget with `carry: forward` and a `-37.50` rollover: privacy ON → `/partner/budgets` keeps `carry: "forward"` with `amount: null` AND `carried_in: null`, and neither `500.00` nor `37.50` appears in the privacy-on bytes; privacy OFF → `carried_in: "-37.50"`, and the OWNER's own `GET /budgets` prints the same figure (one producer: `services.budgets.carry_in`) |
| `test_demo_tenant_cannot_invite_and_cannot_be_invited` | demo `POST /sharing/invites` → `403 demo_account_restricted`; owner inviting `demo@fintracker.local` → `403 demo_account_restricted`; demo `POST …/accept` → `403`; demo link rows == 0 after both attempts; `GET /partner/state` as demo → `200 {linked: false}` (M92); `GET /partner/{accounts,transactions,categories,budgets}` as demo → `401 sharing_not_linked` |
| `test_partner_state_contract` (M92) | unauthenticated → `401 invalid_token`; authenticated + never-linked → `200 {linked: false}` and **no other key**; linked → `200 {linked: true, owner, account_ids, privacy_mode, link_id, accepted_at}` (the pre-M92 payload plus `linked: true`); revoked → back to `200 {linked: false}` while `/partner/accounts` still answers `401 sharing_not_linked` |
| `test_every_sharing_query_is_scoped_by_the_join` (**the escalation guard**) | a `before_cursor_execute` listener records every SQL statement during a sweep of all five partner routes and asserts: each SELECT on a domain table binds `user_id = <owner id>` (never the partner's), and each SELECT on an account-scoped table also carries `account_id IN (<allowlist>)`. Any statement failing either assertion fails the test — that is the "escalation, not a design choice" tripwire |
| `test_partner_payload_contains_no_hidden_and_no_own_rows` | owner seeds `A` (shared), `B` (hidden); partner seeds `C` (their own); every partner route is swept and no `B`/`C` id, amount or description appears anywhere |
| `test_only_the_sharing_modules_import_the_link_table` | static scan of `backend/app/**`: `SharingLink` is referenced only by `models/sharing.py`, `schemas/sharing.py`, `api/sharing.py`, `api/partner.py` and their tests — proof that **no pre-existing route gained a cross-tenant path** |
| `test_partner_router_is_get_only` + `test_partner_surface_writes_no_domain_row` | the router exposes only `GET`; sweeping it leaves every domain table's row count unchanged (the accept mutation lives on the sharing router) |
| `test_invite_rate_limit` (`limiter.enabled = True`, `test_import_dos.py` pattern) | 6th `POST /sharing/invites` inside a minute → `429 {"detail": "Too many attempts", "code": "rate_limited"}`; a 6th outstanding pending invite → `400 sharing_invite_cap` |
| `test_single_use_token_and_address_privacy` | replay of an accepted token → `409 sharing_invite_used`; accept with a token owned by another address while signed in as a third → `401 sharing_invite_invalid` (**identical body** to an unknown token); `GET /sharing/invites` never contains `token`/`accept_url` |
| `test_accept_race_is_decided_by_the_database` (**PARTNER seat arm**) | two owners' invites accepted by one user concurrently → exactly one `200`, one `409 sharing_partner_occupied`, and exactly one `active` row for that partner — refused by the partial unique index, not by a pre-check |
| `test_owner_side_accept_race_loser_gets_the_owner_attributed_code` (**OWNER seat arm**, M98 F-6) | two invites of ONE owner accepted concurrently → the winner takes the seat, the loser gets **`409 sharing_invite_used`** (owner-attributed: the owner's seat was consumed — never `sharing_partner_occupied`), the receipt is `actor = invitee`, `invite_id = loser's invite`, `response_code = 409`, exactly one `active` row survives; the sequential path still answers `409 sharing_partner_exists` |
| `test_owner_state_scope_and_revoke_target_the_active_row_not_the_newest` (M98 F-1) | two invites, the OLDER accepted → `GET /sharing` reads the **active** row (`status: "active"`, `link_id` = the accepted one), `PATCH /scope` writes that row (asserted through the partner's own `/partner/accounts`), `POST /revoke` → **200** (not 409) and `/partner/state` → `{linked: false}`, the repeat → 200 on the same row, and the stray pending invite is still cancellable **by its own id**; every `revoke` receipt names the row actually revoked |
| `test_cancel_pending_then_revoke_active_both_succeed_in_sequence` (M98 F-1) | cancel the newer pending row → 204, `GET /sharing` still reads the active link, `POST /revoke` → **200** (was 409 forever after the cancel), partner goes `{linked: false}` |
| `test_owner_scope_and_revoke_without_a_live_link_join_the_not_linked_family` (M98 F-1) | pending-invite-only owner and never-linked owner both get **404 `sharing_not_linked`** from `PATCH /scope` and `POST /revoke` with a body **byte-identical** to a partner data read's 401 envelope; the two owner-side refusals still write receipts with `invite_id = null` |
| `test_partner_leave_targets_the_active_row_not_the_newest` (M98 F-2) | partner row that sorts NEWER than the live one → `leave` ends the **active** row for real (`/partner/state` → `{linked: false}`, was `linked: true` after a 200), the response and the receipt under the row's OWN owner both name that row, and the M79 repeat answers 200 on the partnership that ended last |
| `test_sharing_audit_matches_the_mcp_audit_shape` | column-for-column parity check against `McpAudit` (`args_hash` 64-hex, `response_code`, `duration_ms`, append-only), **handler-level** refusals included and **pre-handler refusals (429 limiter / 403 demo guard / 422 validation) leaving no row** (docs/43 §3), best-effort (a forced audit-insert failure still returns the invite) |
| `test_export_registry_picks_up_both_tables` / demo-reset classification | `GET /export/full` keys include `sharing_links` and `sharing_audit`; `test_demo_seed.py::test_every_user_owned_table_is_classified_for_demo_reset` green |
| `test_partner_can_leave_and_both_sides_render_the_ended_state` | `POST /sharing/leave` → **200** `SharingState{status: "revoked", revoked_at}` with the row landing in exactly the state `revoke` writes, one `leave` receipt (`actor_user_id = partner`, `user_id = owner`, `invite_id = link`), the owner's next `GET /sharing` reading `revoked`, the partner's next `GET /partner/state` → **200 `{linked: false}`** (M92) with a data route → `401 sharing_not_linked`, while their OWN session still answers 200, and a repeat → **200** again with a second receipt (idempotent, M79) |
| `test_leave_by_a_non_partner_answers_the_uniform_not_linked_envelope` | a never-linked stranger AND the never-accepting invitee both → **404** with `content` byte-identical to a partner DATA read's 401 envelope (`/partner/accounts`; `/state` deliberately stopped producing it in M92), **zero** receipts written (no owner to attribute), live household untouched |
| `test_leave_mutates_only_the_partner_row_and_reads_distinctly_from_revoke` | a user who is simultaneously an owner keeps their own active link and the third tenant re-verifies 200 on the next request; the owner's log ends up carrying BOTH `leave (actor = partner)` and `revoke (actor = owner)` — distinct rows, distinct actors, same terminal state |
| `test_leave_shares_the_mutation_rate_limiter` | 11th `POST /sharing/leave` inside a minute → `429 {"detail": "Too many attempts", "code": "rate_limited"}` (`SHARING_MUTATE_LIMIT` = 10/min, the invitee's own limiter) |
| `test_partner_projection_boundary` (M106, docs/02 §transaction_events) | ONE visible row carrying four receipts — system (`actor IS NULL`), owner, partner, third party — and `GET /partner/transactions/{id}/events` returns exactly the owner's and the partner's, newest first (`["categorized", "edited"]`, `total == len(items)`): "partner did X" renders from the partner's own chair (`"you set category Groceries"`), "owner did X" names them (`"Owner Mark edited amount"`, `changed_fields == ["amount_native"]`). **No `payload` key on any item**, and none of `-88.88` (the third party's), `-12.34` (the owner's), `-99.99` (the sync row's) or the string `sync` appears anywhere in the response bytes. The owner's OWN route still shows all four with payloads. A row outside scope → `404 not_found`, a never-linked caller (the same third party) → `401 sharing_not_linked` |
| tenancy regression | `GET /transactions`, `GET /accounts`, `GET /budgets` for both users are **byte-identical to the pre-M90 baseline** — the sharing work added no predicate to any existing query |
| `test_view_only_partner_is_refused_by_every_mutation_surface` (Kilter wave, t_09492311 — role enforcement) | the view-only partner aiming at the OWNER's rows through every mutation surface gets a 4xx with the `{detail, code}` envelope, never a 2xx: 15 attempts spanning `PATCH`/`DELETE` on the owner's shared **and** hidden transaction/account/budget/category rows, creates addressed at the owner's account/category (`POST /transactions`, `POST /budgets`), and the owner's sharing seat (`PATCH /sharing/scope`, `POST /sharing/revoke` → 404 `sharing_not_linked`, `DELETE /sharing/invites/{stray}` → 404 `sharing_missing`); afterwards the owner's four list endpoints plus `GET /sharing` are **byte-identical** to the pre-attempt baseline, domain row counts and the link-row count are unchanged, exactly one `active` link survives naming the partner, and `GET /partner/state` still answers `200 {linked: true}` (refusals ≠ logout) |
| `test_two_households_never_leak_into_each_other` (Kilter wave, t_09492311 — cross-household isolation) | two complete households (owner+partner each, one shared account apiece) plus the first partner's own private rows: neither partner's five-route sweep, and neither owner's `GET /transactions`, contains the other household's account name, merchant, amount or ids; each `GET /partner/state` names its own owner; the allowlists hold id-for-id (`[account1]` / `[account2]`, partner-1 transactions exactly `{tx1}`); and a cross-household `PATCH /transactions/{tx2}` / `DELETE /accounts/{account2}` from partner 1 answers **404** with the target row surviving unchanged in owner 2's list |

### 3. Gates (AGENTS §4, in order)

1. Doc reconciliation: this doc + `docs/10` (spec commit); `docs/02` + `docs/08`
   + `docs/09` + `docs/11` (implementation commit).
2. `cd backend && uv run ruff format --check && uv run ruff check`.
3. `cd backend && uv run pytest -q` green against real Postgres (never SQLite);
   `alembic upgrade head` clean on a fresh database.
4. `cd frontend && npx tsc --noEmit && npx eslint . --max-warnings 0`,
   `NODE_ENV=test npx vitest run`, `npm run build`.
5. `docker compose up -d --build` then smoke through **`http://localhost:8080`**,
   never TestClient alone.
6. Secret scan (`git ls-files | grep -iE '\.env$|secret|token|pem'` empty), one
   commit per wave, push to `origin/main`.

### 4. Running-stack smoke (through `http://localhost:8080`)

1. Register `owner@…`, create accounts **A** (shared) and **B** (hidden) with
   transactions; Settings → Sharing → invite `partner@…` with only **A**
   selected → `201`, link copied, "email not sent" honest line shown.
2. Register `partner@…` in a second browser profile, open the copied
   `/sharing/accept?token=…` → confirm → land on `/shared`.
3. `/shared` shows **A**'s balance and rows and **no trace of B**; budgets
   there are labelled "shared accounts only"; the partner's sidebar has no
   write controls.
4. Owner removes **A** from the scope → partner's next refresh shows the empty
   projection immediately (no logout, no cache wait).
5. Owner **Revoke** → partner's next request renders "No shared household —
   access was removed" and their own session still works (they are not bounced
   to `/login`).
6. Owner's `GET /sharing/audit` lists invite → accept → scope → revoke with
   actor ids; `GET /export/full` includes both new tables.
7. Repeat 1 with the demo tenant → `403 demo_account_restricted`, and inviting
   the demo address → `403 demo_account_restricted`.
