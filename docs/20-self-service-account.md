# 20 — Self-Service Account Management

Self-service endpoints for a signed-in user to change their password, edit their
profile (display name + email), and delete their account. All three live under
the `users` router, which is mounted at `/api/v1` (so the full path for every
route below is `/api/v1/...`). The demo tenant
(`demo@fintracker.local`) is blocked from all three by a shared guard. See plan
`local://doc-reconciliation-plan.md` §3 (extras).

> **M74 split:** second-factor management (TOTP enrollment/disable, recovery
> codes, passkeys) and the SSO flow live under the `/auth` router and are
> specified in **docs/38** — this doc keeps password/profile/delete only. The
> Settings UI mounts both cards (`AccountSection` here, `SecuritySection` in
> docs/38), and account deletion wipes the second-factor rows too (Cascade
> order below, items 26).

## Context

Before this feature, only the server (register / admin) could mutate account
credentials. Users needed a self-service surface in Settings to:

- **Change password** — re-hash, log out every other device, and keep the
  current tab signed in by re-issuing a fresh token pair.
- **Edit profile** — set an optional display `name` and change `email`, where an
  email change requires re-authentication via `current_password` and is rejected
  on collision.
- **Delete account** — atomically purge every user-owned row (in FK-safe child→
  parent order) in a single action.

Decisions (locked with code):

- **Demo is immutable for self-service mutations; `POST /auth/demo/reset` is the sanctioned restore.** `POST /auth/demo` **seeds the demo tenant on first use and is idempotent thereafter — it does NOT reset existing demo data**: `ensure_demo_account` returns early once the demo user has at least one transaction, so later logins keep whatever the previous session left. Demo writes are blocked in `app/api/deps.py::_assert_demo_mutation_allowed`, which raises `demo_account_restricted` (403) for **every** `POST`/`PUT`/`PATCH`/`DELETE` on the demo tenant that depends on `get_current_user` — that is a global guard in the auth dependency, not four `_assert_not_demo` call sites. Reads (`GET`/`HEAD`/`OPTIONS`) stay open, since reading the demo is its purpose. **Six deliberate exemptions** (exact paths in `app/api/deps.py::DEMO_MUTATION_EXEMPT_PATHS`, one path-prefix set in `DEMO_MUTATION_EXEMPT_PREFIXES`, plus one route outside the dependency):
  1. `POST /auth/demo` itself — it is outside `get_current_user` entirely, so the guard never sees it.
  2. `POST /acb/rebuild` — recomputes the demo user's own derived `acb_pools` cache from that user's own trades; it can touch no seeded transaction, trade, price or balance.
  3. `POST /auth/demo/reset` — the demo tenant's own recovery path (M50, below). It keeps every part of the boundary's intent: the caller must be authenticated **as the demo user** (any other tenant → 403 `forbidden`), it is rate-limited 5/min exactly like `/auth/demo`, and the only thing it can produce is the deterministic M10 seed — a visitor cannot set a value everyone else then sees, they can only restore the published dataset. Without it, any future gap in the guard is permanent damage (a manual psql wipe was the only recovery).
  4. `POST /auth/mcp-tokens` (M81, docs/40 §1) — minting the demo tenant's OWN MCP credential row: it changes no seeded transaction, trade, price or balance, only a token that maps to the demo user. The handler forces the policy to `read_only` and refuses an explicit `"full"` with `422 demo_mcp_read_only`, so the exemption cannot mint a demo write capability.
  5. `DELETE /auth/mcp-tokens/{id}` (M81) — revoke, matched by **path prefix** (`DEMO_MUTATION_EXEMPT_PREFIXES = ("/api/v1/auth/mcp-tokens/",)`) because the id segment makes it a different path from the mint. Exemption 4 without this one would let a visitor fill `MCP_TOKEN_ACTIVE_CAP` (10) with tokens no visitor can delete for 90 days. Both routes still authenticate as the demo user, stay rate-limited, and are the only two MCP mutations the guard lets through.
  6. `POST /projections/run` (M157, docs/12 §7) — the projection calculator, and the first exemption that touches **no row at all**: the handler takes no `Session`, `services/projections.py::project_scenario` is a pure function of the request body, and the response is a deterministic function of the caller's own input, so one visitor's run is invisible to the next. It is here because the guard fires on the METHOD, and this method does not mutate. Blocking it is what pushed the whole projection run into the browser (M61 #2), which M135 §2 K-12 classifies as a *demo write-guard artifact* rather than an E2EE one; M157 restored the route as the Reports calculator's source of truth for every tenant, the demo included.

  The Settings UI also disables the form for `isDemo`.
- **The demo's import teach-path is the `first-import` walkthrough, not a live upload** (M92): `POST /imports/upload` sits under the same guard, so `/help`'s Track 1 links to `/help/first-import` (screenshots of the real flow) instead of `/imports` and says in copy that the demo cannot upload. Do not "fix" the resulting `403` by exempting uploads — the guard is what keeps the published demo dataset intact; the walkthrough keeps the teach moment inside the demo (docs/32 § "M89 learning path").
- **Password length is bounded at the bcrypt boundary.** Pydantic accepts
  `new_password` up to 128 chars, but bcrypt silently truncates at 72 **bytes**;
  the endpoint rejects `> 72 bytes` with `password_too_long` (422) before hashing
  (mirrors register).
- **`llm_api_key` is write-only and encrypted at rest.** It is never returned by
  any read (`UserPublic` omits it); on write it is encrypted with
  `encrypt_secret` (Fernet, key derived from `JWT_SECRET`).
- **Privacy + cloud is rejected on the settings PATCH, not on password change.**
  `PATCH /users/me` (the generic settings endpoint, `update_me`) raises
  `privacy_mode_requires_local_llm` (409) when `privacy_mode=true` would combine
  with a cloud `llm_provider`. `POST /users/me/change-password` does **not** apply
  this check (see Discrepancy note at end).

## Approach

### Routes (backend)

All routes require `get_current_user`. Errors use the `{detail, code}` envelope
(docs/08).

#### `POST /api/v1/users/me/change-password`

Re-hash the password, revoke all other sessions, re-issue a fresh pair.

- **Request** — `ChangePasswordRequest` (`backend/app/schemas/users.py`):
  ```python
  class ChangePasswordRequest(BaseModel):
      current_password: str                       # required
      new_password: str = Field(min_length=8, max_length=128)
  ```
- **Behavior** (`backend/app/api/users.py:change_password`):
  1. `_assert_not_demo(current_user)` → 403 `demo_account_restricted`.
  2. `verify_password(current_password, hashed_password)` else 401
     `invalid_credentials`.
  3. `len(new_password.encode()) > 72` → 422 `password_too_long`.
  4. `hashed_password = hash_password(new_password)`.
  5. `DELETE RefreshToken WHERE user_id = current_user.id` (logs out all other
     devices).
  6. `commit()`; `_issue_pair(db, current_user)` returns a new pair.
- **Response** — `TokenResponse`
  (`backend/app/schemas/auth.py`), `200`:
  ```python
  class TokenResponse(BaseModel):
      user: UserPublic          # see models below
      access_token: str
      # refresh_token: delivered ONLY as an httpOnly cookie (audit M1)
  ```
- **Errors:** `401 invalid_credentials`, `422 password_too_long`,
  `403 demo_account_restricted`.

#### `PATCH /api/v1/users/me/profile`

Edit `name` and/or `email`. Email change requires `current_password` and is
unique-checked.

- **Request** — `ProfileUpdate`:
  ```python
  class ProfileUpdate(BaseModel):
      name: str | None = None       # optional display name
      email: EmailStr | None = None # optional
      current_password: str | None = None  # required ONLY when email changes
  ```
- **Behavior** (`backend/app/api/users.py:update_profile`):
  1. `_assert_not_demo(current_user)` → 403 `demo_account_restricted`.
  2. If `email is not None and email != current_user.email`:
     - `current_password is None` → 401 `current_password_required`.
     - `verify_password(current_password, ...)` fails → 401 `invalid_credentials`.
     - `SELECT User WHERE email == payload.email` exists → 409 `duplicate_email`.
     - else assign `current_user.email`.
  3. If `name is not None`: assign `current_user.name`.
  4. `commit()`; return `current_user`.
- **Response** — `UserPublic`, `200`.
- **Errors:** `401 current_password_required`, `401 invalid_credentials`,
  `409 duplicate_email`, `403 demo_account_restricted`. (Pydantic field
  validation also yields `422 validation_error` for a malformed `email`.)

#### `DELETE /api/v1/users/me`

Atomically purge the account and all owned data.

- **Request** — `DeleteAccountRequest`:
  ```python
  class DeleteAccountRequest(BaseModel):
      current_password: str        # required
  ```
- **Behavior** (`backend/app/api/users.py:delete_account`):
  1. `_assert_not_demo(current_user)` → 403 `demo_account_restricted`.
  2. `verify_password(current_password, ...)` fails → 401 `invalid_credentials`.
  3. `_delete_user_data(db, current_user)` — iterates `_USER_OWNED_MODELS` in
     child→parent order (below), `DELETE ... WHERE user_id == user.id`, then
     `db.delete(user)`; `commit()`.
- **Response** — `204 No Content`.
- **Errors:** `401 invalid_credentials`, `403 demo_account_restricted`.

#### `POST /api/v1/auth/demo/reset` (M50 — demo tenant only)

Wipe the demo tenant's data and re-run the M10 seed, in **one transaction**.
Mounted on the `auth` router (`backend/app/api/auth.py:demo_reset`), so the full
path is `/api/v1/auth/demo/reset`.

- **Request** — no body. Requires `Authorization: Bearer <access token>` via
  `get_current_user`, i.e. the caller must be signed in **as
  `demo@fintracker.local`** (the demo token from `POST /auth/demo`).
- **Behavior** (`backend/app/services/demo_seed.py:reset_demo_tenant`):
  1. `SEED_DEMO_ACCOUNT=false` → 404 `demo_disabled` (checked before anything
     else, exactly like `POST /auth/demo`).
  2. Caller is not the demo user → 403 `forbidden` (a second, independent
     check inside the handler — the shared guard only knows "is this the demo
     tenant", not "may this tenant reset itself").
  3. `delete_user_rows(db, demo_user, keep=(RefreshToken, McpToken))` — every
     user-owned row in the FK-safe child→parent order of `_USER_OWNED_MODELS`
     (the cascade table above), **except** the credentials `refresh_tokens` and
     `mcp_tokens` and the `users` row itself. Junction rows (`rule_tags`,
     `transaction_tags`, no `user_id` of their own) are cleared by subquery
     first, as in `_delete_user_data`.
  4. Every model in `_RESET_EXTRA_MODELS` is deleted by `user_id` in the same
     transaction (M63/M63b): `backup_artifacts` (a restore must keep its own
     source, docs/11 — that reason does not apply to a reset) and
     `vault_envelopes` (device enrolment: the demo tenant can never enrol a
     server-side device, docs/33, so a stray row is bug state that must not
     outlive the reset). Both sit outside `_USER_OWNED_MODELS` and neither can
     be reached through the FK cascade, because step 3 keeps the `users` row —
     `seed_fingerprint` reads exactly this set (docs/30), so the M50
     fingerprint-equality acceptance sees them too.
  5. The ten `is_system` default categories are re-created (they were wiped in
     step 3), then `ensure_demo_account` runs the full M10 seed — the user
     exists and has **no** transactions, so it takes the cold path and commits
     once at the end.
  6. Any exception rolls the whole thing back: the wipe and the re-seed share
     one transaction, so there is no state in which the demo is half-wiped
     (a partial wipe would be worse than no wipe).
- **Response** — `204 No Content`. The caller's own session is untouched
  (same `user_id`, refresh tokens kept), so the client just refetches.
- **Errors:** `404 demo_disabled`, `403 forbidden` (non-demo caller),
  `401 invalid_token` (no/expired token), `429 rate_limited` (**5/minute** per
  client — same bucket size as `POST /auth/demo`).
- **Not touched:** `fx_rates` (the one shared, `user_id`-less cache, docs/07
  H7), every other tenant, and `DEMO_PASSWORD` / `SEED_DEMO_ACCOUNT` themselves.
- **Guard exemption:** the route depends on `get_current_user`, so it would be
  blocked like any other demo write; `/api/v1/auth/demo/reset` is therefore
  exemption 3 documented above and pinned in `DEMO_MUTATION_EXEMPT_PATHS`. The
  exemptions are enumerated exact paths (plus one enumerated prefix) — adding a
  route never inherits one.
- **Restore proof:** `seed_fingerprint(db, demo_user_id)` (docs/30) hashes the
  demo's content using natural keys only (no serial ids, no timestamps), so
  `fingerprint(after reset) == fingerprint(fresh seed)` is the acceptance
  property (M10 / M50).

### Models

`backend/app/models/users.py` — `User` (table `users`):

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `email` | str (index, unique `uq_users_email`) | |
| `hashed_password` | str | bcrypt, cost ≥ 12 |
| `is_admin` | bool | default False |
| `name` | str \| None | `max_length=255`, optional display name; null until set |
| `privacy_mode` | bool | default False |
| `currency_tracker_enabled` | bool | default True |
| `llm_provider` | `LlmProvider` enum | `ollama` \| `openai_compatible`, default `ollama` |
| `llm_base_url` | str \| None | |
| `llm_model` | str \| None | |
| `llm_api_key` | str \| None | write-only; encrypted at rest via `encrypt_secret` |

`LlmProvider` (`enum.StrEnum`): `ollama`, `openai_compatible`.

`UserPublic` (`backend/app/schemas/users.py`) — the serialized user; **omits**
`hashed_password` and `llm_api_key`: `id`, `email`, `name`, `privacy_mode`,
`currency_tracker_enabled`, `llm_provider`, `llm_base_url`, `llm_model`.

### Cascade order (delete)

`_USER_OWNED_MODELS` is iterated child rows before parents so nothing orphans;
the loop lives in `backend/app/api/users.py:delete_user_rows(db, user, keep=())`
and `_delete_user_data` adds `db.delete(user)` after it, so `user` is deleted
last (order updated by M47, docs/29 F3 — new-table rows and
the tag junctions were previously missing and blocked the delete with FK
errors; the list is FK-safe, i.e. every referencing child is deleted before
the parent row it points at — note `Budget` precedes its `Category` parent,
`ReconciliationItem` precedes `Transaction`/`Account`, and `Transaction`
precedes `ImportBatch` because `transactions.import_batch_id` references the
batch — the last one was caught by the M47 smoke test):

1. `LlmSuggestion`
2. `LearnedPayee`
3. `Trade`
4. `AcbPool`
5. `Tag` (junctions `RuleTag`/`TransactionTag` are cleared by subquery on the
   user's rules/transactions BEFORE the loop — junctions carry no `user_id`)
6. `ReconciliationItem` (references `Account` + `Transaction`)
7. `Transaction` (references `ImportBatch` — before it)
8. `ImportBatch`
9. `EtfHolding`
10. `DividendHistory` (M184 raw provider events; references `Security`)
11. `DividendHistoryRefresh` (M184 refresh metadata; references `Security`)
12. `Fundamentals`
13. `Price`
14. `WatchlistItem`
15. `Rule`
16. `SpendingAlert` (references `Budget`)
17. `Budget` (references `Category`)
18. `BillReminder` (references `Category`)
19. `TaxProjection`
20. `NotificationSetting`
21. `Category`
22. `Goal` / `Debt` (reference `Account` — deleted before `Account`)
23. `McpToken`
24. `Account`
25. `Security`
26. `Institution`
27. `RefreshToken`
28. `TotpFactor` / `RecoveryCode` / `WebAuthnCredential` / `OidcLink` (M74
   second-factor rows, docs/38 — same treatment as the other credential
   tables: a deleted account leaves no orphaned factors or provider links)
29. `User` (deleted explicitly after the loop)

> New user-owned tables MUST be added here in foreign-key-safe delete order (before their referenced parents); otherwise their rows can orphan on delete.

### Services / helpers

- `backend/app/api/users.py` — `change_password`, `update_profile`,
  `delete_account`, `delete_user_rows`, `_delete_user_data`,
  `_assert_not_demo`, `_USER_OWNED_MODELS`. `_issue_pair` lives in
  `backend/app/api/auth.py`.
- `backend/app/api/auth.py` — `demo_reset` (`POST /auth/demo/reset`, rate
  limit 5/minute, 204).
- `backend/app/api/deps.py` — `DEMO_MUTATION_EXEMPT_PATHS` +
  `_assert_demo_mutation_allowed` (the shared guard and its exact-path
  exemptions).
- `backend/app/services/demo_seed.py` — `DEMO_EMAIL`, `reset_demo_tenant`,
  `seed_fingerprint`.
- `backend/app/core/security.py` — `verify_password`, `hash_password`
  (bcrypt, `_BCRYPT_ROUNDS = 12`).
- `backend/app/core/crypto.py` — `encrypt_secret` / `decrypt_secret` (Fernet,
  key = `urlsafe_b64encode(sha256(JWT_SECRET))`); `decrypt_secret` returns
  legacy plaintext unchanged on `InvalidToken`.

### Frontend

`frontend/src/api/users.ts`:

```ts
export interface ProfileUpdateInput { name?: string; email?: string; current_password?: string; }
export interface ChangePasswordInput { current_password: string; new_password: string; }
export interface DeleteAccountInput { current_password: string; }

export async function updateProfile(input: ProfileUpdateInput): Promise<User>;
export async function changePassword(input: ChangePasswordInput): Promise<AuthResponse>;
export async function deleteAccount(input: DeleteAccountInput): Promise<void>;
```

`frontend/src/types/index.ts` — `User` includes `name?: string | null` and
`currency_tracker_enabled: boolean` (mirrors `UserPublic`; never carries
`hashed_password` / `llm_api_key`). `AuthResponse` = `TokenPair` + `user`.

`frontend/src/pages/Settings.tsx` — `AccountSection`:
- Reads `me` query; `isDemo = user?.email === "demo@fintracker.local"` disables
  every input and button.
- **Profile:** sends `name` only when trimmed name changed; sends `email` +
  `current_password` only when email changed. On success: invalidates `me`,
  `setUser(updated)`, clears the email-password field, toasts "Profile updated".
- **Change password:** `canChangePassword = curPw>0 && newPw>=8 && newPw===confirmPw`.
  On success: `tokens.store(access, refresh)`, `setUser(res.user)`, clears
  fields, toasts "All other devices were signed out".
- **Delete:** revealed by a "Delete account" button; requires typing `DELETE` in
  a confirm field + current password. On success: `tokens.clear()`, toasts,
  `navigate("/login")`.
- Disabled (demo) state shows a notice that the form is reference-only.

## Touch points

- `backend/app/api/users.py` — `change_password`, `update_profile`,
  `delete_account`, `delete_user_rows`, `_delete_user_data`, `_assert_not_demo`,
  `_USER_OWNED_MODELS`.
- `backend/app/api/auth.py` — `demo_reset` (`POST /auth/demo/reset`).
- `backend/app/api/deps.py` — `DEMO_MUTATION_EXEMPT_PATHS`,
  `_assert_demo_mutation_allowed`.
- `backend/app/services/demo_seed.py` — `reset_demo_tenant`,
  `seed_fingerprint`, `_add_default_categories`.
- `backend/tests/test_demo_seed.py` — reset + fingerprint contract tests.
- `backend/tests/test_auth_security_hardening.py` — guard regression rows
  (budgets, bills) + exemption-scope pin.
- `backend/app/schemas/users.py` — `ChangePasswordRequest`, `ProfileUpdate`,
  `DeleteAccountRequest`, `UserPublic`.
- `backend/app/schemas/auth.py` — `TokenResponse`.
- `backend/app/models/users.py` — `User`, `LlmProvider`.
- `backend/app/core/security.py` — `verify_password`, `hash_password`.
- `backend/app/core/crypto.py` — `encrypt_secret`, `decrypt_secret`.
- `frontend/src/api/users.ts` — inputs + `updateProfile` / `changePassword` /
  `deleteAccount`.
- `frontend/src/types/index.ts` — `User`, `AuthResponse`.
- `frontend/src/pages/Settings.tsx` — `AccountSection`.

## Verification

- **Change password (happy path):** `POST /api/v1/users/me/change-password`
  `{current_password:"old", new_password:"newpass123"}` as a non-demo user →
  `200` with `TokenResponse` (`user`, `access_token`; the rotated refresh token arrives only as an httpOnly cookie — audit M1). The new
  `access_token` decodes to the same `sub`; all prior `RefreshToken` rows for the
  user are gone (other devices logged out).
- **Change password — bad current:** wrong `current_password` → `401
  invalid_credentials`.
- **Change password — too long:** `new_password` of 73 UTF-8 bytes (e.g. 73 ASCII
  chars) → `422 password_too_long`; 72 bytes (or ≤128 chars that encode ≤72
  bytes) accepted.
- **Change password — demo:** as `demo@fintracker.local` → `403
  demo_account_restricted`.
- **Profile — name only:** `PATCH /api/v1/users/me/profile` `{name:"Sam"}` →
  `200` `UserPublic` with `name:"Sam"`; email unchanged.
- **Profile — email requires re-auth:** `PATCH .../profile` `{email:"x@y.z"}`
  with no `current_password` → `401 current_password_required`. With correct
  `current_password` and a free address → `200`; `email` updated.
- **Profile — email collision:** `current_password` correct but `email` already
  registered → `409 duplicate_email`.
- **Profile — demo:** as demo → `403 demo_account_restricted`.
- **Delete (happy path):** `DELETE /api/v1/users/me`
  `{current_password:"correct"}` → `204`. Subsequent `GET /api/v1/users/me` with
  the deleted user's token → `401`; a fresh inspect of the DB shows the user row
  and every `_USER_OWNED_MODELS` row for that `user_id` removed, in cascade order.
- **Delete — wrong password:** `401 invalid_credentials`. Delete as demo → `403
  demo_account_restricted`.
- **Frontend:** open Settings → Account card. Change password → inputs persist
  only when `newPw>=8 && match`; success toasts "All other devices were signed
  out" and the tab stays logged in. Edit email without current password → Save
  disabled; with it → profile refreshes. As demo user the whole card is disabled
  with the reference notice. Delete requires `DELETE` + password; success returns
  to `/login`.
- **Encryption at rest:** after any `llm_api_key` write (via `PATCH /users/me`),
  the `users.llm_api_key` column holds a Fernet token, not plaintext;
  `decrypt_secret` round-trips it.

- **Demo reset — fingerprint restore (M50):** capture
  `seed_fingerprint(db, demo_user_id)`, mutate the demo through the API
  (transactions + budgets + bills) → the fingerprint changes →
  `POST /api/v1/auth/demo/reset` with the demo token → `204`, and the
  fingerprint equals the captured value **byte-for-byte**; per-table row counts
  return to their seeded values.
- **Demo reset — disabled:** `SEED_DEMO_ACCOUNT=false` → `404 demo_disabled`
  (same code as `POST /auth/demo`).
- **Demo reset — foreign tenant:** any registered non-demo user → `403
  forbidden`; no token → `401 invalid_token`.
- **Demo reset — rate limit:** 6th call from one client inside a minute →
  `429 {"detail": "Too many attempts", "code": "rate_limited"}`.
- **Boundary not weakened:** `POST /transactions`, `POST /budgets`,
  `POST /bills` as the demo tenant → `403 demo_account_restricted` (the
  exemptions are enumerated, pinned by
  `DEMO_MUTATION_EXEMPT_PATHS == {"/api/v1/acb/rebuild", "/api/v1/auth/demo/reset", "/api/v1/auth/mcp-tokens", "/api/v1/projections/run"}`
  and `DEMO_MUTATION_EXEMPT_PREFIXES == ("/api/v1/auth/mcp-tokens/",)` in
  `tests/test_auth_security_hardening.py`; M81 added the MCP credential pair,
  docs/40 §1, and M157 added `/projections/run`, docs/12 §7 — the same file's
  `test_projections_run_is_the_only_new_exemption` runs a projection as the
  demo tenant and then refuses the same session on `POST /accounts`).

### Discrepancy note

The reconciliation plan asserted `change_password` raises
`privacy_mode_requires_local_llm` (409) for privacy+cloud. The current code does
**not**: `change_password` only guards demo + credentials + length. The
privacy+cloud check exists solely in `update_me` (`PATCH /users/me`, the generic
settings endpoint). This doc follows the code; `privacy_mode_requires_local_llm`
is documented under `docs/08` / `docs/11` for `PATCH /users/me`, not here. If the
intended contract was to also block cloud providers on password change, that is a
code gap (out of scope for this docs-only change).
