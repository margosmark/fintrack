# 08 — API Spec

REST routes under `/api/v1`, JWT bearer auth on everything except register/login, the second-factor challenge endpoints and the OIDC routes (docs/38). Grouped by resource; routers live in `backend/app/api/`.

Resources: auth, users, institutions, accounts, categories, transactions, imports, rules, suggestions, securities, trades, acb, reports, fx, prices, health, search, watchlist, tags, budgets, goals, bills, debts, notifications, reconciliation, tax_projection, tax_alerts, insights, spending_comparison, dividend_calendar, networth_snapshot. Rate limiting (per-route, `backend/app/core/ratelimit.py`) and the `health` router (`GET /api/v1/health`) are documented in docs/01.

## Auth

| Method/Path | Notes |
| --- | --- |
| `POST /auth/register` | **M87 (docs/42).** `{email, password}` → **200** `{status: "accepted", detail}` — the SAME body for a free and an already-taken address (byte-identical; a `201 + session` vs `409 duplicate_email` split was an account-existence oracle). No token, no cookie: a free address still gets the account + default categories (docs/02), so the recovery path is "sign in with the password you just chose"; a taken address changes nothing and pays the same `_dummy_bcrypt` cost as the fresh branch, so timing does not separate them. Still `422 validation_error` (bad email) / `422 password_too_long` (>72 bytes) / `429 rate_limited` (**5/min**) — none of which depend on the address. `409 duplicate_email` is retired *here* but unchanged on `PATCH /users/me/profile` |
| `POST /auth/login` | `{email, password}` -> token pair (unknown emails pay the same bcrypt cost as known ones, so timing does not leak account existence; M87 closed the same oracle on register, docs/42). **M74 (docs/38):** for an account with an active TOTP factor — never the demo tenant — the 200 body is instead `{challenge: "totp", challenge_token, expires_in: 300}` and NO session exists yet; the code goes to `/auth/login/totp` or `/auth/login/recovery` |
| `POST /auth/refresh` | NO request body — the refresh cookie only (a `refresh_token` in a JSON body is ignored, never honoured) -> NEW pair, cookie-only; presented token revoked (rotation, docs/02). Replaying a revoked token revokes ALL the user's refresh tokens, then 401 `invalid_or_revoked_refresh_token` (reuse detection) |
| `GET /users/me` | Current user incl. `privacy_mode`, `currency_tracker_enabled`, `llm_provider`, `llm_base_url`, `llm_model`, `encryption_required`, `dashboard_preset` (M97 — always one of `overview \| import_first \| budget_first`, default `overview`, so a client never reads "absent"), `saved_report_views` (M108 P2 / M123 — always present, `[]` when none, never absent; every element carries `kind` (`reports` default | `transactions`) so a client never has to infer which `state` contract it follows; docs/09 §Saved report views / §Create from search), `recurring_intent_tag_ids` (**M174** — always present, `[]` = the tag source is OFF, never absent; the caller's own `tags.id`s that mark recurring intent for the confirmed-subscription query, docs/09 §Tags that mark recurring); NEVER `llm_api_key` / `hashed_password` |
| `PATCH /users/me` | Partial settings update `{privacy_mode?, currency_tracker_enabled?, llm_provider?, llm_base_url?, llm_model?, llm_api_key?, encryption_required?, dashboard_preset?, saved_report_views?, recurring_intent_tag_ids?}` → 200 `UserPublic`. `dashboard_preset` (M97, docs/09 §Dashboard layout presets) is validated by the enum: an unknown value → `422 validation_error` and the row is unchanged. `saved_report_views` (M108 P2, docs/09 §Saved report views) is the WHOLE preset list in one write — `[]` clears, rename and delete are client-side list edits — and `app/schemas/users.py::SavedReportView` validates every element (`id` 8–64 of `[A-Za-z0-9_-]`, unique in the list; `title` 1–80 non-blank; `kind` ∈ `reports | transactions`, default `reports`; `tab` ∈ the seven `REPORT_TABS` when `kind=reports`, exactly `transactions` when `kind=transactions` — the two are cross-checked, so a preset can never claim one kind and carry the other's shape; `state` selected BY `kind`: `reports` = a closed 8-key report set with per-key types, `transactions` = the closed `{q, category, account, tag}` set — `q` non-blank ≤ 240 chars, `category` a digits id or `uncategorized`, `account` a digits id, `tag` a comma list of digits ids, every value non-blank; ≤ 8 keys, ≤ 50 presets): any violation → `422 validation_error` and the row is unchanged. **M174:** `recurring_intent_tag_ids` is likewise the WHOLE list in one write (`[]` = off) — validated on the RAW value before any coercion, so a string id (`["7"]`) is a `422 validation_error` rather than a silently absorbed `7`, and so is a non-positive id; at most 20 ids (`RECURRING_INTENT_MAX_TAGS`). `api/users.py::update_me` then checks OWNERSHIP, because the column is JSON and the database carries no FK: any id the caller does not own → `422 invalid_recurring_intent_tag_ids` with the offending ids named, and nothing is written. `llm_base_url` policy-checked at write time: http(s) only, loopback-or-public (private/link-local denied unless operator env `LLM_BASE_URL` host or `LLM_ALLOW_PRIVATE_HOSTS=true`; loopback-only under privacy) → `422 invalid_llm_base_url`. Demo tenant → `403 demo_account_restricted` (M8). `privacy_mode=true` with `openai_compatible` → `409 privacy_mode_requires_local_llm`. Enabling privacy clears any stored cloud `llm_base_url` (docs/11 step 5). |
| `POST /users/me/llm-test` | Probe the configured (or candidate) LLM endpoint with one tiny chat completion; writes nothing. 200 `{ok, model, provider}` on success; error envelope otherwise: `400 llm_test_invalid_config` (provider could not be constructed), `502 llm_upstream_error` (ANY upstream failure — generic detail; status/URL logged server-side only), `422 invalid_llm_base_url` (candidate URL violates egress policy), `403 privacy_mode_blocks_external_api` (privacy blocks non-loopback at send). Accepts optional candidate `{llm_provider, llm_base_url, llm_model, llm_api_key}` to test before saving (docs/04). **M185 (docs/47 §4 W2):** the demo tenant's Settings control renders this probe DISABLED before dispatch with local copy and sends nothing; the route itself is unchanged and not exempt from the demo write guard. |
| `POST /users/me/change-password` | `{current_password, new_password (8–128)}` → 200 re-issues the token pair (refresh via cookie, no `refresh_token` in the body). Errors: `401 invalid_credentials`, `422 password_too_long` (>72 bytes), `403 demo_account_restricted`. |
| `PATCH /users/me/profile` | `{name?, email? (EmailStr), current_password?}` → 200. Changing `email` requires `current_password` (else `401 current_password_required` / `invalid_credentials`); `409 duplicate_email`. |
| `DELETE /users/me` | `{current_password}` → 204; `401 invalid_credentials`; `403 demo_account_restricted`. Cascade-deletes all user-owned rows (docs/20). |
| `POST /auth/logout` | NO request body — revokes the presented refresh token from the cookie; clears the cookie |
| `POST /auth/demo` | One-click demo login: idempotently seeds `demo@fintracker.local` with 12 months of synthetic data, returns the login token shape (refresh via cookie). 404 `demo_disabled` when `SEED_DEMO_ACCOUNT=false` |
| `POST /auth/demo/reset` | **M50 — demo tenant only.** Wipes the demo's domain rows in FK-safe child→parent order (docs/20 cascade; keeps `refresh_tokens`, `mcp_tokens` and the `users` row, and takes the tenant's `mcp_audit` receipts with it; never touches the shared `fx_rates`) and re-runs the M10 seed in ONE transaction → `seed_fingerprint` identical to a fresh seed (docs/30). No body; needs a Bearer token for `demo@fintracker.local`. → **204**. Errors: `404 demo_disabled` (`SEED_DEMO_ACCOUNT=false`), `403 forbidden` (any non-demo caller), `401 invalid_token`, `429 rate_limited` (**5/min**, same bucket size as `POST /auth/demo`). Third, documented exemption from the demo write guard (exact path in `DEMO_MUTATION_EXEMPT_PATHS`, docs/20) |
| `GET /auth/mcp-tokens` | List MCP tokens for current user (paginated, tokens never returned after creation; each item carries `policy`, `expires_at` + `last_used_at`) |
| `POST /auth/mcp-tokens` | Create MCP token: `{name, policy?}` → 201 `{id, name, token, policy, created_at, expires_at, last_used_at}` (token shown ONCE). `policy` is `full` (default), `read_only`, or `read_propose` (M189 — the BYO-AI connector tier, docs/59); anything else → `422 validation_error` (M80, docs/40). TTL **90 days** from creation. At most **10 active** (non-expired) tokens per user → `400 mcp_token_cap`; creation rate **10/min** → `429 rate_limited`. **M81 — demo tenant:** exempt from the demo write guard on this route only, and policy-FORCED: `policy` omitted → `read_only`; explicit `"full"` **or** `"read_propose"` → `422 demo_mcp_read_only` (`{"detail": "The demo tenant can only mint read-only MCP tokens", "code": "demo_mcp_read_only"}`) |
| `DELETE /auth/mcp-tokens/{id}` | Revoke MCP token (204). **M81:** the demo tenant is exempt from the demo write guard here too (path-prefix exemption — a visitor must be able to revoke what it minted), and nothing else changes |
| `POST /auth/validate-mcp-token` | Internal: validate MCP token via Bearer header → `{user_id, email, policy, token_id, is_demo}` (M80 added `policy`/`token_id`/`is_demo` — the MCP server derives its toolset filter from these three and never re-derives identity elsewhere; `is_demo` is `email == DEMO_EMAIL`, resolved server-side); stamps `last_used_at`. Errors: `401 missing_token`, `401 invalid_mcp_token`, `401 mcp_token_expired` (DISTINCT code: the token exists but is past `expires_at`), `429 rate_limited` (**120/min** per client — internet-routed validity+email oracle, M9) |
| `POST /auth/internal-issue-token` | Internal: `{user_id}` + `X-Internal-Secret` → `{access_token}` (MCP server only). Errors: `403 forbidden` (missing/wrong secret, `compare_digest` fail-closed), `404 not_found`, `429 rate_limited` (**120/min** per client, M9) |
| `POST /auth/internal-mcp-audit` | **M80, docs/40.** Internal: `{user_id, mcp_token_id?, tool, args_hash, response_code, duration_ms}` + `X-Internal-Secret` → **204** (MCP server only; one receipt per `tools/call`). `args_hash` must be 64 lowercase hex (`422 validation_error` otherwise). Errors: `403 forbidden` (missing/wrong secret, `compare_digest` fail-closed), `404 not_found` (unknown user — no orphan row), `429 rate_limited` (**120/min** per client, own bucket) |
| `POST /auth/login/totp` | **M74.** `{challenge_token, code}` → 200 token pair + refresh cookie (second factor for the password challenge). Errors: `401 invalid_or_expired_challenge`, `401 invalid_totp_code` (wrong, expired, replayed, or not 6 digits — one code for all four), `401 totp_not_enabled`, `429 rate_limited` (**10/min**) |
| `POST /auth/login/recovery` | **M74.** `{challenge_token, code}` → 200 pair + cookie; consumes exactly one recovery code (`401 invalid_recovery_code` when unknown or already used), the remaining codes stay valid |
| `GET /auth/totp/status` | **M74.** → `200 {enabled, pending}` (authenticated; readable by the demo tenant) |
| `POST /auth/totp/setup` | **M74.** → `200 {secret, otpauth_url}`: stores an ENCRYPTED pending secret and leaves any active factor untouched (re-enrollment is confirm-gated). Errors: `403 demo_account_restricted`, `429 rate_limited` (**5/min**) |
| `POST /auth/totp/confirm` | **M74.** `{code}` → `200 {recovery_codes: [×10]}` (shown ONCE): activates the pending secret and replaces the whole recovery-code set. Errors: `401 invalid_totp_code`, `409 totp_not_enrolled` (no pending secret), `429 rate_limited` (**5/min**) |
| `POST /auth/totp/disable` | **M74.** `{code}` (a TOTP code OR an unused recovery code) → 204; deletes the factor and its recovery codes. Errors: `401 invalid_totp_code`, `401 invalid_recovery_code`, `409 totp_not_enrolled`, `403 demo_account_restricted` |
| `GET /auth/passkeys` | **M74.** List → `200 {items: [{id, name, created_at, last_used_at, transports}], total}` |
| `POST /auth/passkeys/options` | **M74.** Registration options → `200 {challenge_token, options}` (rpID/origin from `APP_ORIGIN`; `excludeCredentials` pre-filled). **10/min** |
| `POST /auth/passkeys` | **M74.** `{challenge_token, credential, name?}` → `201 {id, name, created_at, transports}`. Errors: `422 invalid_passkey_response`, `400 passkey_cap` (10 live), `409 passkey_exists`, `403 demo_account_restricted` |
| `DELETE /auth/passkeys/{id}` | **M74.** → 204; `404 not_found` for a foreign or unknown id (never a 403 probe) |
| `POST /auth/passkeys/login/options` | **M74, M87 (docs/42), passwordless (docs/38).** `{email?}` → **200** `{challenge_token, options}` for EVERY input. **Email-first** (non-blank `email`): for an account with passkeys, an account without, or no account at all — the two keyless cases get a server-generated throwaway challenge (random descriptor, token bound to user id 0) so `allowCredentials` stays present and same-shaped; status and token are uniform for the three cases (the old `404 no_passkeys` split is retired), but the `allowCredentials[0].id` VALUE still discloses passkey enrollment (real DB-stable id vs per-request 32-byte throwaway — the accepted email-first residual, docs/42 §2), and the cases part company at `POST /auth/passkeys/login` (`401 invalid_or_expired_challenge` / `401 invalid_passkey`). **Passwordless** (absent/null/blank `email`): same 200 envelope with purpose `passkey_discoverable`, `allowCredentials: []`, `userVerification: "required"`, token bound to user id 0 — the account is resolved at the next step ONLY from the verified assertion. The `404 no_passkeys` code is retired from this route. **10/min** |
| `POST /auth/passkeys/login` | **M74, passwordless (docs/38).** `{challenge_token, credential}` → 200 pair + cookie — passwordless, and no TOTP step follows (the assertion is the second factor). Two purposes: `passkey_login` (email-first — account = token subject, credential must belong to it) and `passkey_discoverable` (account resolved from credential id + `userHandle` alone; UV required). A successful sign-in SPENDS the challenge (single-use; replay → `401 invalid_or_expired_challenge`). Errors: `401 invalid_passkey` (signature/origin/rpID/sign-count/unknown credential/`userHandle` mismatch), `401 invalid_or_expired_challenge` (expired, tampered, wrong purpose, or already-spent token) |
| `GET /auth/oidc/config` | **M74.** → `200 {enabled}` — always 200, so the login page needs no error path |
| `GET /auth/oidc/login` | **M74.** Browser navigation → `302` to the provider's authorize endpoint (`state` + `nonce` minted, bound in the httpOnly `oidc_state` cookie, `SameSite=Lax`). `404 oidc_disabled` when `OIDC_ENABLED=false`. **5/min** |
| `GET /auth/oidc/callback` | **M74.** Validates state, exchanges the code, verifies the ID token (JWKS signature, `iss`/`aud`/`exp`/`nonce`), matches the user by email → sets the httpOnly `oidc_pending` cookie and `302 {APP_ORIGIN}/login/oidc`. Failures redirect to `302 {APP_ORIGIN}/login/oidc?error=<code>` (`oidc_state_mismatch`, `oidc_provider_error`, `oidc_token_invalid`, `oidc_email_required`, `oidc_unknown_account`). **10/min** |
| `GET /auth/oidc/preview` | **M74.** Reads `oidc_pending` → `200 {email, linked}` (`linked` = the `(issuer, sub)` link already exists) |
| `POST /auth/oidc/confirm` | **M74.** Exchanges `oidc_pending` for a session — creates the `oidc_links` row if this is the first sign-in → 200 pair + cookie, issued by the SAME `_issue_pair`/`_set_refresh_cookie` as password login, then clears `oidc_pending`. Errors: `401 oidc_pending_expired`, `403 forbidden` (demo email), `404 oidc_disabled`, **5/min** |
| `GET /docs`, `GET /redoc`, `GET /openapi.json` | **M87 (docs/42)** — root paths, no `/api/v1` prefix. The API map, gated: `200` with a session access token or a LIVE MCP token (same expiry rule as `POST /auth/validate-mcp-token`), otherwise **401** `{"detail", "code": "docs_auth_required"}` — never a 500, even for a malformed bearer. `API_DOCS_PUBLIC=true` (local dev; unset in production) skips the gate. FastAPI's built-in docs routes are disabled (`docs_url=None`) so the dependency is the only way in |

**MCP receipts (M80, docs/40).** `GET /mcp-audit?limit=50&offset=0` (limit 1–200, offset 0–1,000,000; above it `422 validation_error`) → `200 {items: [{id, mcp_token_id, tool, args_hash, response_code, duration_ms, created_at}], total}`, newest first, **own rows only** (the `user_id` filter is on every query; there is no cross-account read). A receipt is written for every `tools/call` the MCP server handled, refusals included: `403` when the MCP layer refused it (`mcp_read_only` for a read-only token, `demo_account_restricted` for the demo tenant), the API's own status when the API rejected it, `400` when the failure came back as the house `{detail, code}` envelope, `500` for an unexpected failure, `200` otherwise. Recording is best effort — a failed receipt never changes the tool's result.

**Refresh delivery (audit M1/M2).** Every response that issues a pair (`register`, `login`, `demo`, `refresh`, `users/me/change-password`, and since M74 `login/totp`, `login/recovery`, `passkeys/login`, `oidc/confirm`) delivers the refresh token ONLY as a cookie: name `refresh_token`, `Path=/api/v1/auth`, `HttpOnly`, `SameSite=Strict`, `Secure` iff `COOKIE_SECURE` (default true). It never appears in a JSON body (`TokenResponse.refresh_token` is `exclude=True`) — same-origin script must not be able to read a 30-day credential from `/auth/refresh`. `refresh` and `logout` take NO request body at all: a `refresh_token` pasted into a JSON body is read by nobody (a body model would make FastAPI REQUIRE a body and break the cookie-only MCP client), so the cookie is the only channel in both directions (`api/auth.py::_refresh_token_from_cookie`). The cookie name is deliberately NOT `__Host-`-prefixed: the prefix would force `Path=/` (losing path scoping) and invalidate every existing session, and browser behavior on the http://localhost:5173 dev origin cannot be verified for all supported browsers — revisit at the next auth wave.

**Second-factor cookies (M74, docs/38).** Two extra cookies exist ONLY on the OIDC path and are scoped to `Path=/api/v1/auth`: `oidc_state` (`state|nonce`, set by `/auth/oidc/login`, read by `/auth/oidc/callback`) and `oidc_pending` (signed confirmation blob, set by the callback, read by `/auth/oidc/preview` + `/auth/oidc/confirm`, cleared on confirm). Both are `HttpOnly`, `SameSite=Lax` (Lax is required: the callback is a cross-site top-level navigation), `Secure` iff `COOKIE_SECURE`, 10-minute max-age. Neither carries a refresh token, neither is ever read by same-origin script, and no token of any kind appears in a URL.

## Core resources

Standard CRUD (`GET list`, `POST`, `GET/{id}`, `PATCH/{id}`, `DELETE/{id}` where sensible) for: `institutions`, `accounts`, `categories`, `rules`, `securities`, `trades`, `prices`.

Token payload everywhere: `{user, access_token, token_type: "bearer"}` plus the refresh cookie — `refresh_token` is never in a JSON body (Auth section above). TTLs and rotation: docs/01.

`DELETE /categories/{id}` returns `409 {code: "category_in_use"}` when the category is referenced by any transaction, child category, rule, `learned_payees` row, or `llm_suggestions` row — reassign first. (`is_system` rows remain undeletable, docs/02.)

`POST /prices` is an upsert keyed on `(security_id, date)`: it sets `price` + `source='manual'` even on an existing row — human data overwrites machine data; it clears `fetched_at` to NULL because the human edit is not a provider fetch. `PATCH /prices/{id}` also clears `fetched_at`. The yfinance backfill never touches manual rows (docs/02, docs/07). `GET /prices` and `GET /prices/history` serialize real fetch evidence as `price_fetched_at: datetime | null`; NULL means none.

`GET /prices/history` (M126) is the READ side of the same table for charts: one security, one window, bucketed server-side — see the Investments section below for the full contract. `GET /prices` stays the flat paginated list (docs/07 price table).

`POST /accounts` accepts optional `starting_balance` (Decimal, nullable). **M145 — `cash_balance` is KIND-dependent.** Cash accounts: committed LEDGER transactions (**split parents excluded** since M107 — their money lives in their children, docs/44 §2) + starting_balance. **Investment** accounts: the docs/05 §Portfolio value cash chain the account's own value chart draws (`services/account_value.account_cash` — starting_balance + committed transactions + Σ trade cash effects, per-row currency, M132), so the card and the chart cannot disagree (real account 459 records every dollar as a CASH-security trade with ZERO transactions — the ledger-only formula read `$0.00` against the chart's `$3,486.12`). `cash_balance` and `total_value` are `str | None`: `null` on an investment account means NO HONEST FIGURE (a non-zero foreign cash leg past the M115 ≤7d fx window) — never a fabricated zero, and the UI renders a dash. `tests/test_m145_account_cash_source.py` pins every clause.

**Rounding (resolved).** Every money string this payload prints — `cash_balance`, `investment_value`, `total_value`, `starting_balance`, and every `GET /accounts/ledger` `amount` / `running_balance` — goes through the single canonical helper `app.tax.acb.cents` at **`ROUND_HALF_UP`** (docs/02 Conventions). `_format_cash` previously called a bare `.quantize(_CENT)` with no `rounding=`, so it inherited the ambient decimal context (half-even) and an exact `.xx5` tie could render differently from every other money surface; that is now stated, not inherited. This is a deliberate behaviour change on an exact tie only: nothing documented half-even here, no client mirror recomputes these fields, and `tests/test_money_rounding_single_source.py` pins both the mode and the string-typed wire form.

**M113 — `pending_import_count: int` (default `0`) on `AccountPublic`.** How many of THIS account's `import_batches` are still `pending_review` — the account page's banner input (docs/09 §Account page pending-import banner). It is a COUNT and nothing else: no file name, batch id, status or row rides on it (E2EE row-count rule), and committed / discarded / reverted batches never count — `0` therefore covers "no batch" and "nothing pending" alike, deliberately. The count is computed on the **single-account** responses only (`GET /accounts/{id}`, `POST /accounts`, `PATCH /accounts/{id}` — `api/accounts.py::_pending_import_count`, scoped to the account's owner); `GET /accounts` and `GET /partner/accounts` do not run the query and return the `0` default, so the list route keeps its docs/34 budget (19 statements since M145's two batched cash legs) and the owner's staging state never crosses the sharing boundary. `tests/test_m113_pending_import_banner.py` pins every clause.

Pagination on every list route: `?limit=&offset=` (default 50, `limit` max 200) -> `{items, total, limit, offset}`. The shared pager (`deps.get_pagination` / `deps.paginate`, M98b F-4) also bounds **`offset` <= 1,000,000** (`deps.MAX_OFFSET`) -> above it `422 validation_error`, never a 500: an unbounded offset was rendered as a bigint `OFFSET >= 2**63` that Postgres rejects ("bigint out of range"). `GET /mcp-audit` and `GET /notifications/deliveries` declare their OWN query params instead of using `get_pagination`, so each repeats the same `le=MAX_OFFSET` bound itself (M98b F-4 tail) — they are NOT excluded from the ceiling. Filters AND-compose; bad params -> 422 in the error envelope below. One route is cursor-paginated instead: `GET /notifications/inbox` takes `?limit=&cursor=` and returns `{items, total, unread_count, limit, cursor, next_cursor}` — the union is re-derived per request, so a numeric offset would be unstable (docs/35).

### Account value history (M118)

`GET /accounts/{account_id}/value-history?from=&to=&granularity=day|week|month&min_event_value=` →

```json
{"account_id": 731, "from": "2026-01-12", "to": "2026-11-02",
 "granularity": "day", "requested_granularity": "day",
 "currency": "CAD", "native_currency": "CAD", "min_event_value": "1000.00",
 "has_data": true,
 "buckets": [{"bucket_end": "2026-02-09", "cash_cad": "16500.00",
             "positions_cad": "18742.00", "value_cad": "35242.00",
              "estimated": false, "valued": true}],
 "events": [{"id": "trade:1234", "source": "trade", "trade_id": 1234,
             "transaction_id": null, "date": "2026-02-09",
             "bucket_end": "2026-02-09", "kind": "contribution", "direction": 1,
             "amount_cad": "16500.00", "amount_native": "16500.00",
             "currency": "CAD", "payee": null, "category_name": null,
             "symbol": "CASH", "security_id": 83, "quantity": "0",
             "action": "contribution"}]}
```

- **Series math** (`backend/app/services/account_value.py`) is docs/05 §Portfolio
  value's `V(d)` scoped to ONE account: `cash = starting_balance + Σ committed
  transactions + Σ trade-generated cash` (docs/05 cash-effect table, imported as
  `total_return._trade_cash_effect`) converted at the bucket end's cached rate,
  plus `positions = Σ signed quantity × newest price ≤ bucket end × listing
  rate`. It is deliberately NOT `net_worth_report` with an `account_id` filter:
  docs/12 §4 pins net-worth `cash_cad` to `starting_balance + Σ transactions`
  ONLY (the browser mirror must match it to the string), and that chain would
  draw a false `0` for an investment account — real account 731 has ZERO
  transactions, every dollar is a trade row. Everything else IS shared: the
  docs/12 §Time bucketing helpers (`bucket_starts`/`bucket_end`/`bucket_key`),
  `FxCache`, `PriceCache`, `_committed_transactions` (gained an `account_id`
  parameter, and M145 its batched `account_ids` twin — the committed/split
  predicate is unchanged in both), the committed-trade
  predicate and the position replay. No second bucketing implementation, no
  second price/fx cache, no second cash-effect table.
- **Cash view note (M145).** The account page's `Total value` card is
  `cash_balance + investment_value`; since M145 an investment account's
  `cash_balance` IS this series' cash leg (the same docs/05 chain), so the card
  and the chart's last point agree by construction — this note's old card-vs-
  chart difference (ledger-only card vs trade-aware chart) was the M145 bug.
  The tooltip still prints `Cash` + `Holdings` for the bucket.
- **Bucketing & point cap.** Buckets follow docs/12 §Time bucketing (week starts
  Monday; a bucket is valued at its END, so the last bucket may reach past
  `to`). The response never exceeds **400 points**: `granularity` is coarsened
  `day → week → month → year` until the count fits, and BOTH the ask
  (`requested_granularity`) and the answer (`granularity`) come back — the
  document `granularity` enum for this route is `day|week|month`, `year` only as
  a coarsening result (a 33+ year window). With no `granularity` the finest
  level that fits is chosen.
- **Honesty (one flag per bucket, never a fabricated number).**
  - `valued: false` + `value_cad: null` → the total is UNKNOWN. `gap_reason`
    says WHICH unknown it is (M144 — the two-reason empty state):
    - `no_price` — a held security has no cached close on or before this day
      (docs/07 §Backfill depth is what closes it);
    - `fx` — the account's, a cash row's or a listing's rate is absent, or
      older than the M115 ≤7d carry-forward window, past which there is no
      honest CAD figure;
    - `not_held` — a HOLDINGS gap, not a data gap: the account held NO cash
      and NO position on this day, and had held nothing earlier in the window
      either, so there was nothing to value. It is null on every valued bucket
      and is the only member of the set that is not a cache problem.
    `cash_cad` stays populated when only the holdings side failed (its own
    cell is `null` when the account's fx failed). The chart draws a GAP for
    all three; the table's Notes column and the tooltip print the reason
    (docs/09 §Empty-state copy).
  - **The portfolio merge treats `not_held` as ZERO, never as unknown**
    (`account_value._merge_buckets`): one account's empty opening weeks must
    not blank every other account's real numbers for those days. A present-
    but-`no_price`/`fx` bucket still contributes `None` and still makes the
    sum `None` — one unvaluable account cannot be papered over with a zero.
  - `estimated: true` → the figure is real but an INPUT was carried forward
    beyond docs/07's 7-day staleness rule (price date > `bucket_end − 7d`, or a
    price from the future — the earliest-price fallback). Weekend/holiday
    carry-forward inside 7 days is normal market cadence and is NOT flagged.
- **Event markers** — derived from the account's committed rows on EVERY
  request (never extracted into a cache table), inside the drawn range:
  - **sources**: `trades` with `action ∈ {contribution, withdrawal,
    transfer_in, transfer_out}` (cash rows at their docs/05 cash effect;
    an in-kind transfer at `|quantity| × price_native`, because value arrived
    even though no cash moved) plus EVERY committed transaction on the account.
    Buys/sells/dividends/fees are internal — they move value between cash and
    holdings without moving money across the boundary, so they are never marked.
  - **`kind`** ∈ `contribution | withdrawal | transfer_in | transfer_out |
    deposit`: read from the ACTION (M131) — a `withdrawal` row IS money
    leaving. A `contribution` whose amount is NEGATIVE (a pre-M131 legacy row;
    the importer now refuses one, 422 `negative_contribution`) is still read
    as a `withdrawal` by sign, and a positive transaction is a `deposit`.
    `direction` is `+1`/`-1` and `amount_cad` carries the same sign.
  - **`min_event_value`** default **1000** in reporting currency. An event is
    significant when `|amount_cad| >= min_event_value` **OR** the window moved
    by at least 10% of that amount (`|amount_cad| >= 0.10 × |last valued −
    first valued|`, skipped when the delta is 0) — so a $300 deposit still
    marks inside an account that moved $2,000. `min_event_value=0` returns
    EVERY flow (the first test in `tests/test_account_value_history.py`).
  - `amount_cad` is converted at the event's own date (falls back to the
    bucket's account rate); an event with no resolvable rate at either is
    omitted rather than guessed.
- **Defaults & validation.** `from` defaults to the account's **floor** and is
  clamped up to it: the EARLIEST of the account's `created_at` and its first
  committed row. `created_at` is when the ROW was written, not when the account
  started — real account 731 was created `2026-11-02` with trades back to
  `2026-01-12` (an import of years of history), so flooring at `created_at`
  alone would draw ONE point and delete the timeline the chart exists for. The
  floor is therefore never before real data and never after it, and no bucket
  is ever fabricated ahead of the account's first evidence. `to` defaults to
  today; `granularity=day`; `min_event_value=1000`. `from > to` → `422
  invalid_date_range`; another tenant's account → `404 not_found` (the same
  `owned_or_404` every other `/{account_id}` route uses). A window that ends
  before the account's first row answers `has_data: false` with an empty
  `buckets` list rather than a fabricated line.
- **Caching.** Read-only and derived, so the response carries
  `Cache-Control: private, max-age=60`; the account page mirrors it with
  `staleTime: 60_000` on `["account-value-history", accountId, from, to]`.
- **`has_data`** is `false` when the account has no committed row at or before
  `to` and no starting balance — the chart renders the three-part empty state
  (docs/09 §Empty states) instead of a flat $0 line.
- **Verification.** `backend/tests/test_account_value_history.py` (12 tests):
  three hand-recomputed probe dates, staging exclusion, the default threshold
  and `min_event_value=0`, the no-price gap, the unresolved-fx gap, both
  coarsening steps, the range/cache/tenancy contracts, the empty-state flag and
  a 366-day × 6-security sweep under the 1.5s house cap.

### Portfolio value history (M130)

`GET /portfolio/value-history?from=&to=&granularity=day|week|month&min_event_value=` →

```json
{"account_ids": [731, 732, 733], "from": "2026-01-12", "to": "2026-11-03",
 "granularity": "week", "requested_granularity": "day",
 "currency": "CAD", "native_currency": "CAD", "min_event_value": "1000.00",
 "has_data": true,
 "buckets": [{"bucket_end": "2026-02-09", "cash_cad": "-582.73",
             "positions_cad": "286540.25", "value_cad": "285957.52",
              "estimated": false, "valued": true}],
 "events": [{"id": "trade:1357", "source": "trade", "trade_id": 1357,
             "transaction_id": null, "date": "2026-01-12",
             "bucket_end": "2026-01-18", "kind": "contribution", "direction": 1,
             "amount_cad": "12600.00", "amount_native": "12600.00",
             "currency": "CAD", "payee": null, "category_name": null,
             "symbol": null, "security_id": 83, "quantity": "0",
             "action": "contribution",
             "account_ids": [731], "account_names": ["TFSA"]}],
 "stats": {"from_bucket": "2026-01-18", "to_bucket": "2026-11-03",
           "from_value_cad": "0.00", "to_value_cad": "285472.31",
           "change_cad": "285472.31", "change_pct": null}}
```

- **Scope.** Every account of the caller whose `type ∈ INVESTMENT_ACCOUNT_TYPES`
  (docs/02), `account_ids` ascending. A user with none answers the empty
  contract — `has_data: false`, empty `buckets`/`events`, `stats` all `null` —
  never a flat $0 line (docs/09 §Empty states).
- **Series math is M118's, run once per account and SUMMED.**
  `services/account_value.py::portfolio_value_history` calls
  `account_value_history` for each account with the same window and adds the
  buckets; there is still only one valuation chain, one `FxCache`/`PriceCache`
  pair per call and no cache table (docs/12 Principles: derived every read).
  Two merge rules:
  - **One granularity for the whole series.** Buckets are calendar-aligned, but
    accounts RESOLVE the ask differently — the account whose floor is earliest
    has the most buckets and coarsens first, so a six-year TFSA answers `week`
    where a three-month RRSP still answers `day`. The merge takes the COARSEST
    answer any account produced and re-asks only the accounts that resolved
    finer, so the summed line has one bucket size (asserted in
    `test_one_granularity_across_accounts_with_different_floors`).
  - **A bucket an account does not have is that account contributing ZERO, not
    unknown** — it had not started yet, so it held nothing. A bucket an account
    IS in and reports `null` makes the sum `null`: one unvaluable account
    cannot be papered over with a zero (docs/08 §Honesty). `estimated` ORs
    across the accounts in the bucket; `valued` ANDs.
- **Event markers** — the per-account lists concatenated and **deduped by
  `id`** (`trade:<id>` / `transaction:<id>`, globally unique, so one row is one
  marker however many series mention it), sorted `(date, id)`. Each merged
  marker carries `account_ids` / `account_names` — every account that produced
  it — which is what the chart's tooltip names. Marker semantics, threshold,
  and the derived-every-request rule are M118's, unchanged.
- **`stats` is the window's own move**, computed server-side from the SAME
  `buckets` in this response: `change_cad = last − first` over the valued
  points, `change_pct` = that ÷ the first (2 dp, `null` when the first is 0 or
  fewer than two points are valued), with `from_bucket`/`to_bucket` naming the
  two dates subtracted. This is the number the header's "over 6 months" line
  renders (docs/09 §Portfolio header) — the client never subtracts.
- **Cross-endpoint rounding note (F-M130-1).** This series and the header
  (`GET /portfolio/positions` §Portfolio summary below) are deliberately two
  separate valuation passes: positions rounds EACH security row to cents and
  sums (`Σ round(xᵢ)`), while this walk sums raw per account and rounds once
  per bucket (`round(Σ xᵢ)`). On fractional-cent prices the two therefore
  differ by a rounding-order penny — on the demo tenant the header reads
  `227739.75` while the last bucket reads `227739.74`. The drift is bounded
  by **0.005 CAD per position row** (the cash leg is shared and contributes
  nothing), pinned by
  `test_m130_portfolio_value.py::test_f_m130_1_positions_header_and_history_last_bucket_drift_is_pennies_bounded`.
  Neither figure is wrong and neither derives from the other: the header is
  the positions table's own Σ, and `stats` is this chart's own walk — sharing
  one rounding order would break the single-source rules both endpoints pin.
  **The mode behind the "row" rounding is `ROUND_HALF_EVEN`** (docs/02
  Conventions), stated explicitly as `cents(value, rounding=ROUND_HALF_EVEN)`
  in `services/valuation.py` — it is the one implementation app-wide, not a
  second helper, and `tests/test_money_rounding_single_source.py` pins both the
  single source and this deliberate mode.
- **`native_currency` echoes `currency`.** A portfolio has no single native
  currency (accounts differ), so every figure is already in the reporting one;
  echoing it keeps the chart's "values in <cur>" badge off instead of claiming
  an account's currency the payload does not have.
- **Defaults & validation.** Identical to M118's: `from` defaults to the
  PORTFOLIO floor (the earliest of every account's floor — the earliest of
  `created_at` and its first committed row) and is clamped up to it, `to` =
  today, `granularity=day` coarsened to the 400-point roofline with both
  `granularity` and `requested_granularity` echoed, `min_event_value=1000`,
  `from > to` → `422 invalid_date_range`. Read-only and derived, so the
  response carries `Cache-Control: private, max-age=60`, mirrored by the
  caller's `staleTime: 60_000` on `["portfolio-value-history", from, to]`.
- **Verification.** `backend/tests/test_m130_portfolio_value.py` (13 tests),
  including the bucket-by-bucket equality against the two per-account
  responses, the stats equality against the chart's own first/last point, the
  marker dedup, the one-granularity reconciliation, scope exclusion against a
  chequing account carrying 999,999.00, staging exclusion, the range/cache
  contracts and the empty state.

### Portfolio summary on `GET /portfolio/positions` (M130)

The header card's figures ride the positions payload itself, so the page's
headline and its table come out of ONE `compute_positions` call (docs/09
§Portfolio header). Each position also carries `price_date` (ISO date or null)
and `price_age_days` (whole calendar days from that date or null); those
values are read from the same tenant-scoped price row. `price_fetched_at: datetime | null` is read from that row's `prices.fetched_at`: it is present only when a real provider response was timestamped at arrival and cached; NULL means no fetch evidence (never derive it from `created_at`, `updated_at`, or `price_date`). `price_age_days` remains calendar-day math on `price_date` only. Added to
the top level:

| field | type | meaning |
| --- | --- | --- |
| `total_value_cad` | Decimal string | Σ `market_value_cad` over `items` — the holdings basis, exactly the CAD column's Σ |
| `cash_cad` | Decimal string \| `null` | docs/05 §Portfolio value's CASH leg read at `today`: `starting_balance + Σ committed transactions + Σ docs/05 trade cash effects`, converted at the cached rate; scoped to the same investment accounts as `items` (`[account_id]` when filtered). `null` when any leg's rate is absent or past the M115 ≤7d window — never a fabricated zero |
| `portfolio_value_cad` | Decimal string \| `null` | `total_value_cad + cash_cad` — the headline, added SERVER-SIDE. `null` exactly when `cash_cad` is |
| `total_day_change_cad` | Decimal string \| `null` | Σ `day_change_cad` over `items`; rows with no prior close contribute nothing and `null` when no row computes one — the same rule as `total_unrealized_cad` |
| `total_day_change_pct` | number \| `null` | `total_day_change_cad ÷ (total_value_cad − total_day_change_cad)`, 2 dp — yesterday's level as the denominator |
| `day_change_as_of` | ISO date \| `null` | The most recent `previous_close_date` among the contributing rows — the honest "vs Oct 7 close" label (never "yesterday") |
| `day_change_mixed_baseline` | boolean | `true` when those baseline dates DISAGREE: the latest is still named, and the UI says the rest closed earlier instead of implying one market day |

Every field is a sum over the SAME `items` in the response — there is no
second valuation pass anywhere, which is why `Σ(items.market_value_cad) ==
total_value_cad` holds by construction and the header can be asserted equal to
the Σ row. (The portfolio value-history series is a SEPARATE pass with a
different rounding order and can differ from this header by a bounded penny —
see the cross-endpoint rounding note under §Portfolio value history, F-M130-1.) Rows with a `null` input contribute nothing rather than a zero, and
`total_day_change_cad` is `null` (not `0.00`) when no row has a baseline.

## Transactions & imports

| Method/Path | Shape |
| --- | --- |
| `GET /transactions` | Filters as discrete query params (AND-composed): `account_id`, `category_id` (null = uncategorized), `tag_id` (only transactions carrying the tag; foreign id → 404), `tag_ids` (repeated — **M104**: a row must carry EVERY id, an AND of "carries this tag" predicates, which is the semantics the docs/09 §Tag UX multi-tag filter states as "matches all selected"; `dict.fromkeys` dedupes repeats, each id ownership-checked → 404, and `tag_id` + `tag_ids` AND together), `from`/`to` (date range), `payee` (text), `amount_min`/`amount_max` (amount range), `direction=paid\|received` (signed amount: paid < 0, received > 0), `payee_token=` exact blind-index match (E2EE, docs/33), **`reviewed=true\|false`** (M106 — `false` IS the "Needs review" queue, `true` its inverse; NULL `reviewed_at` is unreviewed, docs/02). **`q` RE-MOUNTED (M137, docs/33 §Server-first computing)** — fuzzy payee search is scored by the SERVER again: `greatest(similarity, word_similarity) >= 0.3` over `normalized_payee` (`app/api/_search_helpers.py`, the same helper `services/categorization` scores with, behind `ix_trgm_transactions_payee`). With `q` the page is ordered **`score desc, id desc`** and `total` is the HIT count (the envelope `searchPage()` used to compute in the browser over a full-ledger download); without it the list keeps its `date desc, id desc`. `q` (min 1, max 200) AND-composes with every structured filter above. **M137 also adds `hide_encrypted=true`** — a locked-vault client asks for only the rows it could display: cipher payees are plaintext on the server and always MATCH, so the response shrinks and gains **`withheld: int`** (0 unless asked), the count `VaultLocked`'s `partial` copy prints instead of inventing. `lib/searchMatch.ts` survives as the E2EE/offline fallback — M160 (P2-5) retired the REPORT-engine mirrors, not the search matcher, so this path is unchanged. Paginated. Transaction/account/user shapes carry opaque E2EE ciphertext fields (`payee_cipher`, `raw_desc_cipher`, `amount_cipher`, `cad_amount_cipher`, `payee_token`; `accounts.name_cipher`; `users.encryption_required`) — null on legacy rows. Every `TransactionPublic` also carries the M96 recurring fields: `is_recurring: bool` (false until the user accepts a candidate group), `recurring_cadence: weekly\|biweekly\|four_weekly\|monthly \| null` and `recurring_next_date: date \| null` — the badge source on `/transactions` (docs/09 §Recurring prompt). **M106** adds `reviewed_at: datetime \| null` and `reviewed_by: int \| null` to the same shape (null = unreviewed, docs/02). **M108 P4** adds `has_receipt: bool` — false unless a receipt image is attached (docs/02 §transaction_receipts), computed from ONE batched query per page so the list never N+1s and never carries the image itself (docs/34 budget rows 4/5 = 5 statements).**M107 (docs/44 D11)** makes the page LEDGER-populated: `items` holds children + ordinary rows and **never a split parent**, so a naive consumer that sums `items` cannot double-count. The page gains `split_parents: list[SplitParentPublic]` — the parents of the children **in this page**, ordered `date desc, id desc`, `[]` when the page carries no children; `SplitParentPublic` = `TransactionPublic` + `child_count: int` (the group's TRUE part count in the TABLE, independent of the current filter, so a content filter that returned one child still renders a header claiming the real number of parts). `TransactionPublic` itself gains `parent_split_id: int \| null` (set on children only), `is_split: bool` (default `false`) and `note: str \| null` (plaintext ≤ 500 chars — docs/33 §Server-readable plaintext). **`total` counts matching LEDGER rows**: a three-child split contributes 3 to `total` and to a `limit`/`offset` page. `amount_min` / `amount_max` / `direction` are evaluated against `COALESCE(parent.amount_native, own.amount_native)` — an amount filter finds the **statement**, not the part: a `+187.42` row split into `+120.00 / +67.42` returns **both** children for `amount_min=100`, and a `-187.42` row split into `-120.00 / -40.00 / -27.42` returns **all three** for `amount_max=-100` (the bound is compared against the parent's own signed amount, so a negative statement is reachable only through `max` and a positive one only through `min` — `amount_min=100` on `-187.42` matches nothing, which is correct: `-187.42 >= 100` is false). Content filters (`from`, `to`, `payee`, `payee_token`, `account_id`, `category_id`, `tag_id`, `tag_ids`, `reviewed`) still match each child on its OWN copied identity/category/tags — that is the filter splitting exists for — and `split_parents` still carries the header with the true total and `child_count`.  When `encryption_required` is true, tx create/update without `payee_cipher`+`amount_cipher` and account create/update without `name_cipher` are rejected 422 `encryption_required`; cipher fields over 8000 chars are 422 `invalid_cipher`. |
| `POST /transactions` | JSON `TransactionCreate` → `TransactionPublic`, HTTP 201. Required `account_id`, `date`, non-empty `raw_description`, Decimal-string `amount_native`; optional `posting_date`, non-empty `normalized_payee`, three-letter uppercase `currency`, and `category_id`. Omitted payee collapses whitespace from `raw_description`; omitted currency uses the owned account currency. Clients never send derived fields. Manual rows have no import batch. |
| `PATCH /transactions/{transaction_id}` | Extra-forbid partial `TransactionUpdate` over the editable ledger fields above, including `account_id` for moving between owned accounts; `account_id: null` is invalid. Recomputes dedup/CAD when ledger identity fields change. Inline category/payee patches remain valid.. **M107 (docs/44 §5)**: `TransactionUpdate` also accepts `note: str \| null` (≤ 500, plaintext), and the split guard runs FIRST, before any other validation — a PATCH carrying any LEDGER field (`account_id`, `date`, `posting_date`, `raw_description`, `normalized_payee`, `amount_native`, `currency`, `category_id`, `note`) on a split **parent** → **409 `transaction_is_split`**; on a **child**, the frozen identity/money fields (`account_id`, `date`, `posting_date`, `raw_description`, `normalized_payee`, `amount_native`, `currency`) → **409 `split_leg_immutable`**, while `category_id` / `note` / cipher fields / review state stay ordinary 200 edits. A **cipher-only** PATCH on a split parent (`payee_cipher`, `raw_desc_cipher`, `amount_cipher`, `cad_amount_cipher`, `payee_token`) stays allowed — the docs/33 A17 E2EE backfill must keep working. **Learning loop (docs/04)**: a PATCH whose body moved `category_id` or `normalized_payee` upserts the learned map and stamps its receipt payload with `learned: {normalized_payee, category_id}` (the drawer's "learned from you" badge); an edit that moved neither writes no `learned` key, and clearing a category writes none |
| `DELETE /transactions/{transaction_id}` | Deletes a manual or committed transaction, its dependent suggestions, and unlinks a paired counterpart through the existing foreign-key action; HTTP 204. Pending-batch rows are batch-managed and rejected. **M107 (docs/44 §5)**: `DELETE` on a split **parent** → **409 `transaction_is_split`** ("This transaction is split. Merge its parts first." — merge is the only edit a split parent admits); `DELETE` on a **child** → **409 `split_leg_immutable`** (a child is never deleted on its own — re-split with `PUT …/split` or merge back). |
| Bulk actions on `/transactions` (M96 B1) | **No new route.** The toolbar's *Recategorize* / *Delete* / *Set payee* sequence the `PATCH /transactions/{id}` and `DELETE /transactions/{id}` rows above — **one id at a time**, in order, with a visible `n / total` progress readout (never a parallel burst: N authenticated requests fired at once trades the indicator for `429`s). Each row answers for itself, so a refusal (`409 transaction_in_pending_batch`, `404 not_found`, `422 …`) becomes a per-row conflict the CLIENT reports — `Deleted 7 of 9 — 2 skipped` + one line per skipped row with the envelope `{detail, code}` — while the rest of the batch commits. Selection is client state only and is never transmitted: the ids a bulk action touches are always exactly the rows on screen. This is the **PAGE path**, and it is unchanged by M125 — an explicit id list (≤ `PAGE_SIZE`) still drives the single-row routes one row at a time. The other half of the toolbar — "select all N matched" — DOES take a route: `POST /transactions/bulk/{action}` below (docs/09 §Bulk selection toolbar §Select-all-matched). |
| `POST /transactions/bulk/recategorize` `\|` `payee` `\|` `review` `\|` `tags` `\|` `delete` | **M125 — bulk over a MATCHED set (docs/09 §Bulk selection toolbar §Select-all-matched).** ONE request per action instead of N, for the toolbar's "select all N matched" state. Every body (`extra=forbid`) = `{filter: BulkFilter, exclude_ids: int[], …action}`. `filter` is field-for-field the `GET /transactions` query above (same names, same `category_id: "null"` = uncategorized convention, `tag_ids` AND-composed, `payee_token` blind-index, **and `q` since M137** — a bulk run over a ranked set touches exactly the rows the ranked page rendered) and is built by the ONE constructor `_build_txn_filters()` that the GET route also calls — so what a run touches IS what the page rendered. `exclude_ids` are the rows the user unchecked out of that set; **the matched ids themselves are never transmitted** (345 ids would be 345 ids — the server re-derives the set at execution time, which is also the only place the count can still be trusted). `filter.q` thresholds the same `_ledger_scope` predicate the GET route uses (M137 — it was refused with 422 `q_removed` before the re-mount). Action fields: `recategorize {category_id: int \| null}`; `payee {normalized_payee: str, payee_cipher?, payee_token?}` (docs/33 dual-write — only the two PAYEE fields travel, so a rename never rewrites a row's amount ciphertext); `review {reviewed: bool}`; `tags {tag_id: int}` (the M104 add, idempotent — an already-tagged row is NOT a refusal); `delete {expected_count: int}`. Ownership (`account_id`/`category_id`/`tag_id`) is 404-checked BEFORE anything is written, in GET's own order. → **200 `BulkResult`** `{matched: int, updated: int, skipped: [{id, label, code, detail}]}`: `matched` is the SERVER's count of the selection (the number the confirmation printed), `updated` what actually landed, `skipped` every refusal with its own envelope — `409 transaction_in_pending_batch`, `409 transaction_is_split` / `409 split_leg_immutable`, `422 encryption_required`, `422 validation_error` — and `label` is the row's `normalized_payee`, because a matched set can skip a row on page 7 the browser never loaded. Every per-row guard runs BEFORE that row's first mutation, so a caught refusal leaves the session clean; one `db.commit()` at the end; `transaction_events` receipts are written exactly as `PATCH`/`DELETE` write them (M120 — the History drawer cannot tell a run from N clicks). **`delete` ONLY**: `expected_count` must equal the current `matched` or the run is refused **409 `bulk_count_mismatch`** with ZERO writes ("the ledger moved after that count was read"). `404`/`403`/`422` above the row level abort the whole run with nothing written. Rows are resolved to id ints first, then ORM objects are loaded in chunks of 500 (`_BULK_CHUNK`), so a matched run never materialises a whole ledger. **Declaration order matters**: these five routes are registered BEFORE `POST /transactions/{transaction_id}/…` — Starlette matches in registration order and `bulk` is not an int, so a later registration would answer 422 `validation_error` on `int("bulk")`. Demo tenant → `403 demo_account_restricted` (central guard). |
| `POST /transactions/{transaction_id}/split` | **M107 (docs/44 §4).** Body `{children: SplitChildIn[]}` (`extra=forbid`); `SplitChildIn` = `{amount_native: Decimal-string (required), category_id: int \| null, note: str ≤ 500 \| null, tag_ids: int[] (default []), amount_cipher: str \| null, cad_amount_cipher: str \| null}`. → **201 `TransactionSplit`** = `{parent: SplitParentPublic, children: TransactionPublic[] (id ASC = entry order), split_checksum: Decimal-string, sum_ok: bool}`. 2–20 children. Full shape + the 13-step validation order + every code: § Split transactions below. |
| `PUT /transactions/{transaction_id}/split` | Same body; **replaces ALL children** (the old ones are purged through the same internal helper as a normal `DELETE /transactions/{id}`, minus the `deleted` receipt — D15). → **200 `TransactionSplit`**. |
| `GET /transactions/{transaction_id}/split` | → **200 `TransactionSplit`** — the drilldown the register opens. **M156 correction:** the checksum is RECOMPUTED on every write and STORED in `transactions.split_checksum`; every read (this GET included) returns the stored column and derives `sum_ok` from it rather than re-adding the legs. That is deliberate — a row whose children no longer add up answers `sum_ok: false` (`M127`; `backend/tests/test_m127_split_checksum.py` pins a corrupted stored checksum doing exactly that) instead of quietly re-deriving a truth the ledger no longer holds. Integrity is therefore assertable by reading, and a corrupted row is VISIBLE rather than papered over. Runs only validation steps 1–5. |
| `DELETE /transactions/{transaction_id}/split` | **Merge-back.** → **200 `TransactionPublic`** — children deleted, `is_split=false`, `split_checksum=NULL`, every other field byte-identical to pre-split (docs/44 T9). One `merge` receipt carries the deleted child ids. Runs only validation steps 1–5. |
| `GET /transactions/recurring/candidates` | **M96 B2 (docs/09 §Recurring prompt); `?source` + `dismiss_key` added M138 (docs/09 §Bills you probably have).** Passive, local-only detection over the caller's committed ledger (trailing `LOOKBACK_DAYS=400`, rows not yet tagged): same normalized merchant (`services/subscriptions.py::normalize_payee_key` — the ONE grouping rule, so `SUPERSTORE 1234` ≈ `Superstore #1234`, and — M165 — whitespace-free, so `GOOGLE *YouTubePremium` ≈ `Google*Youtubepremium`) × ±15% amount band (same sign required) × **every** gap fitting one cadence (monthly-same-day first, then fixed 28/30/31/14/7-day) × **≥3** occurrences. **Query `source`** ∈ `recurring_prompt` (default) \| `subscriptions` — which surface is asking; it selects which `recurring_suppressions` keys are subtracted, and an unknown value → **422**. → 200 `{groups: [{group_key, dismiss_key, payee, cadence: weekly\|biweekly\|four_weekly\|monthly, occurrence_count, transaction_ids: int[], monthly_total, first_date, last_date, next_expected_date}], combined_monthly_total, suppressed_count, window_start}`. **`dismiss_key`** is the key THIS source suppresses under: `group_key` for `recurring_prompt`, `services/recurring.py::make_subscriptions_group_key(payee)` = `<normalize_payee_key(payee)>\|subscriptions` for `subscriptions` — a caller never re-derives the normalization client-side. Suppressions for `source` and merchants already carrying a tagged row are subtracted BEFORE answering (a decision is final — that surface does not come back; M165: each stored key is RE-READ through today's `normalize_payee_key` — `services/recurring.py::rekey_suppression` — so a key written under an older normalization still subtracts its group, with no data migration), so a dashboard dismissal never silences the prompt and the reverse. Zero outbound calls: no LLM, no fx/market-data, no network import anywhere in `services/recurring.py` (`tests/test_recurring.py`). |
| `POST /transactions/recurring/accept` | `{group_key, transaction_ids: int[] (≥1), cadence}` (`extra=forbid`) → 200 `{updated, group_key, cadence, next_expected_date}`. Sets `is_recurring=true`, `recurring_cadence` and `recurring_next_date` (= last occurrence + cadence; monthly clamped to the month's last day) on each id. Every id is ownership-checked (`404 not_found`) and live-checked (`409 transaction_in_pending_batch`) **before** any write, so a mixed batch never half-applies. Empty ids, unknown `cadence` or an extra field → `422 validation_error`. Per-group, never all-or-nothing. **M120: writes receipts** — one `edited` event per id in the group, in the same commit (`{"fields": ["is_recurring", "recurring_cadence", "recurring_next_date"], "changes": {...}}`, no `via` key); a pre-write `404`/`409` on any id writes none (docs/02 §transaction_events). |
| `POST /transactions/recurring/suppress` | `{group_key}` (`extra=forbid`, ≤120 chars) → **204**. Upserts `recurring_suppressions(user_id, group_key)` `ON CONFLICT DO NOTHING` — idempotent, and deliberately has **no** unsuppress route: the answer to "is this recurring?" is final (docs/09). The client re-tags rows by hand if they change their mind. **Both surfaces write through here** (M138): the M96 prompt posts its `group_key` (`<merchant>\|<cadence>`), the dashboard card posts `<merchant>\|subscriptions` — the source discriminator lives IN the key, which is why no `source` column and no migration were needed. Undo on either surface needs no compensating request: nothing is POSTed until the 6-second window closes (docs/09 §Bills you probably have). |
| `GET /transactions/similar?q=` | **RE-MOUNTED (M137, docs/33 §Server-first computing; docs/04 §Similar examples).** Ranked payee examples from the same pg_trgm predicate as `?q=` above. Query params: `q` (required, 1–200), `exclude_id` (the row asking about its own family never appears in it — M119), `categorized_only=false` (docs/04's original "only categorized rows qualify"), `categorized_first=true` (M119: a categorized sibling outranks an EQUAL-SCORING uncategorized one, so the example the user can APPLY is the top row), `hide_encrypted=false`, `limit=10` (1–50). → 200 `SimilarPage` `{query, items: SimilarTransaction[], total, withheld}` ordered `score desc, [categorized desc,] id desc`; `SimilarTransaction` = id, account_id, date, raw_description, normalized_payee, amount_native, currency, cad_amount, category_id, category_name, score + **`encrypted: bool`** (M137 — payee stored as ciphertext; a locked client withholds on this). `total` counts the matches this request kept, `withheld` the ones dropped only because `hide_encrypted` was set. `lib/searchMatch.matchRows` stays as the E2EE/offline fallback. |
| `POST /imports/preview` | multipart `file` + `account_id` + optional `profile_json` → `DryRunResponse` `{format, mapping, rows[], summary}` — the full parse, column-role `mapping` (`kind` `structured`\|`csv_profile`), every row's `verdict` (`new`\|`duplicate`\|`near_duplicate`) with its `near_duplicate_match`, and the cached-FX `cad_amount`, **with zero writes** (docs/03). Same validation chain and codes as upload: `413 file_too_large` / `413 too_many_rows`, `422 unsupported_format`, `422 ofx_parse_error` / `422 qif_parse_error` / `422 csv_parse_error` / `422 parse_error`, `422 missing_importer_profile`, `422 invalid_profile`. Rate **6/min**. **M181:** deliberately NOT guarded — it writes nothing, so an `encryption_required` tenant can still dry-run (pinned by a zero-rows test). |
| `POST /imports/upload` | multipart file + account_id → creates `import_batches` status=pending_review, returns batch + parsed preview. Rate **6/min**; `413 file_too_large` (>5 MB / oversized Content-Length), `422 csv_line_too_long` (line > 1,000,000 chars), `422 csv_too_complex` (>1,000 quotes on one line), `422 csv_parse_error` — pathological input is rejected BEFORE parsing (audit H2, docs/03). **M181:** `422 encryption_required` for a tenant with `users.encryption_required` — refused after the account ownership check and BEFORE the file is read, so nothing is staged and no cipher column can be left NULL (docs/33 § wire inventory) |
| `GET /imports/{batch_id}` | Batch summary: imported / duplicates_skipped / unresolved counts |
| `POST /imports/{batch_id}/commit` | pending_review → committed. **M181:** `422 encryption_required` before any write for a tenant with `users.encryption_required` — the enrol-after-staging hole is closed: a batch staged while the tenant was ordinary cannot be committed once the flag is on. Non-pending → `409 batch_not_pending_review` (checked first). |
| `POST /imports/{batch_id}/discard` | `pending_review` → `discarded`: deletes the batch's transactions AND trades (transaction children unlinked first, docs/29 F3; `acb_pools` rebuilt if trades went). Non-pending → `409 batch_not_pending_review`. |
| `POST /imports/suggest-profile` | multipart `file` (CSV) → `SuggestProfileResponse` (`columns`, `date_format`, `decimal_comma`, `credits_positive`, `format`, `confidence`, `reasoning`); known export format first (deterministic preset, docs/58 §A, `confidence=0.95`, no LLM call), then LLM, then heuristic. `422 not_csv`, `413 file_too_large` (>5 MB), `422 csv_line_too_long` / `csv_too_complex` / `csv_parse_error` (pre-parse caps), rate **6/min**. **M181:** deliberately NOT guarded — zero writes (pinned: an `encryption_required` tenant gets a real answer and 0 new rows). |
| `GET /imports` | Batch history list (paginated) for the Imports page. |
| `GET /imports/{batch_id}/rows` | Staged rows of a batch for review/commit. Each row also carries `near_duplicate` + `near_duplicate_match`, re-derived on read so a reopened batch keeps its verdicts (docs/03) |
| `POST /imports/{batch_id}/revert` | `committed` → `reverted`: deletes the batch's transactions AND trades (tag links / LLM suggestions / reconciliation matches unlinked first, docs/29 F3); `acb_pools` rebuilt in the same transaction; `transfer_pair_id` ON DELETE SET NULL (migration 0010). Non-committed → `409 batch_not_committed`. |
| `POST /imports/ynab/preview` | multipart `file` (+ optional `account_id`, `account_type`, `currency`, `date_format`) → `200 YnabMigrationPlan`: the full mapping (accounts, groups, categories, targets, rows, `unmappable`, warnings, notes, counts). **Read-only.** Rate **6/min**; `413 file_too_large`/`too_many_rows` (5 MB / 50 000), `422 ynab_parse_error`, `422 invalid_account_type`/`invalid_currency`/`invalid_date_format`. Must be registered before `/imports/{batch_id}/commit` (docs/38 §6). |
| `POST /imports/ynab/commit` | Same form; applies the plan in one transaction → `201 YnabCommitResult {plan, batches[], net_new, duplicates_skipped}`. Staging goes through the shared `stage_rows` pipeline (docs/03), so `413 too_many_rows` when one target account carries >2000 importable rows — the whole migration rolls back. Rate **10/min**; `422 encryption_required` for E2EE tenants (docs/38 §8). |
| `POST /imports/networth/preview` | multipart `file` (ProjectionLab JSON export or Account/Balance CSV) + optional `currency` (ISO, default CAD) + optional `account_name` → `200 NetWorthPlan {source_format, file_name, currency, counts{accounts, accounts_create, accounts_reuse, accounts_unmapped, balance_rows_collapsed, progress_snapshots_skipped, assets_skipped}, accounts[]{name, source_type, mapped_type, action(create/reuse/unmapped), existing_id, file_balance, current_balance, currency, reason, notes[]}, skipped[]{kind, name, reason}, warnings[]{code, detail}, can_commit}`. **Read-only** (docs/58 §B). Rate **6/min**; `422 unsupported_networth_format` / `networth_missing_columns` / `networth_row_invalid` / `networth_no_accounts` / `invalid_currency`, `413 networth_row_cap` (>1000 rows) / `file_too_large` (5 MB). Registered ABOVE `/{batch_id}` so the literal path is never swallowed as a batch id (docs/38 §6). |
| `POST /imports/networth/commit` | Same form; maps AND applies in one transaction → `200 NetWorthCommitResult` (plan + `created`, `reused`). Creates accounts only: an existing account with the same name is reused and its balance is NEVER overwritten (`file_balance` sits beside `current_balance` for review). Rate **10/min**; `422 encryption_required` for E2EE tenants (`Importing a net-worth file`, docs/33 rule 7d — preview stays open). |
| `POST /transactions/{transaction_id}/detect-transfer` | Finds the matching transfer counterpart within ±3 days (opposite amount, different account, `transfers` system category) → 200 updated `TransactionPublic`; none → `404 no_matching_transfer`. **M107:** the search space is IDENTITY (children are never a counterpart) and the write itself is guarded — a split **parent** → **409 `transaction_is_split`**, a **child** → **409 `split_leg_immutable`**. |
| `POST /transactions/{transaction_id}/link-transfer` | `{target_id}` manually links two transactions as a transfer pair (sets `transfer_pair_id` both ways + `transfers` category); `409 already_paired` if either already paired. **M107:** a split parent among the two → **409 `transaction_is_split`**, a child → **409 `split_leg_immutable`**. |
| `POST /transactions/{transaction_id}/unlink-transfer` | Clears `transfer_pair_id` on both sides. **M107:** split parent → **409 `transaction_is_split`**, child → **409 `split_leg_immutable`**. |
| `GET /transactions/transfer-suggestions?account_id=` | Unpaired transactions (manual or committed rows only; `transfer_pair_id IS NULL`) with opposite `amount_native`, equal abs(amount), date within ±10 days. Deduplicated (each pair once, `source_id < candidate_id`). Optional `account_id` filter. -> `{items: [{source_id, source_account_id, source_account_name, source_amount, source_date, source_payee, candidate_id, candidate_account_id, candidate_account_name, candidate_amount, candidate_date, candidate_payee}]}` **M107 (IDENTITY):** children are never listed, split parents still are — a statement line pairs with a statement line. Accepting such a listed parent is refused by the route above (`409 transaction_is_split`), which is the intended outcome, not a bug: pairing would write `category_id` on an inert row. |
| `POST /transactions/accept-transfer-suggestion` | `{source_id, candidate_id}` — validates both exist, belong to user, are unpaired; finds "transfers" system category; sets `category_id` and `transfer_pair_id` bidirectionally. Returns updated source `TransactionPublic`. **M107:** BOTH ids are guarded — a split parent → **409 `transaction_is_split`**, a child → **409 `split_leg_immutable`**. The candidate scan (docs/44 §2) is LEDGER, so a new suggestion can never target a parent; the guard closes rows that were already there. |
| `GET /transactions/unreviewed-count` | **M106 (docs/09 §Review queue).** `200 {count}` — how many rows `reviewed=false` would return **in the caller's current filter scope** (every `GET /transactions` query param is accepted; `reviewed` itself is ignored here, so the header hint and the queue chip read one number that cannot cancel itself out). One indexed COUNT over `ix_transactions_user_unreviewed`, deliberately its own route rather than a field on the page: `GET /transactions` carries a 5-query budget (docs/34 — 4 before M108 P4 added the batched receipt flag) and the hint needs the number while the queue filter is off. |
| `POST /transactions/{transaction_id}/review` | **M106 (docs/09 §Review queue).** `{reviewed: bool}` (`extra=forbid`) → `200 TransactionPublic` with `reviewed_at`/`reviewed_by` set or cleared. Idempotent: asking for the state a row already has writes nothing (no state change, no receipt). The row toggle AND the bulk toolbar's **page** selection drive this single-row route — N rows are N sequential requests, no bulk schema (docs/09 §Bulk selection toolbar). The toolbar's **matched** selection drives `POST /transactions/bulk/review` instead: ONE request, same idempotency, same receipts. Pending-batch row → `409 transaction_in_pending_batch`; unknown/foreign id → `404 not_found`. |
| `GET /transactions/{transaction_id}/events` | **M106 (docs/02 §transaction_events, docs/09 §History drawer).** `200 {items: [{id, tx_id, event_kind, actor, actor_label, at, payload_hash, summary, payload}], total}` — every receipt for one transaction, **newest first**, unpaged (one row's history is bounded by how often it was touched). `at` is the receipt's `created_at`; `actor_label` is `"you"` for the caller's own receipts and the actor's name otherwise; `summary` is rendered server-side from `event_kind` + `payload` (`"you set category Groceries"`), so the sentence can be reworded without a migration; `payload_hash` is the canonical sha256 the payload re-derives to. **M120:** a `categorized` receipt written by the pipeline or by an accept/backfill route carries an extra payload key `via` (`rule | learned | llm | rule_backfill | suggestion_accept`), which is what renders the parenthetical — `"the system set category Groceries (rule)"`, `"you set category Groceries (accepted suggestion)"`. A plain PATCH receipt has no `via` and renders exactly as before; `summary` still arrives pre-rendered, so no client parses `payload`. Unknown/foreign `transaction_id` → `404 not_found` — including a row the caller deleted (the drawer has no row to open; the receipt itself survives in the table and in `/export/full`). |
| `PUT /transactions/{transaction_id}/receipt` | **M108 P4 (docs/02 §transaction_receipts, docs/09 §Receipt images).** multipart form, field `file` → `200 {transaction_id, content_type, size_bytes}`. ONE image per transaction: a second upload REPLACES the first (`ON CONFLICT`-style upsert on the UNIQUE `transaction_id`), never accumulates. Before any row is written: declared `Content-Length` over `RECEIPT_MAX_BYTES` (2 MB) → **413 `receipt_too_large`** (refused before the body is read; the same cap is re-checked against `len(raw)` for chunked bodies), zero bytes → **422 `receipt_empty`**, and a body whose MAGIC BYTES are not `image/webp\|jpeg\|png\|gif` OR whose bytes do not match the declared `Content-Type` → **422 `receipt_unsupported_type`** (the header is the client's claim; the sniff is the truth, and they must agree because we serve the bytes back from our own origin). Unknown/foreign transaction → **404 `not_found`**; demo tenant → **403 `demo_account_restricted`** (central guard); over 12 uploads/minute → **429** (`RECEIPT_UPLOAD_LIMIT`, `human_paced_limit`). |
| `GET /transactions/{transaction_id}/receipt` | The image itself — `200` with the stored `Content-Type` as `Content-Type`, `Cache-Control: private, max-age=300` (per-user financial bytes: a shared proxy must not keep them, the browser may). No attachment/content-disposition: it opens inline as an image. Missing/foreign → **404 `receipt_not_found`**; foreign transaction → **404 `not_found`** (the caller cannot distinguish "no image" from "not yours" — both are a plain 404). |
| `DELETE /transactions/{transaction_id}/receipt` | Removes it → **204**, **idempotent**: 204 whether or not one existed. Deliberately not a 404-on-absent — the row's chip and the editor's field both vanish on success, and a double click or a second tab must not turn that into an error a person has to read. Unknown/foreign transaction → **404 `not_found`**; demo tenant → **403 demo_account_restricted**. |
| `GET /accounts/ledger?account_id=` | Per-account ledger: committed transactions ordered by `date ASC, id ASC`, each with `running_balance` (starting_balance + cumulative sum). -> `{items: [{transaction_id, date, description, amount, running_balance}], starting_balance, current_balance, total}` **M107 (LEDGER):** split parents drop out of both the rows and `current_balance`/`total` — the chart's rows are the rows its sums are made of, so a split moves no account balance by a cent. |

CRUD applies to manual rows and rows from `committed` batches. Rows in `pending_review` return `409 {detail, code: "transaction_in_pending_batch"}`; discarded rows are not live/addressable. CAD uses only the exact transaction-date FX cache row and remains `NULL` when unavailable. Account/category/transaction tenancy failures return the existing `404 {detail, code: "not_found"}` envelope; dedup conflicts return `409 {detail, code: "duplicate_transaction"}`. Tag attachment is a transaction sub-resource — its four operations (read, add one, replace all, detach one) are specified under **Tags**, as is the `tags` field embedded on every list item. The receipt IMAGE is likewise a transaction sub-resource: its three operations are specified in the table above and the flag they maintain (`has_receipt`) is the only trace of them on a list row.

### Split transactions (M107 — docs/44)

Four routes, all under `/api/v1/transactions/{transaction_id}/split`, all JWT, tenant-scoped, `extra=forbid` bodies, Decimal strings on the wire, **no route-specific rate limit and no NEW demo-tenant rule**: the global method-scoped guard inside `get_current_user` already answers `403 demo_account_restricted` for these three writes on the demo tenant, exactly as it does for `POST`/`PATCH`/`DELETE /transactions` (none of them is exempt — `GET …/split` is a read and stays open). Splitting never changes a money total anywhere: children sum exactly to the parent in native *and* CAD (docs/44 §3).

**Request body — POST (create) and PUT (replace):**

```jsonc
{ "children": [
    { "amount_native": "-120.00",   // Decimal string, required; sign must match the parent
      "category_id": 4,             // int | null
      "note": "kids clothes",       // str ≤ 500 | null (plaintext, docs/33)
      "tag_ids": [2, 7],            // int[] default []; every id ownership-checked
      "amount_cipher": "v1.…",      // REQUIRED when users.encryption_required (same rule as POST /transactions)
      "cad_amount_cipher": null }   // same rule as POST /transactions today
] }
```

**Response — `TransactionSplit` (201 POST, 200 PUT/GET):**

```jsonc
{ "parent": SplitParentPublic,        // the real parent row + child_count
  "children": [TransactionPublic, …], // id ASC = entry order
  "split_checksum": "-187.42",
  "sum_ok": true }                    // sum_ok is the stored split_checksum compared against the legs (M127) — see the GET row above
```

`TransactionCreate` does **not** gain `note` — a created or imported row never arrives pre-split; splitting is always a separate, explicit action. Children of a split **copy** the parent's `reviewed_at` / `reviewed_by` (docs/02 §transactions): the statement line was reviewed and its parts are the same money, so a split never re-queues a row the user already signed off on.

**Validation order — the tests assert *which* code wins.** POST and PUT run all 13 steps; GET and DELETE `/split` stop after step 5. The body is parsed **inside the handler** (a raw `dict` validated at step 6), which is what makes steps 1–5 genuinely outrank a malformed body: a request aimed at a child answers `split_nested` even when its payload is garbage — a typed FastAPI body parameter would have run `validation_error` first.

1. `owned_or_404` → `404 not_found`
2. live row → `409 transaction_in_pending_batch`
3. row is a child (`parent_split_id IS NOT NULL`) → `422 split_nested`
4. account type ∈ `INVESTMENT_ACCOUNT_TYPES` → `422 split_account_not_allowed` (docs/44 §13)
5. POST: parent already `is_split` → `409 already_split` · PUT/GET/DELETE: not split → `409 not_split`
6. body shape (extra key, wrong type, `note` > 500, blank) → `422 validation_error`
7. `users.encryption_required` and any child missing `amount_cipher` → `422 encryption_required`
8. fewer than 2 children → `422 split_too_few_legs` · more than 20 → `422 split_too_many_legs`
9. parent `amount_native == 0` → `422 split_zero_parent`
10. per child: `0.00` → `422 split_leg_zero`; sign ≠ parent's → `422 split_leg_sign`
11. `category_id` / every `tag_id` ownership → `404 not_found`
12. Σ children ≠ `split_checksum` → `422 split_sum_mismatch` — detail states the expected sum and the residual (`Children must sum to -187.42; they sum to -177.42 (residual -10.00).`) and **zero rows are written**
13. insert everything in ONE transaction, then ONE `split` receipt on the parent (docs/02 §transaction_events); a unique race → `409 duplicate_transaction` (same path as create — unreachable in practice, child hashes are salted)

**New error codes** (added to the master list below):

| code | HTTP | raised when |
| --- | --- | --- |
| `split_account_not_allowed` | 422 | split requested on a row in an investment account (detail names the account type); refused before any write, so no child, no `trades`, no `acb_pools` row exists |
| `split_nested` | 422 | splitting a child — nesting never happens |
| `already_split` | 409 | POST on a parent that is already split (replace is `PUT`) |
| `not_split` | 409 | `PUT`/`GET`/`DELETE` `/split` on a row that is not split |
| `split_too_few_legs` | 422 | fewer than 2 children |
| `split_too_many_legs` | 422 | more than 20 children |
| `split_zero_parent` | 422 | parent `amount_native` is `0.00` |
| `split_leg_zero` | 422 | a child amount is `0.00` |
| `split_leg_sign` | 422 | a child amount has the opposite sign to the parent |
| `split_sum_mismatch` | 422 | children do not sum to `split_checksum` |
| `transaction_is_split` | 409 | ledger-field `PATCH`, `DELETE`, or a tag/transfer write aimed at a split parent; also `POST /suggestions/{id}/accept` whose target has since been split (it writes `category_id`, a LEDGER field) |
| `split_leg_immutable` | 409 | identity/money field `PATCH` or `DELETE` on a child, a transfer write aimed at a child, or reconciliation-linking a child |
| `encryption_required` | 422 | existing code, NEW trigger: a child without `amount_cipher` under `users.encryption_required` |

Still reachable and unchanged on these routes: `validation_error` (422), `not_found` (404), `transaction_in_pending_batch` (409), `duplicate_transaction` (409).

**Mutation matrix (D8/D9).** While a split exists the parent admits exactly one edit — merge. Ledger-field `PATCH`/`DELETE`/tag writes/transfer writes on it → `409 transaction_is_split`; a **cipher-only** `PATCH` stays 200 (the docs/33 A17 backfill). Children own `category_id`, `note`, ciphers, tags and review state (200) and are frozen on identity/money (`409 split_leg_immutable`). **Deliberately not guarded anywhere:** `POST /transactions/{id}/review`, recurring accept/suppress and reconciliation-linking the *parent* — review state and the recurring badge are identity-row properties, and a statement line legitimately matches a statement row. The full matrix is in docs/19 (docs/44 §5).

**Population.** `GET /transactions` and `GET /partner/transactions` answer LEDGER rows in `items` plus `split_parents` (see the table row above). Reports, budgets, dashboards, balances and the browser report engines count children and exclude parents; imports, exact/near-dedup, OFX export, search and recurring detection keep the parents and exclude children (docs/44 §2 names every consumer).


## Bank sync (M79, docs/39)

Router `backend/app/api/sync.py`, prefix `/api/v1/sync`. Every handler depends on
`get_current_user` (demo write guard + privacy locks apply unchanged) and `get_db`.

| Method/Path | Shape |
| --- | --- |
| `GET /sync/providers` | → 200 `[{slug, display_name}]`, stable slug order. Registry read: no provider call, so it works under privacy mode and for the demo tenant. |
| `GET /sync/providers/{slug}/institutions` | → 200 `[{ref, name, country}]`. Unknown slug → **404 `sync_provider_unknown`**. May reach the provider → privacy-guarded. |
| `POST /sync/agreements` | `{provider: str, account_ids: [int]}` (`extra="forbid"`) → **201** `AgreementPublic` `{id, provider, consent_id, status, account_ids, scopes, provider_status, last_sync_at, last_error, created_at, revoked_at}`. `account_ids` order decides the provider's account-ref mapping. Errors: **422 `validation_error`** (empty list), **404 `not_found`** (unknown/foreign account), **409 `sync_agreement_exists`** (another active agreement for `(user, provider)`), **422 `sync_account_mapping_incomplete`** (the grant does not cover every requested account), **404 `sync_provider_unknown`**. Rate **10/min**. |
| `GET /sync/agreements` | → 200 `[AgreementPublic]`, newest first. |
| `GET /sync/agreements/{agreement_id}` | → 200 `AgreementPublic`; unknown or another tenant's → **404 `sync_agreement_missing`**. |
| `POST /sync/agreements/{agreement_id}/pull` | → 200 `PullResponse` `{agreement, staged_batch_ids: [int], preview: [PreviewRow], rows, imported, duplicates_skipped, unresolved, balances: [{account_id, amount, currency, as_of}], dropped_rows}`. Rows stage into `pending_review` batches through the shared `stage_rows` pipeline (docs/03) — review/commit through the existing `/imports` routes. Errors: **409 `sync_agreement_revoked`**, **409 `sync_auth_required`**, **403 `privacy_mode_blocks_external_api`**, **413 `too_many_rows`**, **502 `sync_provider_unavailable`**, **422 `encryption_required` (M181 — refused before any provider call for a tenant with `users.encryption_required`; a server-side pull cannot produce ciphertext, so it is disabled while encryption is on — docs/33, docs/39 §9)**. Rate **10/min**. |
| `POST /sync/agreements/{agreement_id}/revoke` | → 200 `AgreementPublic` with `status: "revoked"`. Idempotent. Local revocation always wins; a provider-side failure is recorded in `last_error` instead of blocking the disconnect. Errors: **404 `sync_agreement_missing`**, **404 `sync_provider_unknown`**, **403 `privacy_mode_blocks_external_api`**. Rate **10/min**. |

`GET /accounts` additionally returns `last_sync_at` (ISO timestamp or null) and
`provider_status` (`"ok" | "degraded" | "auth_required" | null`) — the status-page
groundwork. NULL `provider_status` means "not linked to a live agreement".

Full semantics, the error table and the E2EE disclosure live in docs/39.

## Search
| Method/Path | Shape |
| --- | --- |
| `GET /search?q=&payee_token=&limit=&hide_encrypted=` | Global lookup (docs/33 §Server-first computing): **transaction hits come from TWO sources, in order (M137)** — the exact `payee_token` blind-index equality first (the HMAC the client computes when the vault is unlocked; indexed user-scoped lookup, docs/03 staging enforced, `score` = 1.0), then the pg_trgm matcher (`greatest(similarity, word_similarity) ≥ 0.3` over `normalized_payee`) filling the remaining window. Both halves use the **IDENTITY** population rule (docs/44 §2 — a split statement and its parts never both surface). `hide_encrypted=true` drops cipher rows from the fuzzy half for a locked-vault client and the response reports how many with **`withheld`**. Plus four trigram-ranked metadata groups (plaintext-by-design v1): categories (top 3 `{id, name}`), accounts (top 3 `{id, name, type}`), securities (top 3 `{id, symbol, name, kind, exchange}`), tags (top 3 `{id, name}`). -> `{query, transactions, categories, accounts, securities, tags, withheld}`; unknown text yields empty groups, never 404 |

## Reports

**Owner population contract (M179 ruling).** These TEN GET routes declare an optional query parameter `scope`, default `own`, with the singleton allowed value `own`: `/reports/cashflow`, `/reports/net-worth`, `/reports/cashflow-sankey`, `/reports/total-return`, `/reports/investment-performance`, `/reports/capital-gains`, `/reports/dividends`, `/reports/dividend-calendar`, `/reports/cashflow-calendar`, and `/reports/networth-snapshot`. OpenAPI publishes the `own` constant and default. Any supplied value other than `own` (including `household`, unknown strings, and empty) receives FastAPI's ordinary query-validation **422 `validation_error`**, located at `query.scope`; no report-specific error code is introduced. Every implemented CSV/HTML format twin follows the same validation. Omitted `scope` and `scope=own` have byte-identical 200 responses. This explicit declaration refuses an unsupported financial population rather than silently returning own-only figures. (`/reports/insights` and `/reports/spending-comparison` were in this set until the Owner-lens expansion widened them — they now carry `scope=own|household` and are specified in their own rows below.)

Five routes accept `scope=own|household`: the three Owner-lens tabs `/reports/spending`, `/reports/spending/categories` and `/reports/category-tag-matrix`, plus the Owner-lens expansion pair `/reports/insights` and `/reports/spending-comparison`, whose entire population is committed transactions — the one data set the M93 projection delivers (docs/43 §6). Every one of them runs the same household allowlist, privacy, staging and arithmetic contract (docs/12 §Owner dimension; docs/43 §6), and CSV behavior stays unchanged where a twin exists; unknown and empty values still 422 at `query.scope` on all five. This query parameter is not global middleware: `/export/*` and `/budgets` contracts are unchanged.

| Method/Path | Shape |
| --- | --- |
| `GET /reports/spending?from=&to=&granularity=&group_by=&category_id=` | **M124** — `{from, to, granularity, group_by, items, total_cad, spend_total_cad}`. `group_by` ∈ `category` (default) `| tag`; the ITEMS SHAPE FOLLOWS THE DIMENSION, so a client knows which one it got by which fields are present: `category` → `{bucket, category_id \| null, category_name, total_cad}` (byte-identical to pre-M124), `tag` → `{bucket, tag_id \| null, tag_name, total_cad}`. Under `tag` a multi-tag transaction counts in EVERY one of its tag buckets, so `items` sum to MORE than was spent; `spend_total_cad` is the once-counted denominator the UI's honesty label points at (docs/12 §Tag dimension). `category_id` is the drill filter — a category id (that category **and its descendants**) or the literal `uncategorized`; composes with `group_by` (`group_by=tag&category_id=<id>` = "this category, by tag"); a malformed value → **422 `invalid_category`**. `total_cad` remains the sum of `items`. **UI reads this route since M137 P0-2** (docs/33 §Server-first rule 1 — the browser renders the payload the server computed); M160 (P2-5) RETIRED `lib/reportEngine.ts` — this route is the only implementation and the browser renders it (docs/33 §Server-first rule 5, "the mirrors retire"); reads the mandatory **plaintext columns** (docs/33 A19/F6). **M159** — `scope` ∈ `own` (default) \| `household`, the household population docs/12 §Owner dimension pins: the caller's own committed rows **PLUS** the owner's allowlisted rows reached through the caller's own active `sharing_links` row (docs/43 §6). Tenancy: the counterpart id and the account allowlist come only from that link — never from the request — so a caller with no link, and an OWNER-seat caller (M90's grant is one-directional, docs/43 §2), get their own rows under either value; a dangling allowlist entry is skipped at read time (docs/43 §3 I6). Buckets, `granularity`, `group_by`, the `category_id` drill, zero-fill and the sign convention are byte-identical to `own` — the scope changes WHO is summed, never the math — and category/tag names resolve from BOTH tenants' trees (a projected row carries the OWNER's ids, docs/43 §6; the counterpart's entry names a bare-id conflict). Anything else than `own`/`household` → **422**. **CSV-parity obligation**: `format=csv` under `scope=household` re-runs the same computation over the same scope, so the CSV's rows and the JSON `items` are the same population, pinned by `tests/test_reports_household.py::test_household_csv_carries_the_same_rows_as_the_json`. Parity gate: `node frontend/scripts/parity/parity.ts --slice household` (M135 §4.3) must be 0 diffs before the Owner lens stops computing in the browser. **UI reads this route for the Owner lens since M159**; M160 (P2-5) RETIRED `lib/reportEngine.ts` — this route is the only implementation and the browser renders it (docs/33 §Server-first rule 5, "the mirrors retire"); reads the mandatory **plaintext columns** (docs/33 A19/F6). Route retained for `format=csv` + non-browser consumers — both must stay numerically identical. |
| `GET /reports/spending/categories?from=&to=&group_by=` | **M124** — `{group_by, items, total_cad, spend_total_cad}`; items are `{category_id \| null, name, cad_total}` (root roll-up, descending) or `{tag_id \| null, name, cad_total}` when `group_by=tag` (docs/12 §1). **UI reads this route since M137 P0-2** (docs/33 §Server-first rule 1); M160 (P2-5) RETIRED `lib/reportEngine.ts` — this route is the only implementation and the browser renders it (docs/33 §Server-first rule 5, "the mirrors retire"); reads the mandatory **plaintext columns** (docs/33 A19/F6). Route kept for CSV/API (the client mirror that used to be diffed against it is gone, so "numerically identical" is now a property of one implementation). **M159/M179 — `scope` ∈ `own` (default) \| `household`**, the SAME parameter and the SAME contract as `GET /reports/spending` (docs/12 §Owner dimension, docs/43 §6): the caller's own committed rows **PLUS** the owner's allowlisted rows reached through the caller's own active `sharing_links` row — deny-by-default, one-directional (an OWNER-seat caller gains nothing, docs/43 §2), a privacy-mode owner projecting NOTHING (docs/43 §9 D4), a dangling allowlist entry skipped at read time. `group_by`, the root roll-up, the descending order and the sign convention are byte-identical under either value: the scope changes WHO is summed, never the math. `format=csv` re-runs the same computation over the same scope. **This row previously described the route without `scope` at all — the parameter has existed since M159 (M159 QA must-fix F-Q1); the sentence above is that correction.** |
| `GET /reports/category-tag-matrix?from=&to=&scope=` | **M124 (docs/12 §Category × Tag matrix).** Cross-tab for the Categories tab: `{from, to, columns: [{tag_id \| null, tag_name}], rows: [{category_id \| null, category_name}], items: [{category_id, category_name, tag_id, tag_name, cad_total}], total_cad}`. `items` are LONG FORM and sparse (zero cells absent), ordered row-major by (row, column); `columns` = the top 8 tags by spend (desc, name tiebreak) + an `Other tags` column absorbing the spend of every tag past the cut + `Untagged` always last; `rows` = RAW categories with spend > 0 (**not** root-rolled — the answer to "Groceries tagged vacation" is the child row). Cells count a transaction once PER TAG, so they can sum past `total_cad`, which is the once-counted figure. Two aggregate SQL statements (`GROUP BY (category_id, tag_id)` over a LEFT JOIN through `transaction_tags` + one `GROUP BY category_id`). `&format=csv` → the long-form columns with headers pinned to the JSON field names. **M179 — `scope` ∈ `own` (default) \| `household`**: the SAME population and the SAME contract as `GET /reports/spending` (docs/12 §Owner dimension, docs/43 §6). The matrix is the Categories tab's SECOND panel, so it must sum what the donut above it sums or one screen shows two populations; `rows`/`columns` are ranked and cut exactly as under `own` (the top-8 cut is over the household's tag spend), and category/tag NAMES resolve from BOTH tenants' trees — the counterpart names a bare-id conflict, because a projected row carries the OWNER's ids (docs/43 §6). Tenancy is the link's, never the request's: deny-by-default, one-directional (owner seat gains nothing), privacy-mode owner projects nothing. `&format=csv` under `scope=household` re-runs the same computation over the same scope, so the CSV rows and the JSON `items` are one population (pinned by `tests/test_reports_household.py::test_matrix_household_csv_carries_the_same_rows_as_the_json`). Anything but `own`/`household` → **422**. **UI reads this route for the Owner lens since M179**; under `own` every request is byte-identical to the pre-M179 URL (`own` stays off the wire). |
| `GET /reports/cashflow?from=&to=&granularity=` | `{from, to, granularity, buckets: [{bucket, income_cad, expenses_cad, net_cad}]}`. **UI reads this route since M137 P0-2** (docs/33 §Server-first rule 1; trade cash actions + cache-only fx are the route's own inputs); M160 (P2-5) RETIRED `lib/reportEngine.ts` — this route is the only implementation and the browser renders it (docs/33 §Server-first rule 5, "the mirrors retire"); reads the mandatory **plaintext columns** (docs/33 A19/F6). Route kept for CSV/API. |
| `GET /reports/net-worth?from=&to=&granularity=` | `{from, to, granularity, buckets: [{bucket_end, cash_cad, positions_cad, net_cad, assets_cad, liabilities_cad}]}` — `assets_cad − liabilities_cad == cash_cad + positions_cad` on the raw sums (M97 presentation split for the net-worth tooltip; each cell rounds on its own, docs/12 §4). **UI reads this route since M137 P0-2** (docs/33 §Server-first rule 1); M160 (P2-5) RETIRED `lib/reportEngine.ts` — this route is the only implementation and the browser renders it (docs/33 §Server-first rule 5, "the mirrors retire"); reads the mandatory **plaintext columns** (docs/33 A19/F6). Inputs are committed rows + cache-only fx/prices (docs/33 A19/F6); route retained for `format=csv` + non-browser consumers — both must stay numerically identical. `format=csv` emits the four original columns only. The frozen `s2b-vectors.json` golden (7 ranges) is replayed against this route by `backend/tests/test_m160_replay_s2b.py`. |
| `GET /reports/investment-performance?from=&to=` | `{items: [{security_id, symbol, quantity, realized_cad, unrealized_cad \| null, acb_pool_cad \| null, market_value_cad}], totals: {realized_cad, unrealized_cad, market_value_cad}}`. **UI reads this route since M137 P0-2** (docs/33 §Server-first rule 1); M160 (P2-5) RETIRED `lib/reportEngine.ts::investmentPerformanceReport` — this route is the only implementation (docs/33 §Server-first rule 5). Inputs are committed rows + cache-only fx/prices over the mandatory plaintext columns (docs/33 A19/F6); route kept for CSV/API. The frozen `s2b-vectors.json` golden (4 ranges) is replayed against this route by `backend/tests/test_m160_replay_s2b.py`, answered at the capture's `captured_at` (the closing position prices at `today`). Errors the compute can raise (`422 fx_rate_missing / oversell / invalid_corporate_action / invalid_date_range`) keep their server envelopes either way. |
| `GET /reports/dividends?year=` | `year` is OPTIONAL (defaults to latest available / current year). `{year, available_years, items: [{security_id, symbol, name, dividend_cad, distribution_cad, roc_cad, total_cad, payment_count, last_payment_date}], by_month: [{month, total_cad}] (12 zero-filled), totals: {dividend_cad, distribution_cad, roc_cad, total_cad}, ttm_total_cad, unresolved_fx, provenance: {status: \"ok\"\|\"stale\"\|\"missing\", as_of: date-string\|null, sources: [{key, kind: \"ledger\"\|\"market_data\", label, provider: string\|null, as_of: date-string\|null, age_days: integer\|null, status: \"ok\"\|\"stale\"\|\"missing\", detail}]}}`. The `provenance` block (docs/07 §Dividend-view provenance) is JSON-only — `format=csv` stays a row export of `items`. It lists `income_trades` (ledger; `as_of` = newest recorded income payment in the selected year) and, only when that year converts a foreign denomination, `trade_date_fx` (market data: distinct `fx_rates.source` values, newest cached rate for those bases, `missing` when `unresolved_fx > 0`, `stale` past 7 days). **UI reads this route since M160-R2 (t_85311c34)** — `api/reports.ts::getDividendReport` is the thin read `DividendsPanel` renders (M137 P0-2 pattern); the S2c client port retired with M160 / P2-5, and the shim that temporarily carried it was deleted by M160-R2. The route's arithmetic is pinned by `backend/tests/test_dividends.py`, and the frozen `s2c-vectors.json` golden is now REPLAYED against this route (clock pinned to the capture, because TTM is defined relative to today) by `backend/tests/test_m160_replay_s2c.py`. **M160-R2: left the M137 P1-1 orphan set — a live screen route is never gated by `SERVER_REPORTS_DEPRECATED`.** |
| `GET /reports/cashflow-sankey?from=&to=` | **M77 (docs/12 §8).** Three-column money-flow graph: `{from, to, columns: ["source","total","destination"], nodes: [{id, column: "source"\|"total"\|"destination", label, amount_cad, group}], links: [{source, target, amount_cad}], totals: {income_cad, expenses_cad, invested_cad, saved_cad, drawn_cad, money_in_cad}}`. Population/signs are `GET /reports/cashflow`'s; node ids, top-level category roll-up, the balancing `src:drawn` source, per-row cent quantization and amount-desc/label-asc ordering are pinned in docs/12 §8. **UI reads this route since M160-R2 (t_85311c34)** — `api/reports.ts::getCashflowSankey` is the Sankey panel's thin read; `lib/sankeyEngine.ts` survives only as the reference implementation the route was written against (M160-R1 ruling 3) and still owns the `SankeyReport` wire shape the chart renders. The frozen `m77-vectors.json` golden is REPLAYED against this route by `backend/tests/test_m160_replay_m77.py`, and the route's own arithmetic is pinned by `backend/tests/test_reports_sankey.py`. JSON-only — no `format=csv`. `422 invalid_date_range` for `from > to`. **M160-R2: left the M137 P1-1 orphan set — never gated.** |
| `GET /reports/total-return?from=&to=` | **M77 (docs/05 §Total return).** `{from, to, status: "ok"\|"no_data", start_value_cad, end_value_cad, net_flow_cad, income_cad, twr_pct: string \| null, mwr_pct: string \| null, valuation_days, active_days, flow_days, account_count}`. TWR = daily chain-linked `(V_d − CF_d)/V_{d−1}`; MWR = Modified Dietz over the same window; both PERIOD returns, one half-up rounding to a fixed 8-decimal plain string, `null` when undefined (M65 honesty, never a numeric 0). Money fields are 2-decimal strings (`"0.00"` when empty). **This route is the only implementation** — the `lib/totalReturnEngine.ts` client port retired with M160 / P2-5, and **since M160-R2 (t_85311c34) `api/reports.ts::getTotalReturn` is what `TotalReturnMetrics` renders**. The hand-derived pins in `backend/tests/test_total_return.py` are the arithmetic authority, and the frozen `m77-vectors.json` golden is REPLAYED against this route by `backend/tests/test_m160_replay_m77.py`. JSON-only — no `format=csv`. Scope = `INVESTMENT_ACCOUNT_TYPES`; committed rows only; cache-only reads. `422 invalid_date_range`. **M160-R2: left the M137 P1-1 orphan set — never gated.** |

All support `&format=csv`; contracts defined in docs/12. The M124 routes follow the dimension: `/reports/spending` emits `bucket,category_id,category_name,total_cad` or `bucket,tag_id,tag_name,total_cad`, `/reports/spending/categories` emits `category_id,name,cad_total` or `tag_id,name,cad_total`, and `/reports/category-tag-matrix` emits the long-form five (docs/12 §CSV). **Exceptions (both deliberate):** `/reports/cashflow-sankey` and `/reports/total-return` are JSON-only — a graph and a pair of window returns have no flat table to export (same decision as `/projections/*`).

**M179 scope ruling.** The three widened routes `/reports/spending`, `/reports/spending/categories` and `/reports/category-tag-matrix` accept `scope=own|household` as specified above; the Owner-lens expansion adds the same two-value parameter to `/reports/insights` and `/reports/spending-comparison`, whose whole population the projection delivers (docs/12 §Owner dimension). The ten own-only report routes listed in the Owner population contract declare optional `scope=own`; this explicit singleton rejects household, unknown and empty values with FastAPI's standard query-validation 422 instead of silently returning a narrower population. Every actually implemented CSV/HTML twin shares that validation, and the five widened routes reject unknown and empty values the same way. This is report-router validation only: `/export/*` has no scope selector and remains the caller's own tenant snapshot; `GET /budgets` remains the caller's budget data. Tests classify every published report GET route from OpenAPI and pin this split.

**Orphan-report deprecation flag (M137 P1-1, docs/33 §Server-first computing).** Four `/reports/*` routes once had no UI consumer — `/dividends`, `/cashflow-sankey`, `/total-return` (M134 §3.6.2) and `/networth-snapshot` (found later by @qabot). M160-R2 (t_85311c34) gave the first three real consumers (their panels read them now), so **`/networth-snapshot` is the one orphan left**, and `SERVER_REPORTS_DEPRECATED` in `.env` gates exactly that one route and nothing else: **unset/false (the default) → it keeps answering**, so a deployment that never sets the key can never break; **`true` → 410** with the standard `{detail, code: "report_route_deprecated"}` envelope whose `detail` names the route. Every route the screens consume — the original seven (`/spending`, `/spending/categories`, `/category-tag-matrix`, `/cashflow`, `/net-worth`, `/investment-performance`, `/capital-gains`) plus the three M160-R2 re-points — is NOT gated in either state: `assert_report_route_live()` ignores anything outside the orphan allowlist, so a typo (or a re-point landing on the old list) cannot 410 a live screen route. `backend/tests/test_report_route_deprecation.py` asserts both states.

**Performance budgets (M60, docs/34).** Every budgeted read below carries a
CI-enforced ceiling on SQL statements per request, measured on the M10 demo
dataset by `backend/tests/test_perf_budgets.py`:
`GET /reports/spending/categories` ≤ **3**, `GET /reports/net-worth` ≤ **7**,
`GET /reports/cashflow` ≤ **4**, `GET /reports/investment-performance` ≤ **7**,
`GET /reports/capital-gains` ≤ **3**, `GET /search` ≤ **5**, and
`GET /transactions` ≤ **5** at any `limit` — **6** when the page contains split children (the ONE conditional `split_parents` fetch, docs/34; the count is page-size independent,
not per-row). Each also has a p95 latency target; the full table, the
measurement method and the waivers live in docs/34.

## Suggestions & rules

| Method/Path | Shape |
| --- | --- |
| `GET /suggestions?status=suggested` | SUGGEST-ONLY queue items |
| `POST /suggestions/{id}/accept` | Applies category, writes learned-map entry (docs/04 loop). **M120: writes receipts** — one `categorized` event on the target row in the same commit, payload `{category_id, category_name, via: "suggestion_accept", suggestion_id}`, rendered `"<actor> set category <name> (accepted suggestion)"`; the `409`/`404`/`409 transaction_is_split` exits write none. **M107:** a target that has since been split → **409 `transaction_is_split`** (accept writes `category_id`, a LEDGER field — the candidate scan is LEDGER so a new suggestion can never offer a parent, this closes the row that was already there) |
| `POST /suggestions/{id}/reject` | Negative signal recorded |
| `POST /rules/preview` | **RE-MOUNTED (M137 P1-2, docs/31 §Preview, docs/33 §Server-first computing).** Dry-run a rule draft: `{match_type, pattern, from?, to?, only_unassigned=true, limit=25 (1–100)}` (`extra=forbid`) → 200 `{matched_count, assigned_skipped, items: [{id, date, payee, amount_native, category_id, category_name}]}` newest-first, **never writes**. The verdict is the SERVER's: the same `match_transactions` engine `POST /rules/{id}/apply` writes with, over the same committed scope — so a preview and the apply it precedes cannot disagree. 422 `invalid_regex` / `validation_error` from the shared `_validate_pattern`. M160 (P2-5) RETIRED the `previewRule` client twin with `s3Engine.ts` — this route is the only engine. The captured `frontend/tests/fixtures/s3-vectors.json` goldens now pin the ROUTE, replayed by `backend/tests/test_m160_replay_s3.py`. |
| `POST /rules/{id}/apply` | Targeted backfill: `{mode: all\|range\|forward, from?, to?, overwrite=false}` → `{rule_id, mode, matched_count, applied_count, note?}`. `range` needs from<=to (422 `invalid_date_range`); `forward` writes nothing, pipeline covers new imports (docs/31). **M120: writes receipts** — one `categorized` event (`via: "rule_backfill"`) per row `applied_count` reports, plus a `tagged` event when the rule's tags moved the junction, all in the route's single commit; rows skipped under `overwrite=false`, a zero-match run and `mode=forward` write nothing (docs/02 §transaction_events) |

## Investments, tax, market data

| Method/Path | Shape |
| --- | --- |
| `GET /securities/lookup?symbol=` | yfinance autofill (docs/05, docs/07 §Symbol search fallback + §Candidate resolution). `market_data.resolve_symbol` answers in three ways — `direct`/`suffix` (the typed TICKER resolved; `symbol` is what was sent), `search` (a typed NAME resolved to the ONE listing Yahoo's search returns, and `symbol` is that canonical ticker — `APOTEX` → `APTX.TO`), and `ambiguous` (**more than one listing matched**: several search hits, OR **M143** both the bare and the `.TO` quote candidate — `VGRO` on NYSEArca and `VGRO.TO` on the TSX; the five result fields are `null` and `candidates` is the pick-one chooser — never a silent guess). Shape: `{symbol: string\|null, name: string\|null, kind: "stock"\|"etf"\|"mutual_fund"\|null, listing_currency: string\|null, exchange: string\|null, exchange_display: string\|null, match: "direct"\|"suffix"\|"search"\|"ambiguous", candidates: [{symbol, name, kind, listing_currency, exchange, exchange_display}]}`. **A candidate is create-ready (M143)**: it carries `kind` + `listing_currency`, so the client applies a pick DIRECTLY (one lookup per typed string) and never re-resolves — re-asking would land on the same collision. **M163 (docs/07 §Listing currency)**: `listing_currency` is the provider body's own `currency` field whenever it states a well-formed code (`005930.KS` → `KRW`, `9984.T` → `JPY`, both live-probed), and the `.TO`/Canadian-venue heuristic only when it does not — a `search` answer always carries the heuristic, because search rows carry no currency. LSE's minor-unit code (`GBp` = pence) is **M167: a refusal of the whole listing** (docs/07 §A minor-unit listing is refused) — a London body that answers `GBp` resolves `none` here (404), and the name-search fallback drops a London row entirely, because the venue answers both units live (`HSBA.L` `GBp`, `HKLD.L` `USD`) and one of the two readings is a 100× price. A London listing quoted in USD (`HKLD.L`, `AV98.L`) still resolves `direct` with `USD`. No answer at all → 404 `lookup_unavailable` — the dead-symbol state M141's guard consumes, which is also what the add form's UNRESOLVED path (`listing_currency: \"\"`) is built on. Rate-limited 30/minute |
| `POST /securities` | Create: `{symbol, name, kind?, listing_currency, exchange?}` → 201 `SecurityRead`. 409 `duplicate_security` on `(user_id, symbol, exchange)`. **M143 (docs/07 §Unresolved symbols): `listing_currency: ""` is the UNRESOLVED marker** — the row stores the caller's name with no invented venue/currency, is created with `dead_symbol=true` + `dead_symbol_reason`, and gets **no backfill queued** (there is no listing to fetch). A non-empty currency behaves as before: the 365-day backfill runs as a background task and never overwrites manual prices. |
| `GET /securities/quote-check?symbol=` | **M141** add-time symbol validation → `{symbol, quote_found: bool \| null, reason: str \| null}`. `quote_found=false` = Yahoo said no such symbol — an answer body that carries no readable close counts as that too (docs/07 §Body contract); `null` = we could not ask (network/rate-limit) and is NEVER reported as "no quote". Always 200 — a never-fetched symbol is legitimate to watch, so this is a warning surface, never a block. Privacy mode → 403 `privacy_mode_blocks_external_api`. Rate-limited 30/minute (docs/07 §Symbol check) |
| `GET /securities/{id}/overview` | One payload for the security dashboard page: `{security, latest: {date, price, source, age_days, stale, previous_close, day_change_native, day_change_pct} \| null, stats: {high_52w, low_52w, change_pct, cached_days}, history: [{date, price}]` (ascending, ≤365 closes), `positions` (`compute_positions` rows for this security, delta block included), `totals: {quantity, book_cost_cad, market_value_cad, unrealized_gain_cad, unrealized_gain_native, unrealized_gain_pct, day_change_cad}`, `watchlisted`, `recent_trades` (last 10, date desc), `fundamentals: {trailing_eps, pe_ratio, market_cap, dividend_yield_pct, beta, sector, industry, country, fifty_two_week_high, fifty_two_week_low, source, fetched_at} \| null` (7-day cache, silent degradation — docs/07), `holdings: {items, total_weight_pct, source} \| null` (cache-only, null for stocks/empty — docs/05)`}`; `latest.previous_close`/`day_change_*` follow the M129 delta semantics pinned on `/portfolio/positions` (same baseline, `market_data.previous_close`); the `totals` impact fields are the arithmetic sums of the SAME per-position fields the `positions` rows carry, so the card headline and the holdings table can never disagree, and `unrealized_gain_pct` is gain ÷ book over the rows that carry BOTH numbers; foreign id → 404 |
| `PUT /securities/{id}/fundamentals` | Manual override upsert: any subset of the nullable fundamental columns + required `source:"manual"` → row stamped `fetched_at=today`; yfinance refresh never overwrites manual rows; foreign id → 404 |
| `GET /securities/{id}/holdings` | `{items: [{holding_symbol \| null, holding_name, asset_class, region \| null, country \| null, sector \| null, weight_pct}], total_weight_pct, source: "manual" \| "yfinance" \| "issuer" \| "vanguard" \| null}` — majority source (provider rows report the majority PROVIDER, M147; M147-followup adds `vanguard`); null when empty; foreign id → 404 |
| `PUT /securities/{id}/holdings` | `{items: [{holding_symbol?, holding_name, asset_class, region?, country?, sector?, weight_pct}]}` ≤250 rows; sum > 100.01 → 422 `holdings_weight_exceeded` (under 100 legal — residual uncategorized); replaces ONLY manual rows |
| `POST /securities/{id}/holdings/import` | provider-chain import (M147: issuer full CSV first, yfinance top-holdings fallback — docs/07); writes through `market_data.import_fund_composition` — replaces ALL non-manual rows, links `holding_security_id`, repairs a proven-fund `kind=stock` → `etf`; None from every provider → 503 `market_data_unavailable`; privacy 403 passthrough; returns the GET payload |
| `GET /trades?limit=&offset=&account_id=&security_id=&action=&committed_only=` | `TradePage {items: TradeRead[], total, limit, offset}`, ordered (date, id) asc. `TradeRead` carries `currency: string | null` (**M132** — the currency `price_native`/`commission` are stated in; `null` on a row written before M132, which every consumer reads as the ACCOUNT's currency so client and server cash legs agree to the string; docs/02 §trades). Default returns ALL rows including staged import batches — that list is also the review window (docs/03). `committed_only=true` (S2b) applies the shared committed scope (`committed_trade_conditions`); report input loaders pass it so client-side compute reads exactly the population the server reports read. |
| `POST /trades` | Any action from the trades enum. **M131:** a `contribution` with a NEGATIVE `price_native` → **422 `negative_contribution`** (a withdrawal is the honest spelling of money leaving; docs/02 §Signed-amount convention). **M132:** the row records the ACCOUNT's currency (the create form carries no currency field); `POST /trades/record-dividend` records the SECURITY's listing currency, because a suggested amount is per-share in it. Unknown keys are **422 `validation_error`**, not silently discarded (`extra="forbid"` on `TradeCreate`/`TradeUpdate` — sending `currency` here is rejected by name rather than accepted-and-dropped; QA nit, M132 acceptance). **M181:** `422 encryption_required` for a tenant with `users.encryption_required` — `trades` has no cipher columns, so a manual trade is refused exactly like an imported one; the same guard applies to `PATCH /trades/{trade_id}` (after ownership + investment-account checks), while `DELETE /trades/{trade_id}` stays available (removal only — docs/33 § exclusion table). |
| `POST /securities/{id}/refresh` | Force-refresh market data (price + fundamentals) for one security; returns the overview payload (docs/05). **M141:** the manual "Try refresh" — the ONE path that probes a `dead_symbol` row (`probe_dead=True`), so a flag Yahoo has since disproved clears itself on the next real close; a still-dead symbol simply re-arms it. Every AUTOMATIC path skips the row (docs/07 §Dead symbols). |
| `POST /securities/{id}/split-repair?start_date=&end_date=` | **M149 (docs/07 §Split repair)** re-cache one window in the ledger's as-traded frame. Required `start_date`/`end_date` (`YYYY-MM-DD`; `start > end` → **422 `invalid_date_range`**). → 200 `SplitRepairRead {security_id, symbol, start, end, fetched, splits: [{date, ratio}], updated, inserted, unchanged, skipped_manual, refused}` — `ratio` is a Decimal string (`"8.0"` for an 8:1, `"0.1"` for a 1:10 reverse split); `refused: "no_closes"` means Yahoo answered nothing for the window and the cache was left untouched. Re-fetches through the normalizing seam and rewrites `source=yfinance` rows in place at the column's `Numeric(20, 8)` precision, so it is idempotent (a second run reports `updated=0`); `source=manual` rows are counted as `skipped_manual` and never written. The M141 verdict of the refetch is booked like any other fetch. Foreign id → 404; the demo tenant's write guard rejects it; privacy mode → 403 `privacy_mode_blocks_external_api` with nothing written |
| `GET /securities/{id}/holdings?recursive=true` | Recursive `asset_class_breakdown` for a fund (depth ≤ 3, cycle-safe); `recursive=false` (default) returns one level (docs/05, docs/16). |
| `GET /trades/dividend-suggestions?account_id=` | `DividendSuggestion[]` — pending dividends recomputed from current committed/manual trades and tenant-scoped raw provider history for open positions (same-day trades included; recorded income trades suppress matching account/security/date). Missing source history is not treated as no dividends. Additive `X-Dividend-History-State` header is JSON `{pending, unavailable, empty, stale, quote_miss, missing, source, as_of, age_days}`: `pending` counts queued or active refreshes; `unavailable` and `empty` distinguish failed/unavailable from successful no-event responses; `stale` counts only histories with a prior successful refresh older than 24 hours (never-successful sources are pending/unavailable, not stale); `quote_miss` (M186) is the subset of `unavailable` whose failure is a hard Yahoo quote miss or the M141 dead-symbol flag — a verdict about the symbol, where automatic retry cannot help; `missing` counts sources that have NEVER answered (`last_success_at` null — never counted as stale); `source` is always `yfinance` (the `dividend_history.source` CHECK); `age_days` is the whole-day age of `as_of`, `null` with it; each count is per distinct tenant/security source in scope; `as_of` is the oldest successful refresh in scope as a nullable UTC timestamp ending in `Z`. Body remains list-compatible. Provider refresh is asynchronous and does not delay this read. |
| `POST /trades/record-dividend` | `DividendRecordInput` → `DividendRecordResult` (201): atomically creates 1–3 rows (income trade + cash transaction + optional DRIP buy). **M181:** `422 encryption_required` for a tenant with `users.encryption_required`, raised after the account/security ownership 404s and **before the first `db.add`** — the route creates a plaintext `transactions` row, so it cannot run for an opted-in tenant.|
| `POST /trades/import/preview?profile=` | **M82 (docs/41) dry run, zero writes.** Multipart `file` + `account_id`; `profile` is a query-parameter slug. → 201 `TradeDryRunResponse {profile \| null, rows: [{index, date, symbol, security_name, action, quantity, price_native, commission, currency, verdict: "new" \| "duplicate", security_action: "existing" \| "create"}], unmapped, notices, summary: {total, new, duplicates, unmapped}}`. A row staging would refuse carries `unmapped[].code = "stuck"`. No `securities` / `trades` / `import_batches` row is written. Same 413/422 chain as the import route; rate **6/min**. **M181:** deliberately NOT guarded — zero writes (pinned: an `encryption_required` tenant gets a real dry run and 0 new rows).|
| `POST /trades/import?profile=` | Investment / brokerage CSV import → staged `trades` batch (same dedup/force-include flow as transactions). `profile` is a **query parameter** (docs/41); absent → header auto-detect, and a Wealthsimple-shaped file runs the legacy parser. → 201 `TradeImportResponse {batch_id, profile \| null, institution \| null, imported, duplicates_skipped, preview: [TradePreviewRow + currency], unmapped: [UnmappedRow + institution \| null + code \| null], notices: string[]}`; `import_batches.institution` is the profile label (or `investment_csv` on the legacy path). `413 file_too_large` / `too_many_rows`; `422 unknown_import_profile` / `profile_header_mismatch` / `unknown_corporate_action` / `parse_error` (stuck row or malformed CSV) / `negative_contribution` (M131 — an EFT/"Deposit" row whose amount is negative; the row is refused, not imported); rate **6/min**. **M181:** `422 encryption_required` for a tenant with `users.encryption_required`, refused before the body is read — `trades` has no cipher columns, so the file is refused rather than asked for a cipher.|
| `POST /trades/import/{batch_id}/commit` | Commit a staged trade-import batch. **M181:** `422 encryption_required` for a tenant with `users.encryption_required` (after the ownership and `pending_review` checks, before any write) — a batch staged before enrolment cannot be committed afterwards.|
| `POST /trades/import/{batch_id}/discard` | Discard a staged trade-import batch (deletes staged trades). |
| `POST /acb/rebuild` | Rebuild pools from trades (docs/02 invariant). Accepts an OPTIONAL request body (`RebuildRequest`); the body is currently unused and the rebuild covers all of the user's pools. |
| `GET /acb?limit=&offset=&account_id=&security_id=` | Current `acb_pools` state. **Kept, not replaced (docs/33 S4)** — the Taxes page renders `id` + `updated_at`, both database-assigned and not derivable from trades, so `capture.ts` records the response as the frozen input `datasets.demo.acbPools` and the client parity port takes only those two columns from it; the pool set, `symbol` / `quantity` / `total_acb_cad`, the sort order and the `total`/`limit`/`offset` envelope are computed client-side from committed trades + cache-only fx. The MCP `acb_list` tool consumes this route too. |
| `GET /reports/capital-gains?year=2025` | Annual per-security summary; `&format=csv` or printable HTML. **Every format is computed by this route** — M160 (P2-5) retired the `lib/reportEngine.ts::capitalGainsReport` client port (docs/33 S4), so `format=json` (the default), `format=csv` and `format=html` are one implementation over committed trades/accounts/securities + cache-only exact-date fx. The frozen golden `tests/fixtures/s4-vectors.json` (14 cases) now pins this route instead, replayed by `backend/tests/test_m160_replay_s4.py`. `year` is `ge=1900 le=2200` (422 `validation_error`). Errors use the `{detail, code}` envelope (`fx_rate_missing`, `oversell`); the retired client engine raised the same codes, and the route's envelopes are pinned by `backend/tests/test_tax_reports.py`. |
| `GET /fx?base=USD&quote=CAD&date=` | Rate with cache/fallback semantics (docs/07) |
| `POST /fx` | Manual gap-fill rate entry (`source=manual`); an existing `(base, quote, date)` row (BoC or manual) → `409 fx_date_not_gap` (first writer wins); `date` after today → `422 fx_future_date`; manual rows fill gaps only (docs/07). |
| `GET /fx?base=&quote=&start_date=&end_date=&fetch=` | **Range form** returns `FxRangeRead` (list of daily rates); `start_date`/`end_date` must be supplied together, else `422 invalid_date_range` (docs/07). `fetch=false` forces cache-only reads (default `true`) — the client report engine passes it so report rendering NEVER triggers a BoC fetch (docs/12 principle, S2a). |
| `GET /fx/board?days=90` | Multi-currency board over `CURATED_FX_BASES` (17 bases → CAD), `FxBoardRead` (docs/07, docs/15). Cache-only (`allow_fetch=False`): it never fetches, so a currency with nothing cached answers `latest_rate: null` / `as_of: null` / `series: []` rather than a 403 under privacy mode. |
| `GET /fx/status` | **M150 (docs/07 §Rate-book status).** Freshness of the rates that price THIS tenant's money, cache-only — never fetches, so a page load can never become a Valet call and privacy mode cannot 403 it. Pairs in use = `trades.currency` (M132 row currency, committed only) ∪ `accounts.currency` (a pre-M132 row with NULL currency reads in its account's currency) ∪ `securities.listing_currency`, minus the reporting currency. → `FxStatusRead` `{reporting_currency, as_of: date\|null, age_days: int\|null, stale: bool, pairs: [{base, quote, as_of, age_days, stale, source: "boc_valet"\|"manual"\|null}]}`. Top level describes the **worst-covered** pair; `stale` is true when ANY pair is stale (past `CARRY_FORWARD_MAX_DAYS` = 7, or no row at all). `pairs: []` = the tenant holds no foreign currency (no FX dependency — the UI chip renders nothing). Errors: `401` unauthenticated only. |
| `GET /api/v1/health` | Liveness probe: `{status, timestamp, checks}`; `200` healthy / `503` if the DB is unreachable. No auth (docs/01). |
| `POST /portfolio/refresh` | BackgroundTasks: `queue_backfill` (365-day `backfill_range`, the same call `POST /securities/{id}/refresh` runs) for every distinct security held ∪ watched, queued **before** the `ensure_fundamentals` fan-out → 202 `{queued, skipped_dead}`; privacy seam applies; works regardless of market hours. M141: rows flagged `dead_symbol` are filtered out before queueing (this sweep is the fan-out whose 404 noise the flag exists to stop) and counted in `skipped_dead`, so the UI names what it did not do; a dead row comes back only through the per-security `POST /securities/{id}/refresh` |
| `GET/POST /watchlist` | Securities tracked but not held; POST `{security_id}` → 409 `already_watched` on duplicate; rows carry latest price + staleness fields **plus the M141 dead-symbol state** (`dead_symbol`, `dead_symbol_reason` — Watchlist section below). Removal is per-security: `DELETE /watchlist/{security_id}` (Watchlist section below) — a collection-level delete does not exist. |
| `GET /portfolio/value-history?from=&to=&granularity=&min_event_value=` | **Contract lives above** (§Portfolio value history, M130) — the whole portfolio's summed value timeline + deduped markers + the server-computed `stats` block the header's range sentence renders. Cross-reference so this index is complete. |
| `GET /portfolio/positions?account_id=` | Per (account, security): signed qty, latest price with `price_date`, source, whole-day `price_age_days` and stale flag, plus `price_fetched_at: datetime | null` from the same tenant-scoped price row (`prices.fetched_at`; NULL means no provider-fetch evidence), market value native + CAD, `book_cost_cad` (ACB pool for non-registered; pure average-cost replay for registered — never persisted), `spark` (last ≤30 closes ascending), plus the **M129 delta block** — `fx_rate_to_cad` (the exact rate used; `"1"` for a CAD-listed holding, `null` when no usable fx row), `previous_close_native` (newest cached close STRICTLY before `price_date`), `day_change_native` (`price − previous_close`), `day_change_pct` (÷ previous close, rounded 2dp), `day_change_cad` (`cents(quantity × Δ × rate)` — the move's impact on THIS holder's shares) and `unrealized_gain_cad` / `unrealized_gain_native` (`market_value − book_cost`, native via `book_cost_cad ÷ rate`) / `unrealized_gain_pct` (gain ÷ book, 2dp). Every delta field is server-computed and `null` when an input is missing — a first-day listing has no previous close and renders no delta, never a zero. Top level gains `total_unrealized_cad`: Σ `unrealized_gain_cad` over the items (rows with an unknown book cost contribute nothing; `null` when no row computes one), and **M130** adds the header block — `total_value_cad`, `cash_cad`, `portfolio_value_cad`, `total_day_change_cad`, `total_day_change_pct`, `day_change_as_of`, `day_change_mixed_baseline` — every one a sum over THIS response's `items` (§Portfolio summary on `GET /portfolio/positions`). Each row also carries `previous_close_date`, the DATE of its M129 baseline, which is what lets the label name a real market day. Optional `account_id` filter scopes to one account. Auto-refreshes prices via background tasks (`ensure_recent_price(force=True)`) on weekdays 09:30–20:00 America/New_York; manual refresh always available via `POST /portfolio/refresh` |
| `GET /portfolio/breakdown?dimension=sector\|geography\|asset_class\|company&account_id=` | `{dimension, items: [{key, market_value_cad, weight_pct, estimated_cad, estimated}] (value desc), total_cad, unclassified_cad, estimated_cad, unclassified_funds: [{security_id, symbol, name, market_value_cad, reason, imported_weight_pct}]}` — look-through resolution order pinned in docs/05; `company` dimension added in M19; `unclassified_funds` (M147) is the honesty copy's evidence: `reason: "missing"` = the fund carries NO composition rows (an Import fills it; `imported_weight_pct` null), `reason: "partial"` = its rows sum to <99 (sub-1% residuals are noise) — the provider published only the top slice (`imported_weight_pct` is what DID import); **M166** `estimated_cad` (top-level and per item, string, cents-quantized) is the value attributed from a fund's own PUBLISHED sector mix rather than a sourced per-holding column and `estimated` is `estimated_cad > 0` — sector chain and the label it drives pinned in docs/16 §Sector chain / docs/09 §Exposure honesty; both are `"0.00"` / `false` on every other dimension and on any portfolio whose funds publish their rows per-holding; invalid dimension → 422 envelope. Optional `account_id` filter scopes to one account. **M169** optional `drill=` (below) adds a `drill` block for ONE bucket. |
| `GET /portfolio/exposure?account_id=` | Holdings grouped by `listing_currency` (docs/05). Optional `account_id` filter scopes to one account. |
| `GET /portfolio/benchmarks?start_date=&end_date=&symbol=` | **Contract lives in docs/28** (§API) — cross-reference so this index is complete. Required `start_date` + `end_date` (`date`), optional `symbol` (default `SPY`; the four picks are `SPY`/`XIU`/`XBB`/`XAW`). Missing/malformed dates → `422 validation_error` (the route raises no domain error code of its own). → `{portfolio_start, portfolio_end, portfolio_return_pct, benchmark_symbol, benchmark_name, benchmark_return_pct, benchmark_points, benchmark_currency, benchmark_listing_currency, benchmark_series_start, benchmark_series_end, benchmark_clipped, excess_return_pct, start_date, end_date}` — money and pct fields are Decimal-as-strings. **M173: the benchmark side is REAL** — a cached index series (docs/07 §Benchmark series), with `benchmark_return_pct` an FX-INCLUSIVE return in `benchmark_currency` or `null` (never a numeric 0 placeholder; docs/28 decisions 5/6), `benchmark_points` the true count of closes in range (M65 — the UI shows "No benchmark data for this window" below 2, docs/28), and `excess_return_pct` `null` exactly when the benchmark return is. `backend/tests/test_m173_benchmarks.py` pins the math; `backend/tests/test_m29_stream.py` pins the Decimal/wire shape and the no-series case. |
| `POST /projections/run` | Deterministic scenario projection (docs/12 §7): monthly compounding, bounds years 1–50 / inflation 0–20% / return −50–50% / 1–8 scenarios → 422 envelope outside. **Demo tenant allowed (M157, docs/20 §6):** the sixth demo-write exemption — the handler takes no `Session` and the run is a pure function of the body, so nothing a visitor computes is visible to the next visitor. Errors: `422 {detail: [<pydantic error>], code: "validation_error"}` |
| `GET /projections/defaults` | `{start_value_cad: string \| null}` — current portfolio market value CAD for UI prefill |
| `POST /suggestions/run` | Kick the categorization pipeline (docs/04 operational contract); BackgroundTasks; optional `{transaction_ids}` |
| `GET /export/full` | Whole-tenant JSON snapshot — M7 privacy plan (every row, staged included) |
| `GET /export/zip?vault=locked\|unlocked` | **M75 one-click export** (docs/11): `application/zip` = `manifest.json` + one `<table>.csv` per exported table, committed rows only, header `X-Export-Masked-Cells`; `vault` default `locked` ⇒ cipher-backed cells carry the `E2EE:<ciphertext>` marker; unknown value → 422 `export_invalid_vault` |
| `GET /export/ofx?account_id=&vault=` | **M75**: one account's committed transactions as an OFX 1.02 statement (`application/x-ofx`, `fin-tracker-<slug>.ofx`); unknown/other tenant's id → 404 `not_found`; `vault` as above |
| `GET /export/schedule` | **M75** → 200 `{frequency, hour, weekday, day_of_month, keep_last_n}` (defaults `off`, 3, 0, 1, 5) |
| `PUT /export/schedule` | **M75** `{frequency, hour, weekday, day_of_month, keep_last_n}` (`extra="forbid"`, full replace) → 200 the same shape; out-of-range/unknown `frequency` → 422 `export_invalid_schedule`; demo user → 403 `demo_account_restricted` (reads stay open) |
| `GET /prices/history?security_id=&from=&to=&granularity=` | **M126, docs/12 §Range → granularity.** Cache-only chart series for ONE security → `{security_id, from, to, granularity, requested_granularity, items: [{date, price}], count}`. `security_id` **required**; foreign/missing id → `404 not_found`. `from` optional (absent = the whole cached history), `to` defaults to today, `from > to` → `422 invalid_date_range`. `granularity=auto` (default) applies docs/12's documented formula (`day\|week\|month\|year` accepted explicitly, coarsened only past the 400-point roofline, both values echoed back). Points are valued at their **bucket END** (M118's convention): a week bucket carries the last close inside that week, and a bucket end after `to` is clamped to `to` so a close is never dated in the future. `from` echoes the window actually SERVED. Reads `prices` rows only — it never fetches, so it works under privacy mode and has no backfill side effect (unlike `/securities/{id}/overview`, which refreshes on every call). `Cache-Control: private, max-age=60`. |
| `GET /securities/stream/prices?security_ids=&token=` | SSE endpoint: streams each requested security's newest cached close on or before today every 5 seconds. Auth via query parameter (JWT token, required for EventSource compatibility). nginx must forward without buffering (`X-Accel-Buffering: no`). -> `text/event-stream` with JSON arrays of `{security_id, symbol, price, date, source, age_days, stale}`; `price` is a Decimal-as-string and the remaining fields describe the cached quote's freshness. |

### Breakdown drill (M169)

`GET /api/v1/portfolio/breakdown?dimension=…&drill=<bucket key>` — the SAME
endpoint, one extra optional parameter. `drill` is the exact `key` of an item
in this response (`Canada`, `Unclassified`, anything the aggregation produced),
URL-encoded. Bucket keys are unique **within** a dimension (they are the
aggregation dict's own keys), so `(dimension, drill)` is the address — no
bucket id to mint, no name that collides across tabs. A key that is not a
bucket in this dimension → **404 `{detail, code: "drill_bucket_not_found"}`**,
never an empty drill: "nothing holds this" is a different, and false, claim.

A caller that never passes `drill` receives a payload **byte-identical** to the
pre-M169 one — the `drill` key is absent, not `null`.

When present, the response gains:

```
drill: {
  key: string,                       # the bucket drilled
  dimension: "sector"|"geography"|"asset_class"|"company",
  bucket_market_value_cad: string,   # == items[].market_value_cad for `key`
  bucket_weight_pct: number,         # == items[].weight_pct for `key`
  contributions_cad: string,         # THE total — the server's number
  contributions: [ … ],              # top-level positions, value desc
  other: {count, market_value_cad, weight_pct} | null,   # folded tail
  depth_cap: 3                       # docs/16's MAX_DEPTH — the walk's own stop
}
```

Each **contribution** is a top-level position that fed the bucket:

```
{
  position_kind: "direct"|"fund",
  security_id, symbol, name,
  account_ids: [int], account_count: int,   # where the money sits
  native_weight_share: null,                # a position has no parent to weigh in
  market_value_cad, weight_pct,             # weight_pct is a share of the BUCKET
  provenance_source: null,                  # a position has no composition row
  estimated_cad, estimated,                 # its M166 slice, if any
  unattributed_cad, unattributed,           # its unplaced residual, if any
  via: [], depth: 0,
  holdings: [ … ],                          # a `fund`'s leaves; [] for `direct`
  holdings_other: {count, market_value_cad, weight_pct} | null
}
```

`direct` — you hold the security itself and it named a bucket, so the
contribution IS its own leaf and `holdings` is `[]` (nesting it under itself
would print the same dollar twice). `fund` — a position the M19 walk looked
through; `holdings` are the leaves it contributed **at the attribution depth
the walk actually used**.

Each **holding** is one composition row the walk placed:

```
{
  position_kind: "direct"|"sub_fund",
  security_id, symbol, name,
  account_ids, account_count,          # inherited from the fund's position
  native_weight_share: string,         # the PROVIDER's published weight in its
                                       # own parent, before renormalization
  market_value_cad, weight_pct,        # weight_pct is ALSO a share of the bucket
  provenance_source: "vanguard"|"issuer"|"yfinance"|"manual",
  estimated: bool, unattributed: bool,
  via: ["VEQT","VXUS"],                # the funds the money passed through
  depth: 2                             # via.length, capped at depth_cap
}
```

`position_kind: "sub_fund"` on a holding means the row RESOLVED to a fund the
walk could not descend into — the depth cap, a cycle back-edge, or a fund with
no rows. The money stopped there; the payload says so rather than implying the
ticker is a company.

**The via honesty rule (pinned).** `via` is the chain of fund SYMBOLS the money
passed through, outermost first, and it is always the walk's own path — never
reconstructed, never truncated to flatter the reader. A Microsoft leaf reached
as VEQT → VXUS reports `via: ["VEQT", "VXUS"]`, so the UI can say "your $x
reaches Microsoft of the USA via VEQT→VXUS" instead of implying you hold
Microsoft directly. Depth is capped at `depth_cap` (= `MAX_DEPTH` = 3) by the
WALK, not by the payload: a deeper chain stays hidden server-side and the
existing "3 levels max" copy (docs/16) discloses it.

**Slices with no row behind them.** M166's fund-mix estimate and the M19
denominator residual are real dollars in a real bucket that no holding
accounts for. They become one holding row naming the FUND that produced them —
`estimated: true` for the mix split, `unattributed: true` for the residual —
with `provenance_source: null` and `native_weight_share: null`. Merging them
into a total instead would make the drill fail to reconcile to the bucket.

**Reconciliation (M91's pin).** The drill is built from the SAME trails that
produced the bucket, so it reconciles by construction:
`Σ contributions + other == bucket_market_value_cad`, and for every fund
`Σ holdings + holdings_other == that contribution's market_value_cad`. Each
row rounds to cents INDEPENDENTLY, so the rows can drift a cent or two from
the total — which is why `contributions_cad` is the SERVER's number and the
client renders it instead of re-adding rows.

**Bounds.** Each level (contributions, and every fund's `holdings`) keeps its
top `DRILL_N` = 25 by value and folds the rest into one `other` block carrying
its exact cent-sum and its count — M167's truncation rule, on the wire, so a
drill never infinite-scrolls.

**Masks (M139).** The drill is a client-side surface: `formatCad` masks the
values like every other money surface, percentages stay visible, names stay
visible. No server-side mask state exists to inherit.

**No new provider calls.** The drill aggregates the cached `etf_holdings` /
`fundamentals` / `positions` rows the walk already reads (docs/07's cache-only
convention for derived views) — it is not a new data source and adds no
network seam.

## Budgets (M25/M37)

| Method/Path | Shape |
| --- | --- |
| `GET /budgets` | List budgets with computed `spent`, `carried_in`, `remaining`, `pct_used` for current period. Response: `[{id, user_id, category_id, amount, period, carry, category_name, spent, carried_in, remaining, pct_used}]` (M110 rollover — `carry` ∈ `off \| forward`, `carried_in` Decimal-string or `null`). `spent` is **signed reporting-currency net spend** clamped at 0: outflows (`cad_amount < 0`) add, refunds/returns (`cad_amount > 0`) offset, and inflow rows never count as spend. Foreign rows convert at the stored `transactions.cad_amount` mirror, else the newest cached `fx_rates` rate on or before the booking date; an unresolvable rate is EXCLUDED, never counted at native face value (docs/12 population rules). Period membership uses booking `date` over the shared **LEDGER** predicate (`txn_filters.countable_tx_conditions` = committed-only + split parents excluded, so a split's parts land under their OWN categories while the parent contributes nothing — docs/44 §2 — plus `today_et()`, docs/12). **M137 P0-2 (docs/33 §Server-first computing)**: it is the only read path for budget *definitions* (there is no `GET /budgets/{id}`) AND the only math — the UI now renders `spent`/`carried_in`/`remaining`/`pct_used` as returned instead of discarding them and recomputing from a full-ledger download. The Budgets page is therefore ONE small `GET /budgets` and no `/transactions?all` at all. `frontend/src/lib/budgets.ts::budgetsProgress` survives — moved out of the retired `s3Engine.ts` by M160 (P2-5) as an AUDITED CLIENT-ONLY compute (docs/33 rule-8 Row-8 class). It is NOT this route's fallback: it backs the PARTNER view (`api/sharing.ts::loadSharedView`), which has no server twin by D3 design (docs/43 §9). Still pinned by the golden `budgets.progress` in `frontend/tests/fixtures/s3-vectors.json` (A13) and replayed against this route by `backend/tests/test_m160_replay_s3.py`. The Dashboard spending alert derives its percentage from the SAME helper (`services/budgets.py::budget_spent`), so both surfaces print one number. **Rounding:** `spent` and `carried_in` are quantized `ROUND_HALF_EVEN` (stated explicitly as `cents(value, rounding=ROUND_HALF_EVEN)` — the one canonical helper, docs/02 Conventions), which is NOT the app’s default money mode. It is preserved because `frontend/src/lib/s3Engine.ts` mirrors exactly this mode and `frontend/tests/fixtures/s3-vectors.json` is a golden captured from `spent_between` over the live stack (docs/33 §S3 parity); moving budgets to half-up requires `s3Engine.dquantize` and that golden to change in the same commit. Response order is `ORDER BY Category.name` under the database's `en_US.utf8` collation (case/accent-insensitive at the primary level); the client port compares UTF-16 code units — the same convention `lib/reportEngine.ts` uses — which agrees for ASCII names of one case (the captured goldens are all lowercase) and can differ only when the deciding character is separated by case or an accent (`Zulu` before `alpha` in code units, after it in `en_US.utf8`). Ordering is display-only, never part of the numeric contract. |

| `POST /budgets` | `{category_id, amount, period, carry?}` → 201 (`carry` defaults `off`; docs/09 §Budget carry). 409 `duplicate_budget` if category already budgeted. |
| `PATCH /budgets/{id}` | Partial update: `{amount?, period?, carry?}` → updated budget. Toggling `carry` never rewrites an already-frozen snapshot (docs/09). |
| `DELETE /budgets/{id}` | 204. Its `budget_carryovers` snapshots and `spending_alerts` rows go with it (child-before-parent, docs/02). |

### Budget rollover (M110 — docs/09 §Budget carry)

Every figure keeps its own field — `carried_in` is NEVER merged into `amount`:

- `spendable` (the pct denominator) = `amount + carried_in`, and with no carry
  `carried_in` is `null` so the denominator is `amount` exactly as before (zero
  behaviour changes for a row that never opted in);
- `remaining` = `spendable − spent` (the money left to spend);
- `pct_used` = `float(spent / spendable * 100)` — the same TRUE ratio, **never
  capped at 100** (M61 #3 / M65), so a 250% overspend still reads `250` and only
  the progress bar clamps its own width (`Math.min(pct, 100)`) — **except**
  when `spendable <= 0`, where no positive denominator exists and the field is
  JSON `null` (docs/09: say "over budget", never invent a percent);
- `carried_in` is the FROZEN snapshot written at the boundary switch
  (`budget_carryovers`, UNIQUE `(budget_id, period_start)`), materialized by
  the first read of a period and never recomputed — a later back-view returns
  the same number even if the source period is edited afterwards;
- it materializes only when `carry = forward` AND the budget row existed before
  the period started (it governed the whole source period — no budget in a
  month, nothing carries); one period deep only: `carried_in(P) = amount −
  spent(P−1)`, so a carried value never feeds its own successor (no compounding
  chain, docs/09). The materialization is the endpoint's only write, the same
  auto-create-on-first-GET pattern `GET /notifications` uses (docs/35);
- the Dashboard/Budgets alert measures the SAME denominator
  (`services/budgets.py::progress`), so both surfaces print one number, and
  `spending_alerts.budget_amount` stores the pool actually used (identical to
  `amount` while carry is off).


## Goals (M33/M36)

| Method/Path | Shape |
| --- | --- |
| `GET /goals` | List goals with computed progress. Response: `[{id, name, target_amount, target_date, account_id, current_amount_override, current_amount, progress_pct, monthly_needed, status}]` where `status` = `on_track | behind | ahead | completed`. An account-linked goal's `current_amount` = `accounts.starting_balance or 0` + Σ committed `amount_native` for that account, matching `api/accounts.py` and the net-worth snapshot; `current_amount_override` still wins when set. |
| `POST /goals` | `{name, target_amount, target_date?, account_id?, current_amount_override?}` → 201. |
| `PATCH /goals/{id}` | Partial update → updated goal. |
| `DELETE /goals/{id}` | 204. |

## Bill Reminders (M40) + match rules & drill-down (M116)

| Method/Path | Shape |
| --- | --- |
| `GET /bills?month=` | List enabled bills ordered by due date. Optional `month` filter (YYYY-MM). **M116 B4:** items are `BillStatPublic` = `BillReminderPublic` + `payment_count: int` (lifetime rows the rule matched; `0` is the honest "none yet") + `rolling_average: string \| null` (mean of the last 12 period totals; `null` when nothing matched). **M122 T4:** + `ytd_total: decimal-string` — calendar-year-to-date spend (Jan 1 → server `today_et()`) from the SAME matched set; `"0.00"` is the honest "nothing this year". One scan of the caller's committed expense rows feeds every bill. |
| `POST /bills` | `{name?, amount?, category_id?, frequency?, next_due_date?, remind_days_before?, enabled?, from_subscription_id?}` → 201. `name`/`frequency`/`next_due_date` are required **unless** `from_subscription_id` (M55) is present: the server then derives whatever the request omitted from that detected subscription **of the same user** — 404 `subscription_not_found` (unknown or foreign id), 422 `unsupported_bill_cadence` (`weekly`/`biweekly` detection), 422 `subscription_next_due_missing`. Request values win over the derived draft; contract + check order: docs/25 §M55. |
| `PATCH /bills/{id}` | Partial update → updated bill. |
| `DELETE /bills/{id}` | 204. |
| `POST /bills/{id}/mark-paid` | Advances `next_due_date` by frequency (monthly→+1m, quarterly→+3m, yearly→+1y); `one_time` disables the bill. Returns updated bill. **M180:** the advance is computed from the schedule's own day-of-month `anchor_day` (docs/25 §M180 D1), so a bill due on the 31st goes Jan 31 → Feb 28 → **Mar 31** → Apr 30 and a Feb-29 yearly bill returns to Feb 29 in the next leap year — the stored date is clamped, the SCHEDULE is not. `anchor_day` is internal storage and never appears on the wire. A stored due date the rule cannot use (not `YYYY-MM-DD`, or an advance past year 9999) → **422 `invalid_due_date`** with nothing written. |
| `PUT /bills/{id}/rule` | **M116 D1 + M121 D1/D2** — full replacement of the match rule: `{match_mode: "contains" \| "starts_with" \| "exact", match_terms?: string[] (1–10 items, each 1–120 chars), match_logic?: "any" \| "all" (default `"any"`), match_term?: string (1–120 — the pre-M121 one-term field), amount_tolerance: decimal \| null (>= 0)}` (`extra=forbid`) → 200 `BillReminderPublic`. **At least one term source is required**: neither `match_terms` nor `match_term` → 422 `validation_error`; both sent → `match_term` must equal `match_terms[0]` (a client that disagrees with itself is refused, never resolved silently). Every term must still name a payee after `normalize_payee_key` runs — blank / digits-only / over 120 chars / an 11th term → 422 `validation_error`, as does an unknown `match_logic`; `amount_tolerance` while the bill has no `amount` → 422 `validation_error`. Nothing is written about transactions: the matched set IS the rule re-applied on every read (docs/25 D3). |
| `GET /bills/{id}/detail?from=&to=&limit=&offset=` | **M116 B2** — `200 {bill: BillStatPublic, matched_count, transactions: TransactionPage, rolling, chart}`. `limit` 1–200 default 50, `offset` ≤ 1,000,000 (the shared pager, 422 `validation_error` outside). `from`/`to` (ISO dates) bound the WHOLE response — transaction page, chart and rolling all describe the same window; omitted, the window is the whole committed ledger, which is what makes "every matched transaction" true by default. `bill.payment_count` / `bill.rolling_average` stay LIFETIME, so narrowing the picture never changes the list's row. `rolling` = `{frequency, six, twelve}`, each `{periods, window_periods, payments, mean, median}` (mean/median over the last N **calendar** period totals, misses counted as `0.00`; `null` when there are none). `chart` = `{frequency, unit, granularity, first_period, last_period, points: [{period, label, total, count, missed, rolling_six}]}` — `period` is the bucket's FIRST DATE (the `GET /reports` bucket key, so `fmtBucket` labels the axis); a period with no payment is present with `total: "0.00"`, `count: 0`, `missed: true`; **M122 T3** `rolling_six: decimal-string \| null` is the trailing ≤6-period mean ENDING at that point (missed periods as `0.00`, window front-truncated) — the LAST point's value IS `rolling.six.mean`, so the chart line and the card's stat are one number. Each `rolling` window additionally carries **M122** `total: decimal-string \| null` (the window's SUM — `total = mean × window_periods` exactly). **M122 T4** the response gains `stats: {all_time: {total, payments, average: decimal-string \| null, years, yearly_average: decimal-string \| null}, ytd: {year, total, payments}}`: LIFETIME over the matched set, never narrowed by `from`/`to`; missed periods never enter `total`; `years` = distinct calendar years with ≥1 payment (the honest divisor for `yearly_average = total / years`, quantized to cents); `ytd` is the CALENDAR year Jan 1 → server `today_et()` (future-dated rows count for all-time, never YTD). Every figure recomputes from the rule per read (M91 — no cached snapshot). Unknown/foreign id → 404 `not_found`. |
| `POST /bills/{id}/find-past` | **M116 B1 + M121 + M123** — read-only preview: body `{match_mode?, match_term?, match_terms?, match_logic?, amount_tolerance?, category_id?}` (`extra=forbid`, all optional; omitted = use the saved rule — including `bill.category_id` — an explicit `null` clears the tolerance / the category scope; an EXPLICITLY blank `match_term` is still 422 `validation_error`, as M116 pinned) → `200 {match_mode, match_term, match_terms, match_logic, frequency, total_count, truncated, groups: [{period, label, count, total, items: [{id, date, amount_native, currency, cad_amount, normalized_payee, account_id, account_name}]}]}`, newest period first. Groups report their TRUE `count`/`total` even when `items` is cut at the 500-row preview cap. **M122 T2:** + `stats: {first_date: date \| null, last_date: date \| null, last_12_total: decimal-string \| null, account_counts: [{account_id, account_name, count}]}` computed over the WHOLE matched set (never the truncated slice): `last_12_total` is the sum of the last ≤12 calendar period totals with misses as `0.00` (the same population `rolling.twelve.mean` divides), `account_counts` ordered by count desc then name. **Writes nothing** — no linkage row exists to write. |
| `POST /bills/find-past` | **M116 addition (see docs/25)** — the same preview for a rule that has no bill row yet (the create form): `{match_mode?, match_terms?, match_term?, match_logic?, amount_tolerance?, category_id?, amount?, frequency?}` → the same shape. **M121:** at least one term source is required here too (there is no row to fall back to) — 422 `validation_error` when neither is sent. Shares the handler with the pinned `{id}` route, including the **M122 T2** `stats` block — the create form's pre-save Test answers the same summary. |

`POST /bills` (M116) additionally accepts `match_mode` (default `contains`), `match_term` (default: the bill's `name`, resolved AFTER any `from_subscription_id` draft) and `amount_tolerance`; a term that normalizes to nothing is 422 `validation_error`, as is a tolerance with no `amount`. `PATCH /bills/{id}` refuses to null `amount` while a tolerance is set (422 `validation_error`) — the band is cleared through `PUT /bills/{id}/rule`, never stranded.

| `GET /bills/suggest-terms?q=&limit=&bill_id=&match_mode=` | **M122 T1 (docs/25 §M122)** — term suggestions from the CALLER'S OWN committed expense rows, grouped by `normalize_payee_key(normalized_payee)` (the matcher's key, so two spellings are one group; the label is the group's newest spelling — a real string out of the data). → `200 [{normalized_payee, count, total, last_date}]` ranked by frequency desc, recency desc, label; `limit` 1..25 default 8. `q` empty = the top groups overall (the editor's opening candidates); `q` set = only groups the draft would match, tested through the shared `match_terms` matcher under `match_mode` (default `contains`); a `q` that normalizes to nothing (`"123"`) answers `[]`. `bill_id` set → payee groups with ≥1 row inside that bill's CURRENT matched set are excluded (a suggestion that would change nothing is noise); foreign `bill_id` → 404 `not_found`. Unknown `match_mode` / out-of-band `limit` / `q` > 120 chars → 422 `validation_error`. Pure DB query — no LLM, no network, `privacy_mode` irrelevant (docs/11). |

**M121 (docs/25 §M121).** `POST /bills` and `PUT /bills/{id}/rule` also accept **`match_terms: string[]`** (1..10 items, each 1..120 chars after trimming, each naming a payee) and **`match_logic: "any" | "all"`** (default `"any"` — the widening mode, which a single-term rule behaves identically under). The legacy `match_term` stays: send `match_terms` OR `match_term` OR both (both must agree — `match_term == match_terms[0]`, else 422 `validation_error`); `match_terms: []` / neither field on `POST /bills` means "derive from `name`", exactly as a blank `match_term` always did. **Every bill response** (`BillReminderPublic`, `BillStatPublic`, the `bill` inside `BillDetail`) carries `match_terms: string[]` and `match_logic: string`, with `match_term = match_terms[0]` — pre-M121 readers keep working unchanged. Bills' terms live in `bill_reminder_match_terms` (docs/02); deleting a bill cascades its terms away at the database.

**M122 (docs/25 §M122).** The rule-editor surface gains three read-only additions, none of them stored: **`GET /bills/suggest-terms`** (the ledger-backed term suggestions above), the **`stats`** block on both `find-past` routes (the dry-run's summary over the whole matched set — first/last date, last-12-period sum, per-account counts), and the **`stats`** block on `GET /bills/{id}/detail` plus `ytd_total` on `GET /bills` (all-time + calendar-YTD spend over the rule's matched set, recomputed per read). Chart points carry `rolling_six` and the rolling windows carry `total` — both derived at read time. No new table, no migration, no write path changed.

## Debts (M44)

| Method/Path | Shape |
| --- | --- |
| `GET /debts` | List debts ordered by name. |
| `POST /debts` | `{account_id, name, debt_type, original_amount?, interest_rate?, minimum_payment?, payment_frequency?, start_date?, end_date?, notes?}` → 201. `original_amount` and `minimum_payment` are `>= 0`, `interest_rate` 0–100, `payment_frequency` is exactly `monthly \| biweekly`; anything else is 422 (validation beats silent tolerance for financial data). |
| `PATCH /debts/{id}` | Partial update → updated debt. Mirrors the create bounds (docs/29 F7). |
| `DELETE /debts/{id}` | 204. |
| `GET /debts/projection` | Payoff projections for all debts. Response: `{debts: [{debt_id, name, debt_type, balance, interest_rate, minimum_payment, months, total_interest, total_paid, payment_frequency, periods_per_year}], total_debt, total_interest}`. **No top-level `total_paid`** — only the per-debt field. `payment_frequency` (`monthly \| biweekly`) drives `periods_per_year` (12 \| 26): interest accrues once per payment period at `rate/100/periods_per_year`. `months` is whole calendar months, rounded up from the payment count. `total_paid` is always `balance + total_interest` — including the 0% branch, where the final payment is partial (`months = ceil(balance/payment)`). `months=-1`, `total_interest="infinite"`, `total_paid="infinite"` when the payment cannot cover the interest or the balance outlives the 50-year amortization horizon. A debt with no `minimum_payment` (or no `interest_rate`) has no projection: all three fields are `null`. |

## Spending Alerts (M41)

| Method/Path | Shape |
| --- | --- |
| `GET /budgets/recurring-summary` | **NEW (M137 P0-2, docs/09 §Recurring prompt).** The Budgets page's M96 "committed monthly" label, one indexed scan instead of a full-ledger download. → 200 `{count: int, monthly: decimal-string}`: rows in the caller's committed LEDGER scope carrying `is_recurring=true` + a cadence, weighted by `weekly → 30.4375/7`, `biweekly → 30.4375/14`, `four_weekly → 30.4375/28`, `monthly → 1`, at the reporting-currency amount (`amount_native` when the row's currency IS the reporting currency, else the `cad_amount` mirror; a foreign row with no mirror is EXCLUDED, never counted at face value — docs/12). Signed (debits negative), rounded to the cent, `{"count": 0, "monthly": "0.00"}` on an empty tenant. Read-only. **`monthly` is a `Decimal`, so it serializes as a decimal STRING** — the wire shape every other money field in this document already has. It was a JSON number while the service accumulated in `float` (`float(value) * _PER_MONTH[...]`), which AGENTS.md §2 forbids for a money magnitude; the cadence table is now `Decimal` and the single `quantize` at the end is the only rounding. `count` remains a JSON number. |
| `GET /budgets/alerts` | Auto-checks thresholds (80% warning, 100% critical), returns unacknowledged alerts. Response: `{alerts: [{id, budget_id, alert_type, threshold_pct, actual_pct, spent, budget_amount, created_at}], count}`. |
| `POST /budgets/alerts/{id}/acknowledge` | Dismisses an alert. → `{status: "acknowledged"}`. |

## Tags (M12)

| Method/Path | Shape |
| --- | --- |
| `GET /tags?limit=&offset=` | Paginated tag list → `{items, total, limit, offset}`. Items are **`TagWithUsage`** (M104) = the three `TagPublic` fields + `transaction_count` + `rule_count` (junction links in `transaction_tags` / `rule_tags`, one grouped query per table — docs/34), so the docs/09 §Tag UX management surface can show "used by N" INSTEAD of attempting a delete the 409 `tag_in_use` guard would refuse (that guard is unchanged and remains the backstop). Objects embedded on `TransactionPublic.tags` stay plain `TagPublic`. |
| `POST /tags` | `{name}` → 201. 409 `duplicate_tag`. |
| `GET /tags/{id}` | Single tag. |
| `PATCH /tags/{id}` | `{name}` → updated tag. 409 `duplicate_tag`. |
| `DELETE /tags/{id}` | 204. 409 `tag_in_use` if referenced by any transaction or rule. |
| `POST /tags/merge` | **M104** — fold duplicates together, the one thing a rename cannot do (renaming "Goceries" to an existing "Groceries" answers 409 `duplicate_tag`). `{source_ids: [int] (min_length=1), target_id: int}` (`extra=forbid`) → **200** `{target: TagPublic, merged_tag_ids: [int]}` (the survivor + every id folded into it, now gone from `tags`). Each source's `transaction_tags` / `rule_tags` rows are re-pointed at `target_id` (`ON CONFLICT DO NOTHING`, so a transaction carrying both sides keeps exactly one link) and the source tags are deleted. Every id ownership-checked (404 `not_found`) BEFORE the first write; `target_id` ∈ `source_ids`, an empty `source_ids` or an extra body field → **422 `validation_error`**; no bearer → 401. |
| `GET /transactions/{transaction_id}/tags` | Tags attached to one transaction (junction `transaction_tags`, docs/02) → 200 `{items: [{id, user_id, name, created_at, updated_at}]}`, ordered by `name` ASC, unpaged. **Wider than `TagPublic`**: these handlers return the ORM row, so both `TimestampMixin` stamps ride along, whereas the `/tags` routes above return `{id, user_id, name}` only — and `TransactionPublic.tags` (the field embedded on every `GET /transactions` item) is `TagPublic` too. Unknown or foreign `transaction_id` → `404 {"detail": "Transaction not found", "code": "not_found"}`. |
| `POST /transactions/{transaction_id}/tags` | `{tag_id: int}` (`extra=forbid`) → **200** `{items}` = the FULL updated set, not just the added tag. Idempotent and race-safe: the junction insert is `ON CONFLICT DO NOTHING` on `(transaction_id, tag_id)`, so re-adding an already-attached tag answers 200 and changes nothing. Unknown/foreign transaction **or** tag → `404 not_found`; missing / wrong-typed / extra body field → `422 validation_error`; demo tenant → `403 demo_account_restricted`. |
| `PUT /transactions/{transaction_id}/tags` | `{tag_ids: [int]}` (`extra=forbid`) → **200** `{items}` reflecting the state after the write. REPLACES the whole set: payload ids are deduped, every id is ownership-checked (`404 not_found` raised BEFORE any write), then only missing rows are inserted (`ON CONFLICT DO NOTHING`) and rows no longer wanted are deleted — diff-based, so concurrent setters cannot 500 on `uq_transaction_tags_txn_tag`. `tag_ids: []` clears all tags. Same `422 validation_error` / `403 demo_account_restricted` errors as POST. |
| `DELETE /transactions/{transaction_id}/tags/{tag_id}` | Detaches one tag → **200 with body `null`** (deliberately NOT 204: the handler returns `None` and FastAPI serializes it; the frontend types it `void`). Idempotent — an owned tag that is not attached still answers 200. Unknown/foreign transaction or tag → `404 not_found`; demo tenant → `403 demo_account_restricted`. |

**Transaction-tag verification (AGENTS §9).** `backend/tests/test_tags.py` is the executable version of the table above: attach → read → detach (`{items: []}` after), payload dedupe (`tag_ids: [a, a, b]` → items `{a, b}`), full replace (`[b]` → items `{b}`), idempotent re-`POST` of an attached tag → 200 unchanged, plus the **M104** half — usage counts on `GET /tags` (a tag on one transaction and one rule answers `(transaction_count, rule_count) == (1, 1)`, an unused one `(0, 0)`), `POST /tags/merge` (junction rows move, a transaction carrying both sides keeps exactly one, `rule_tags` re-point, the sources 404 afterwards, self/empty → 422 `validation_error`, foreign source → 404) and `?tag_ids=` AND (two ids → only the row carrying both, one id ≡ the single-tag filter, an id nobody has intersects to nothing, foreign id → 404). Live against the prod stack (2026-09-28, throwaway tenant created and deleted in the same run): `PUT [T2]` → 200 with `probe-beta`, `POST T1` → 200 with `[probe-alpha, probe-beta]` (name order, even though beta was attached first), `PUT [T1,T1,T2]` → 200 with two items, `PUT []` → 200 `{items:[]}`, `DELETE T1` → 200 `null` and a repeat DELETE → 200 `null`, unknown tx/tag → 404 `not_found`, `{"tag_id": 1, "bogus": true}` → 422 `validation_error` (`extra_forbidden`), `{"tag_ids": "nope"}` → 422 `validation_error` (`list_type`), no bearer → 401 `invalid_token`, demo POST/PUT/DELETE → 403 `demo_account_restricted` while demo reads stayed open.

## Notifications (M31) + in-app inbox (M51) + delivery channels (M59)

| Method/Path | Shape |
| --- | --- |
| `GET /notifications` | List notification settings. Auto-creates any missing row on first call: **legacy five, default disabled** — `large_transaction`, `low_balance`, `dividend_received`, `price_alert`, `monthly_summary` (**no generator exists for any of them** — nothing produces these events and nothing but this endpoint reads `enabled`, docs/35); **generator-backed three, default enabled** — `budget_threshold` (`spending_alerts`), `bill_due` (`bill_reminders`), `tax_loss` (computed). Every row carries `delivery_enabled` (M59), seeded as a **copy of `enabled`** — a second, independent switch that additionally requires an enabled channel before anything is sent (docs/37). |
| `PATCH /notifications/{id}` | `{enabled?, delivery_enabled?, config?}` → updated setting. 404 `not_found` for a foreign/unknown id. Setting `enabled: false` **or** `delivery_enabled: false` cancels that type's pending deliveries in the same request (docs/37). |
| `GET /notifications/inbox?limit=&cursor=` | Union of the three gated sources, tenancy-scoped, cursor-paginated (docs/35). `limit` 1–200 default 50 (422 `validation_error` outside). → `{items: [{id, kind, severity, title, body, link, amount_cad, occurred_at, read, read_at}], total, unread_count, limit, cursor, next_cursor}`. `kind` ∈ `budget_threshold | bill_due | tax_loss`; `severity` ∈ `critical | warning | info`; `amount_cad` Decimal-string or `null`; `id` = `budget_threshold:<spending_alerts.id>` / `bill_due:<bill_reminders.id>` / `tax_loss:<security_id>:<account_id>`. `total` + `unread_count` cover the WHOLE gated inbox (not the page) so `?limit=1` drives the badge. Sort `(occurred_at DESC, id DESC)`; cursor = base64url `{"v":1,"at","k"}` of the last item, `next_cursor: null` on the last page. **Toggle OFF excludes that kind from the response while generation keeps writing `spending_alerts` rows — the surface is gated, not the data.** Errors: `401 invalid_token`, `422 validation_error`, `422 invalid_cursor`. |
| `POST /notifications/inbox/{id}/read` | Mark one item read → `{id, read: true, read_at}`. Idempotent (repeat returns the original `read_at`). Resolution is tenancy-only: unknown / malformed / **foreign** id → `404 not_found`; an item whose kind is toggled OFF still records read state. Errors: `401 invalid_token`, `404 not_found`, `403 demo_account_restricted`. |
| `POST /notifications/inbox/read-all` | Marks read every item in the currently visible (gated) inbox → `{marked, unread_count: 0}`; hidden kinds keep their existing state. Errors: `401 invalid_token`, `403 demo_account_restricted`. |
| `GET /notifications/channels` | The caller's two transports, auto-seeded on first call (idempotent). Bare list (2 rows, so no pagination envelope — same exception as `GET /notifications`): `[{id, user_id, channel, enabled, target, privacy_opt_in, configured, created_at, updated_at}]`. `channel` ∈ `email \| webhook`; both rows seed `enabled: false`, `email.target = users.email`, `webhook.target = null`; `privacy_opt_in` = `privacy_opt_in_at IS NOT NULL`; `configured` = enabling would be accepted right now (email: `NOTIFY_SMTP_HOST` + `NOTIFY_SMTP_FROM` + `target` set; webhook: `target` set). Errors: `401 invalid_token`. |
| `PATCH /notifications/channels/{channel_id}` | `{enabled?, target?, privacy_opt_in?}` (`extra: forbid`) → the updated channel. Validations run against the projected state BEFORE anything is written, so a 4xx leaves the row untouched. `target`: `email` must be a valid address (`422 validation_error`), `webhook` must pass the SSRF policy (`422 invalid_webhook_url` / `403 webhook_target_forbidden`). `enabled: true` requires a configured transport (`409 channel_not_configured`) and, **when the request itself sets `enabled: true`** while `privacy_mode=true`, the explicit opt-in (`409 privacy_opt_in_required`) — the opt-in is sticky once set and `privacy_opt_in: false` may always be sent (it is the off switch). `enabled: false` cancels that channel's pending deliveries. Errors: `401 invalid_token`, `403 demo_account_restricted`, `403 webhook_target_forbidden`, `404 not_found`, `409 channel_not_configured`, `409 privacy_opt_in_required`, `422 validation_error`, `422 invalid_webhook_url`. |
| `GET /notifications/deliveries?limit=&offset=&status=` | Auditable outbox, house envelope `{items, total, limit, offset}` (`limit` 1–200 default 50, `offset` 0–1,000,000, optional `status` ∈ `pending \| sent \| dead \| cancelled`, all else `422 validation_error`). Item: `{id, channel, alert_type, item_key, status, attempts, next_attempt_at, last_error, last_error_code, sent_at, dead_at, retracted, created_at, updated_at, attempt_log: [{attempt, outcome, error_code, http_status, detail, created_at}]}`. `outcome` ∈ `sent \| retryable \| fatal`; `attempt_log` is empty until the worker dispatches. Errors: `401 invalid_token`, `422 validation_error`. |
| `POST /notifications/deliveries/run` | Runs scan + dispatch for the **caller only** (the code path the worker ticks on) → `{scanned_users, enqueued, retracted, attempted, sent, retrying, dead, cancelled}`. Errors: `401 invalid_token`, `403 demo_account_restricted`. |
| `POST /notifications/deliveries/{id}/retry` | Re-arm a `dead` delivery → the delivery with `status: pending`, `attempts: 0`, `dead_at: null`, `attempt_log` **preserved**. Unknown/foreign id → `404 not_found`; a row that is not `dead` → `409 delivery_not_retryable`. Errors: `401 invalid_token`, `403 demo_account_restricted`, `404 not_found`, `409 delivery_not_retryable`. |

Demo tenant: `GET` on all of the above stays open; every `POST`/`PATCH` is `403 demo_account_restricted` (global guard in `app/api/deps.py`, no exemption added).

Outbound payloads, the privacy rule and the retry numbers are pinned in docs/37.

## Reconciliation (M32/M39)

| Method/Path | Shape |
| --- | --- |
| `GET /accounts/{id}/reconciliation` | Statement items + summary. Response: `{items: [{id, statement_date, statement_amount, statement_description, status, transaction_id?}], summary: {total, matched, unmatched, matched_sum, unmatched_sum}}`. |
| `POST /accounts/{id}/reconciliation/upload` | `{items: [{date, amount, description?}]}` → replaces all existing unmatched items, adds new ones. 201. |
| `POST /accounts/{id}/reconciliation/match` | Query params: `item_id`, `transaction_id`. Sets `transaction_id` and `status=matched`. Returns updated item. **M107 (docs/44 §5):** the link target may be a split **parent** (a statement line matches a statement row — the write lands in `reconciliation_items`, never in the transaction, so splitting changes nothing) but never a **child** → **409 `split_leg_immutable`** ("This row is part of a split. Match the statement line instead."), checked inline before the link is written. There is no server-side candidate list on this router: `transaction_id` is supplied by the caller. |

## Tax Projection (M42)

| Method/Path | Shape |
| --- | --- |
| `GET /taxes/projection?year=` | Returns saved projection inputs + computed result: `{year, province, taxable_income, federal_tax, provincial_tax, total_tax, average_rate, bracket_year, brackets_fallback, brackets_fallback_year, stored, ...}`. **There is no `marginal_rate` field.** `year` defaults to 2026. Bracket tables are year-keyed (2025, 2026); a year with no table falls back to 2026 and says so — `brackets_fallback: true` with `brackets_fallback_year: 2026` and `bracket_year: null`, so a stale table is never silent (docs/27). |
| `PATCH /taxes/projection?year=&province=&estimated_income=&deductions=&capital_gains_override=&dividend_income_override=` | Upserts projection inputs, recomputes and returns result. |
| `POST /taxes/projection/simulate` | **Does not exist.** What-if scenarios are computed client-side or via `GET /taxes/projection` with different stored inputs; there is no such route and no caller. |

## Tax Alerts (M26)

| Method/Path | Shape |
| --- | --- |
| `GET /taxes/alerts` | Unrealized losses in taxable accounts. Response: `{alerts: [{security_id, symbol, account_name, unrealized_loss_cad, book_cost_cad, market_value_cad, estimated_tax_benefit_cad, superficial_loss_risk}], summary: {count, total_unrealized_loss_cad, total_estimated_tax_benefit_cad}}`. Threshold: loss > $100 CAD. |

## Reports — additional endpoints

| Method/Path | Shape |
| --- | --- |
| `GET /reports/insights` | Spending velocity. Response: `{current_month_spending, previous_month_spending, month_over_month_pct, daily_burn_rate, projected_month_end, days_elapsed, days_in_month, category_deltas: [{category_id, category_name, current, previous, change_pct}]}`. Spending is **signed reporting-currency net outflow** clamped at 0 (refunds offset purchases; inflow rows are not spending). **M138 (D-1):** the net is taken **per category**, and a category whose rows are all inflows (payroll, interest, a transfer credit) is income — it never enters the headline nor `category_deltas`. The pre-M138 build netted every row in the window and floored at zero, so any month where income exceeded spending printed `$0` while the same response's breakdown carried the real number; `$0` now means *nothing went out*, never *you earned more than you spent*, converted from `cad_amount` with the same `fx_rates` cache-only rules and unresolved-FX exclusion as `GET /budgets` (docs/12). **Owner-lens expansion — `scope` ∈ `own` (default) \| `household`** (docs/12 §Owner dimension): the whole response widens to the household union — the caller's own committed rows plus the owner's allowlisted rows through the caller's own active `sharing_links` row (docs/43 §6; deny-by-default, one-directional, a privacy-mode owner projects NOTHING). The math is untouched — same signed net, same per-category floor, same windows — and `category_deltas[].category_name` resolves from BOTH tenants' trees, because a projected row carries the OWNER's category ids (docs/43 §6); without that a household chip would label a foreign id `Unknown`. `days_elapsed`, `days_in_month` and the month boundaries are calendar facts, identical under either value. Anything but `own`/`household` → **422** at `query.scope`. No lens control reads this route (`src/api/insights.ts` sends no `scope`), so today's screen is exactly the pre-expansion answer. |
| `GET /reports/spending-comparison?current_start=&current_end=&previous_start=&previous_end=` | Period-over-period. `previous_*` auto-computed (same-length window before current) if omitted. **RECONCILED (review):** the response was documented here as `{current_total, previous_total, deltas: [{category_id, name, current, previous, change_amount, change_pct}]}` but the service, the router and the only frontend caller (`src/api/spendingComparison.ts`) have always spoken a different shape. The shape below is the one actually served and consumed. → 200 `{current_period: {start, end, total}, previous_period: {start, end, total}, total_change: decimal-string, total_change_pct: number, deltas: [{category_id: int \| null, category_name: str, current, previous, change: decimal-string, change_pct: number}]}`. **`total`s are signed SPENDING in the reporting currency, not raw sums** — the service now reads `insights._category_flows`, the single reader for signed per-category spending (docs/12 §Population rules), instead of its own `sum(abs(amount_native))`. That removes two defects it alone had: `abs()` counted a refund as extra spending and counted inflow rows as spend, and `amount_native` is the row's OWN currency so a USD row was added to a CAD row unconverted. Now a refund offsets the purchase it belongs with, a foreign row is priced at its CAD mirror or (failing that) the newest cached rate, and an income-only category contributes nothing. Note the two paths differ honestly: a row WITH its `cad_amount` mirror offsets in the signed sum, while an un-mirrored refund prices to zero (`_spend_cad` treats an inflow as not spending) — both paths are pinned in `backend/tests/test_spending_comparison.py`. **Owner-lens expansion — `scope` ∈ `own` (default) \| `household`** (docs/12 §Owner dimension): BOTH windows and the `deltas` namespace widen to the household union — the caller's own committed rows plus the owner's allowlisted rows through the caller's own active `sharing_links` row (docs/43 §6, deny-by-default, one-directional, privacy-mode owner projects nothing). `previous_*` auto-computation, the signed/FX math, the ordering and every field name are byte-identical under either value: only WHO is summed changes. Anything but `own`/`household` → **422** at `query.scope`. No client sends `scope` (`src/api/spendingComparison.ts` does not set it), so today's screen is exactly the pre-expansion answer. |
| `GET /reports/dividend-calendar` | Upcoming dividend projections (docs/24 §M29). **Implemented runtime contract; independent QA pending:** `{upcoming: [{security_id: integer, symbol: string, frequency: "monthly"\|"quarterly"\|"yearly"\|"unknown", currency: code-string\|null, avg_amount: decimal-string\|null, annual_projection: decimal-string\|null, last_payment_date: date-string, next_expected_date: date-string, num_payments: integer}], provenance: {status: \"ok\"\|\"stale\"\|\"missing\", as_of: date-string\|null, sources: [{key, kind: \"ledger\"\|\"market_data\", label, provider: string\|null, as_of: date-string\|null, age_days: integer\|null, status: \"ok\"\|\"stale\"\|\"missing\", detail}]}, summary: {this_month: null, this_quarter: null, this_year: null, by_currency: [{currency: code-string, this_month: decimal-string, this_quarter: decimal-string, this_year: decimal-string}], unresolved_counts: {this_month: integer, this_quarter: integer, this_year: integer}}}`. Per-entry denomination is the ONE valid code agreed by all contributing rows (`trade.currency or account.currency`, never listing currency); mixed/unknown rows withhold all entry money. `by_currency` is code-sorted, zero-filled per period, excludes withheld projections counted by `unresolved_counts`, and never blends currencies. Empty history → `upcoming: []`, `by_currency: []`, zero counts. Legacy scalar keys are ALWAYS null to prevent CAD-only clients misstating native money. Decimal money strings have two fractional digits; no FX reads or conversion. Existing next-payment eligibility/date math stays unchanged (these are next-payment estimates, not annualized income). **Pre-change at `7726644`:** per-entry currency is absent; non-null native money is exposed with numeric scalar summaries that mix denominations. `next_expected` was a documentation error: the actual field is `next_expected_date`. **CSV twin (row-currency export): `?format=csv`** → 200 `text/csv` (`attachment`, `dividend-calendar-<today_et()>.csv`) whose header row is pinned to the JSON field names in wire order `security_id,symbol,frequency,currency,avg_amount,annual_projection,last_payment_date,next_expected_date,num_payments`: the SAME `upcoming` rows the JSON answers with (one `compute_dividend_calendar` computation, never a second reader), money as two-decimal strings, `currency` travelling with its money cells — all three empty together on a withheld projection — and NO total or summary row, so no file can print a figure across denominations. `format` is `json\|csv`; anything else → 422 `validation_error` at `query.format`. No FX read. Reconciliation pinned by `backend/tests/test_dividend_calendar_export_reconciliation.py`. See docs/24 for exact UI, rounding and verification requirements. |
| `GET /reports/cashflow-calendar?month=` | **Cash-flow calendar (M52, docs/36).** `month` is `YYYY-MM`, optional (absent = the server's current `today_et()` month), 1900-01 … 2200-12. Response `{month, today, confirmed_subscription_count: integer, days: [{date, events: [{kind: "bill" \| "dividend" \| "subscription", label, amount: decimal-string \| null, currency: iso-code-string \| null, source_id: string, status: "past" \| "today" \| "upcoming"}]}]}` — exactly one `days` entry per calendar day, ascending, **zero-filled**. `today` is `today_et()` (docs/29 F1d) and is the only clock behind `status`; the client never derives it. `amount` is **signed** (dividend +, bill/subscription −) and `currency` is the ISO code it is stated in — **all-or-nothing** (docs/36 decision 15): a bill is CAD, a dividend states the one normalized trade/account currency shared by all contributing rows (mixed/unknown rows withhold both fields), and a confirmed group is stated in the one currency its rows agree on; a variable bill or mixed/unknown-currency group carries `null` on both (the client paints `—`, never a relabelled CAD figure; no conversion happens here, docs/36 §Currency). `source_id` is `bill id \| security id \| the confirmed group's payee`, and `kind` says how to read it (click-through: `/bills`, `/securities/{id}`, `/bills?autofill=…` — the draft's `amount` param is written only for a CAD figure). Pure composition of the three existing sources — bills (docs/25, forward-projected from `next_due_date` with day-of-month clamping, never replayed backwards), the M29 dividend calendar and, since **M170**, `services/confirmed_subscriptions.py::confirmed_subscriptions` (the ONE confirmed-subscription query: active bills plus the recurring groups `POST /transactions/recurring/accept` tagged — never the M27 detector's candidates, docs/36 §M170) — with **no new detection, no fx read and no network** (docs/07/docs/12). Every query is user-scoped and committed-only. `confirmed_subscription_count` is an integer: the size of the user's WHOLE confirmed set (active bills + accepted groups, deduped), a fact about the **user** rather than about `month` — the grid says which confirmed subscriptions fall in this month, this count says how many exist at all, and it is what lets the client tell "no subscriptions confirmed yet" from "none this month" (docs/36 §M170). Errors: **422 `invalid_month`** for a malformed/out-of-band `month`. |
| `GET /reports/networth-snapshot?target_date=` | **M137 P1-1: ORPHAN — 410 `report_route_deprecated` under `SERVER_REPORTS_DEPRECATED=true` (no frontend caller at all).** Per-account cash balance at a historical date. Response: `{date, reporting_currency, accounts: [{account_id, account_name, currency, balance}], total_cash}`. Per-account `balance` is NATIVE currency (`starting_balance + Σ committed amount_native` through `target_date`); `total_cash` is the reporting-currency roll-up, converting each account at the newest cached rate on or before `target_date` and EXCLUDING an account with no resolvable rate. |

## Subscriptions (M27)

| Method/Path | Shape |
| --- | --- |
| `GET /subscriptions` | **RE-MOUNTED (M137 P1-2, docs/24, docs/33 §Server-first computing).** → 200 `{subscriptions: [{payee, frequency, avg_amount, annual_cost, occurrences, last_date, next_expected, …}], total_monthly, total_annual}` — `services/subscriptions.py::detect_subscriptions` (13-month trailing window, committed rows only, `posting_date` with `date` fallback) plus the removed route's own roll-up (monthly at face value, quarterly/yearly ÷ 12, weekly/biweekly × 4.33, both quantized to the cent). Read-only: `POST`/`PUT`/`PATCH`/`DELETE` on this path stay 405/404. Detection never moved — `bill_reminders`' cash-flow calendar has been calling this engine all along — so the route is that engine exposed again. M170 (docs/36 §M170): the cash-flow calendar no longer calls this engine — its `subscription` series reads `services/confirmed_subscriptions.py` instead — so since M170 this route is DETECTOR output and therefore a CANDIDATE list, never a list of confirmed subscriptions. M160 (P2-5) RETIRED the `subscriptionsReport` client twin with `s3Engine.ts` — this route is the only detector. The captured golden `subscriptions.detect` in `frontend/tests/fixtures/s3-vectors.json` now pins the ROUTE, replayed by `backend/tests/test_m160_replay_s3.py`. **M138 (docs/09 §Bills you probably have):** the DASHBOARD's subscriptions card stopped reading this route — `SubscriptionsPanel` now queries `GET /transactions/recurring/candidates?source=subscriptions`, so `listSubscriptions` has no `src/` consumer left (the module stays, as M137's thin read). `s3Engine.subscriptionsReport` is likewise no longer called from `src/` — and M160 (P2-5) deleted it with the engine; the removed-route golden it used to reproduce is now a server-route regression pin (docs/33 A12). |

Client contract (the shape the removed route returned — golden
`subscriptions.detect` in `frontend/tests/fixtures/s3-vectors.json`):

```json
{"subscriptions": [{"payee": "NETFLIX.COM", "frequency": "monthly",
  "avg_amount": "16.99", "annual_cost": "203.88", "occurrences": 11,
  "last_date": "2026-08-14", "next_expected": "2026-09-13"}],
 "total_monthly": "3274.73", "total_annual": "39296.76"}
```

- Detection: committed transactions with `amount_native < 0` whose booking
  `date` is inside the trailing 13-month window (first day of the month 12
  months back from `today_et()`, inclusive), grouped by normalized payee
  (uppercased, trailing `#?\d+` stripped); a group needs ≥ 2 occurrences.
- Effective date per row is `posting_date ?? date` (posting_date is
  nullable, docs/02); rows with neither are skipped.
- `frequency` from the average gap between sorted effective dates:
  weekly ≤10d, biweekly ≤20d, monthly ≤40d, quarterly ≤100d, else yearly;
  `<2` dates → `unknown` (dropped).
- `avg_amount = round_half_even(sum(abs(amount_native)) / n, 2)`;
  `annual_cost = round_half_even(sum(abs(amount_native)) / n × {weekly:52,
  biweekly:26, monthly:12, quarterly:4, yearly:1}, 2)` — the multiplier runs
  on the **unquantized** average (76.1530… × 52 = 3959.96, not the
  76.15 × 52 = 3959.80 you get from the rounded avg_amount);
  `next_expected = last_date +` {weekly:7, biweekly:14, monthly:30,
  quarterly:91, yearly:365} days (default offset 30). Money fields are 2dp
  Decimal-as-strings, rounded half-even (Python's default context).
- Ordering: `annual_cost` descending (stable — ties keep first-seen group
  order). `payee` is the group's first row's `normalized_payee ?? raw_description`.
- `total_monthly = Σ avg_amount(monthly) + Σ avg_amount/12 (quarterly,
  yearly) + Σ avg_amount×4.33 (weekly, biweekly)`, quantized half-even to
  2dp (`"0"` — scale 0 — when there are no subscriptions); `total_annual =
  round_half_even(total_monthly × 12, 2)`. Amounts are summed in their
  **native** currency, exactly as the removed route did (no fx conversion).

## Watchlist

| Method/Path | Shape |
| --- | --- |
| `GET /watchlist` | All watched securities with latest price + staleness. Response: `{items: [{security_id, symbol, name, listing_currency, latest_price?, price_date?, price_source?, price_age_days?, price_stale?, price_fetched_at: datetime | null, dead_symbol, dead_symbol_reason}], total}`. `price_age_days` is the whole-day age from `price_date` (null when no price). `price_fetched_at` comes from the same tenant-scoped row's `prices.fetched_at`; NULL means no evidence, not a placeholder. The last two (M141) are the dead-symbol state, so the row can say "no public quote" instead of the forever-"yet" "no price". |
| `POST /watchlist` | `{security_id}` → 201. 409 `already_watched`. Queues historical price backfill (skipped when the security is already flagged `dead_symbol`). |
| `DELETE /watchlist/{security_id}` | 204. |

**Picker note (M186).** The Watchlist card's combobox is the one security
picker that reaches BEYOND the table: with no stored match (query ≥ 2 chars,
600 ms after it settles) it calls `GET /securities/lookup` (securities table
above), and picking that answer runs `POST /securities` → `POST /watchlist`
before invalidating `["watchlist"]` and `["securities"]` (docs/09 §Watchlist
ticker search). No new route and no new payload: every status code and error
`code` involved (`lookup_unavailable`, `rate_limited`, `duplicate_security`,
`already_watched`) is the one the endpoint rows above already define, and the
trade form's picker deliberately keeps the local-only behaviour.

## Vault key sync (M53)

Wrapped-DEK enrollment across devices (docs/33 “Phase 4 (M53)”). The server
stores an **opaque** wrapped key blob per device; it never receives the
passphrase, the KEK or the DEK and never decodes the blob. Limits:
`MAX_ACTIVE_ENVELOPES = 10` active rows per user, `MAX_BLOB_CHARS = 8000`.

| Method/Path | Shape |
| --- | --- |
| `GET /vault/envelopes?device_id=` | `device_id` is 32 lowercase hex (`^[0-9a-f]{32}$`), generated once per browser profile. → `{items: [{id, device_id, label, wrapped_blob, revoked_at, created_at, updated_at}], revoked_own_device: bool, active_count: int}` — **active rows only** (`revoked_at IS NULL`), ordered `created_at asc, id asc`, scoped to the caller. `revoked_own_device` is true iff the caller's `device_id` has no active row but has at least one row of any state (the revocation signal). `label` is nullable plaintext metadata (browser · OS, ≤ 64 chars). Malformed/missing `device_id` → 422 `validation_error`; no/bad token → 401 `invalid_token`. |
| `POST /vault/envelopes` | `{device_id, label?, wrapped_blob}` (`extra="forbid"`) → **201** `{id, device_id, label, wrapped_blob, revoked_at: null, created_at, updated_at}`. 422 `validation_error` (shape/`device_id` pattern/`label` > 64), 422 `envelope_too_large` (`len(wrapped_blob) > 8000`), 409 `device_exists` (an active row already exists for this `device_id`), 409 `too_many_devices` (> 10 active), 403 `demo_account_restricted` (demo tenant cannot sync — enrollment stays local-only there). |
| `PUT /vault/envelopes/{envelope_id}` | `{wrapped_blob, label?}` (`extra="forbid"`; `label` applied only when present) → **200** the updated envelope. Re-wraps a device's blob (passphrase rotation). 404 `not_found` (unknown **or** another user's id), 409 `envelope_revoked` (a revoked row is a tombstone — re-enrollment is a `POST`), 422 `envelope_too_large`, 422 `validation_error`. |
| `POST /vault/envelopes/{envelope_id}/revoke` | → **200** the envelope with `wrapped_blob: null` and `revoked_at` set (blob dropped: `ck_vault_envelopes_state`). **Idempotent.** 404 `not_found` (unknown or foreign id), 403 `demo_account_restricted`. |

Invariants: partial unique index `uq_vault_envelopes_active (user_id,
device_id) WHERE revoked_at IS NULL` (at most one active envelope per device;
a revoked row does not block re-creation). There is deliberately no
“re-enroll a revoked identity” route and no “delete all envelopes” route
(docs/33 runbooks explain why). Client flows, failure strings and both
runbooks are pinned in docs/33 § Phase 4 (M53).

## Scheduled backup & restore (M56)

Scheduled, client-side-encrypted backup artifacts (docs/11 § "Scheduled backup
& restore"). The server's whole job is **custody of opaque ciphertext plus
schedule/retention bookkeeping**: it never receives a passphrase, the KEK or
the DEK, and it never decodes `ciphertext` (docs/33 key-holding model). A run
needs an open tab with the vault unlocked — there is no server-side scheduler
and there cannot be one while encryption is client-side. Limits:
`MAX_ARTIFACT_CHARS = 8_000_000` (length of `ciphertext`, sized to stay under
nginx `client_max_body_size 10m`; raise both together). All bodies are
`extra="forbid"`; every handler inherits the demo write guard, so the demo
tenant reads (`GET`) but gets 403 `demo_account_restricted` on every write.

| Method/Path | Shape |
| --- | --- |
| `GET /backup/schedule` | → 200 `{frequency, hour, weekday, day_of_month, keep_last_n}` — the caller's five `users` columns (`off`, 3, 0, 1, 5 for a fresh user). |
| `PUT /backup/schedule` | Same five-field body (full replace) → 200 the same shape. Out-of-range value / unknown `frequency` → 422 **`backup_invalid_schedule`** (message names the field); wrong type or unknown key → 422 `validation_error`. Prunes artifacts in the same transaction when `keep_last_n` is lowered. |
| `GET /backup/artifacts` | → 200 `{items: [{id, period, created_at, size_bytes}]}` — **metadata only, never `ciphertext`**, newest first (`created_at DESC, id DESC`), caller-scoped. |
| `POST /backup/artifacts` | `{period, ciphertext}` → **201** the metadata row. `period` must be a real calendar date shaped `2026-09-28` / `2026-W39` / `2026-09` (else 422 **`backup_invalid_period`**), `len(ciphertext) > 8_000_000` → 422 **`backup_too_large`**, not a 3-part `v1.<b64(iv)>.<b64(ct)>` envelope → 422 **`backup_invalid_envelope`**. **Upsert on `(user_id, period)`** — two racing tabs converge on one row and "Back up now" refreshes today's slot — then prunes to `keep_last_n`. |
| `GET /backup/artifacts/{id}` | → 200 `{id, period, created_at, size_bytes, ciphertext}` — the row needed to decrypt/restore. |
| `DELETE /backup/artifacts/{id}` | → **204** (permanent — retention is the only automatic delete). |
| `POST /backup/restore` | `{confirm: "restore", payload: {v, kind, period, exported_at, snapshot}}` — the **decrypted** artifact, because the server cannot decrypt anything → 200 `{restored: {table: rows}, absent_tables, skipped_tables, fx_rates_inserted}`. |

Restore error codes (all raised **before the first write**, so every 422
leaves the tenant untouched; `restore_integrity_error` additionally rolls the
wipe back): 422 `restore_confirm_required` (`confirm !== "restore"`),
`restore_invalid_payload`, `restore_unknown_table`, `restore_forbidden_table`
(`refresh_tokens`, `mcp_tokens`, `alembic_version`, `backup_artifacts`),
`restore_invalid_row` (message names table + index), `restore_integrity_error`
(FK/unique/NOT NULL at insert). Foreign and unknown artifact ids are both
404 `not_found`. Algorithm, skip/absent semantics, keep-last-N retention and
the coverage invariant are pinned in docs/11 § "Scheduled backup & restore
(M56)"; the client contract (`scheduledBackup.ts` due math, the Settings
panel, the AppShell runner) lives there too.

## Household sharing (M90, docs/43)

**Owner — `backend/app/api/sharing.py`, prefix `/api/v1/sharing`.** Full
contracts (checks-in-order, enumeration-oracle rule) in docs/43 §5. The same
router also serves the invitee's two mutations — `POST /sharing/invites/accept`
and `POST /sharing/leave` — so the partner read surface can stay literally
GET-only.

| Method/Path | Shape |
| --- | --- |
| `GET /sharing` | → 200 `SharingState` — the owner's **`active` link when one exists**, otherwise their latest row by id, whatever its status; no rows → `status: "none"`; never 404 (state first — M98 F-1) |
| `POST /sharing/invites` | `{email, account_ids?: [int]}` → **201 `InviteCreated`** — the ONLY body that ever carries `token` / `accept_url`; `email_sent` is honest (false under the owner's privacy mode or unconfigured SMTP). Never reveals whether the address has an account. Rate **5/min** (`SHARING_INVITE_LIMIT`) |
| `GET /sharing/invites` | → 200 `{items: [InvitePublic], total, limit, offset}` — never `token` / `accept_url` |
| `DELETE /sharing/invites/{invite_id}` | → 204 (pending → cancelled); 404 `sharing_missing`, 409 `sharing_not_active`. Rate **10/min** |
| `PATCH /sharing/scope` | `{account_ids: [int]}` → 200 `SharingState` for the **`active`** row (state-targeted, never newest — M98 F-1); no active row → 404 `sharing_not_linked`; `sharing_account_missing` for an id not owned. Rate **10/min** |
| `POST /sharing/revoke` | → 200 `SharingState` (`status: "revoked"`), written to the **`active`** row, idempotent (M79: the repeat answers 200 on the partnership that ended most recently); no active row and nothing ended → 404 `sharing_not_linked`. Rate **10/min** |
| `POST /sharing/leave` | no body → 200 `SharingState` (`status: "revoked"`) — the **partner's** withdrawal (M93, D6 follow-up), resolving `partner_user_id = caller AND status = 'active'` (state, never newest — M98 F-2) so it mutates ONLY the row that grants access; idempotent (M79), one `leave` receipt per 200 **naming the row actually mutated** with `actor_user_id = partner`; no partnership for the caller → 404 `sharing_not_linked` (same body as the partner read's 401, and **no** receipt — no owner to attribute it to). Rate **10/min** |
| `GET /sharing/audit?limit=&offset=` | → 200 `{items: [SharingAuditPublic], total, limit, offset}` — the owner's own receipts only |
| `POST /sharing/invites/accept` | `{token}` → 200 `SharingState`; 401 `sharing_invite_invalid` / `sharing_invite_expired`, 409 `sharing_invite_used` / `sharing_invite_cancelled` / `sharing_partner_exists` / `sharing_partner_occupied`, 403 `sharing_self_invite` / `demo_account_restricted`. Rate **10/min** |

**Partner — `backend/app/api/partner.py`, prefix `/api/v1/partner`. All five
routes are GET (pinned by `test_partner_router_is_get_only`), authenticated
with the normal session (docs/38) — the partner's ONLY writes in the whole
system are the two on the sharing router above: the accept, and (M93) their
own leave.

| Method/Path | Shape |
| --- | --- |
| `GET /partner/state` | → 200 `{linked: true, owner: {user_id, name, email}, account_ids, privacy_mode, link_id, accepted_at}` when an `active` link exists; → 200 `{linked: false}` when the authenticated caller has none (M92) |
| `GET /partner/accounts` | → 200 `PartnerAccountPage` `{items: [PartnerAccountPublic], total, limit, offset}` — owner's accounts ∩ scope; empty scope → `items: []`. Under the OWNER's `privacy_mode` (docs/43 §9) `cash_balance`/`investment_value`/`total_value`/`starting_balance` are JSON `null`; otherwise identical to the owner's `AccountPublic` (which since M145 can itself carry `null` cash for an unresolvable investment fx figure) |
| `GET /partner/transactions` | `?limit=&offset=&account_id=&from=&to=` → 200 `PartnerTransactionPage` — owner rows ∩ scope ∩ committed-only (`_live_scope`); `account_id` outside scope → 404 `sharing_account_missing`; `from`/`to` gate `transactions.date` only. Under the OWNER's `privacy_mode` (docs/43 §9) `amount_native`/`cad_amount` are JSON `null` **M107 (docs/44 §9):** the page is built by the SAME `_transaction_page`, so `items` are LEDGER rows and the payload gains `split_parents: list[PartnerSplitParentPublic]` (a `SplitParentPublic` whose `amount_native`/`cad_amount` widen to `Decimal \| null`). Under the OWNER's `privacy_mode` the **group header's money** is JSON `null` exactly like every other money field; `child_count` is structure, not money, and always survives. Scope invariant: a child is visible iff its parent is — children copy `account_id` and scope is account-level, so this holds by construction |
| `GET /partner/categories` | → 200 `CategoryPage` — byte-identical to `GET /categories` |
| `GET /partner/budgets` | → 200 bare list `[{id, user_id, category_id, amount, period, carry, carried_in, category_name}]` — `spent`/`remaining`/`pct_used` dropped (D3); `carried_in` is M110's own field, produced by the same `services.budgets.carry_in` the owner's route uses. Both `amount` and `carried_in` are JSON `null` under the OWNER's `privacy_mode` (docs/43 §9); `carry` is structure and never masked; the row keeps its identity |

No active link → **401 `sharing_not_linked`** from the four partner DATA
reads (uniform across never-linked / revoked / expired / cancelled /
owner-deleted), and the frontend lets it through as a domain state
(`AUTH_401_SKIP_PREFIXES`, no refresh → /login dance). `GET /partner/state`
is the exception (M92): an authenticated caller with no link gets
**200 `{linked: false}`** — asking about your own sharing state while signed
in is not an auth failure — and only a caller with no valid session is
refused here, by `get_current_user`, as `401 invalid_token`. Every partner
query reaches rows via
`SharingLink.partner_user_id == current_user.id` → `domain.user_id ==
link.user_id` (docs/43 §6 tenancy rule).

## Forecasting — what-if scenarios (M109, docs/45)

Owner surface: every route authenticates with `get_current_user`, so it reads
the CALLER's own tenant and inherits the global demo write guard (writes are
`403 demo_account_restricted` on the shared demo tenant, reads stay open).
No forecast data ever reaches `/partner/*` or the partner's `/shared` page —
see docs/45 §Tenancy.

| Method/Path | Request | Response |
| --- | --- | --- |
| `GET /forecast/scenarios` | — | `200 {items: [{id, name, events[]}], count}` |
| `POST /forecast/scenarios` | `{id, name, events[]}` | `201 {id, name, events[]}` |
| `GET /forecast/scenarios/{id}` | — | `200 {id, name, events[]}` |
| `PATCH /forecast/scenarios/{id}` | `{id, name, events[]}` | `200 {id, name, events[]}` — **full replace**; the path id wins |
| `POST /forecast/scenarios/{id}/copy` | `{id?, name?}` (both optional) | `201 {id, name, events[]}` — the server mints `id` when absent (`secrets.token_urlsafe(12)`) |
| `DELETE /forecast/scenarios/{id}` | — | `204` |
| `GET /forecast/defaults` | — | `200 {as_of, reporting_currency, net_worth_cad, cash_cad, positions_cad, income_monthly_cad, expense_monthly_cad, flow_window_months, recurring_rows, scenario_count}` |
| `GET /forecast/run` | query params below | `200 Run` (shape below) |

Storage is `users.forecast_scenarios JSON NOT NULL DEFAULT '[]'` (docs/02 §users,
migration `0042_forecast_scenarios`) — the M108-P2 `saved_report_views` pattern:
per-user content that roams with the account, entry shape validated at the API
seam by `app/schemas/forecast.py`, never by a SQL constraint. Cap **25 per
tenant**; an entry holds 0–12 events.

**Event shapes — a CLOSED set of five `type` values** (there is no formula
editor and no scripting surface; anything else is not buildable):

```json
{"id": "ev-7f3k2p9qx1", "type": "house_purchase", "date": "2027-06-01",
 "home_price_cad": "650000", "down_payment_cad": "130000",
 "mortgage_rate_pct": "4.49", "amortization_years": 25}
{"id": "…", "type": "career_break",        "start_date": "…", "end_date": "…"}
{"id": "…", "type": "salary_change",       "date": "…", "monthly_delta_cad": "800.00"}
{"id": "…", "type": "one_off_cash_event",  "date": "…", "amount_cad": "-25000.00"}
{"id": "…", "type": "recurring_cost_delta", "start_date": "…", "monthly_amount_cad": "-900.00", "end_date": null}
```

Every `id` (scenario and event) matches `^[A-Za-z0-9_-]{8,64}$` and is generated
by the CLIENT, so rename/copy address the id the list already carries. Amounts
are decimal strings (AGENTS §2 — no float ever touches them). `extra="forbid"`
on every shape: a typo'd field is a 422, never a silently dropped key.

### `GET /forecast/run` query parameters

| Param | Type | Default | Bounds |
| --- | --- | --- | --- |
| `scenario_ids` | comma-separated ids | *(absent → baseline only)* | ≤ 6, no duplicates, each resolves for THIS user else `404 scenario_not_found` |
| `horizon_months` | int | `24` | 1–120 |
| `granularity` | `day \| week \| month` | `day` | — |
| `inflation_pct` | decimal string | `2.00` | 0–20 |
| `annual_return_pct` | decimal string | `0.00` | −50–50 |
| `flow_window_months` | int | `12` | 1–36 |

Bounds answer `422 validation_error`. Response:

```json
{
  "as_of": "2026-10-06", "horizon_months": 24, "granularity": "day",
  "inflation_pct": "2.00", "annual_return_pct": "0.00", "flow_window_months": 12,
  "reporting_currency": "CAD",
  "buckets": ["2026-10-06", "…"],
  "inputs": { "cash_cad": "…", "positions_cad": "…", "net_worth_cad": "…",
    "history_from": "…", "history_to": "…",
    "income_monthly_cad": "…", "expense_monthly_cad": "…",
    "recurring_income_monthly_cad": "…", "recurring_expense_monthly_cad": "…",
    "residual_income_monthly_cad": "…", "residual_expense_monthly_cad": "…",
    "recurring_rows": 7 },
  "baseline": { "id": "baseline", "name": "Baseline", "values": ["…"],
                "real_values": ["…"], "final_cad": "…", "delta_cad": "0.00" },
  "scenarios": [ { "id": "sc_…", "name": "…", "values": ["…"],
                   "real_values": ["…"], "final_cad": "…", "delta_cad": "…" } ],
  "disclaimer": "Projection, not a prediction."
}
```

- `buckets` are bucket-END dates ascending and **`buckets[0]` is `as_of`** for
  `day` granularity, so the series opens at today's actual net worth.
- `values` = future (nominal) dollars, `real_values` = today's dollars at the
  single stated inflation rate; both are always returned so the UI toggle
  needs no refetch. `len(buckets) == len(values) == len(real_values)`.
- `inputs` publishes the decomposition so the panel can show it:
  `income_monthly_cad == recurring_income_monthly_cad + residual_income_monthly_cad`
  (and likewise for expenses), to the cent.
- **Determinism:** identical inputs ⇒ byte-identical JSON. No timestamp, no
  UUID and no dict-order dependence anywhere in the payload (docs/45 §3.3).

## Plain-English assistant (docs/57)

Ask in plain English; a reply that implies a data change arrives as a
**proposed-change card** the user applies with one click. The model proposes,
the server validates, the user applies — nothing is written autonomously, and
`before`/`after` are the changed fields only (`Decimals` as strings, AGENTS
§2).

| Method/Path | Request | Response |
| --- | --- | --- |
| `POST /assistant/ask` | `{question: str}` (1..2000, `extra="forbid"`) | `200 {answer: str, proposal: ProposalPublic \| null}` |
| `POST /assistant/proposals` | **M189 BYO-AI connector (docs/59 §3.1):** `{proposal_kind: str, proposal_target: str, proposal_summary: str, proposal_fields: object, question: str (1..2000), answer: str (1..2000)}` (`extra="forbid"`) — the same call the MCP `proposals_create` tool proxies (docs/40 §2) | `201 ProposalPublic` (status `pending`; **no LLM call**, no domain write — only the card row is persisted; `before` mirrors the target) |
| `GET /assistant/proposals?status=pending` | `status` ∈ pending/applied/dismissed/undone (default `pending`; unknown = 422 `validation_error`) | `200 {items: [ProposalPublic]}` (≤50, newest first) |
| `GET /assistant/proposals/{id}` | — | `200 ProposalPublic` |
| `POST /assistant/proposals/{id}/apply` | — | `200 ProposalPublic` (status `applied`) |
| `POST /assistant/proposals/{id}/dismiss` | — | `200 ProposalPublic` (status `dismissed`, nothing written) |
| `POST /assistant/proposals/{id}/undo` | — | `200 ProposalPublic` (status `undone`) |
| `GET /assistant/history?limit=50` | `limit` 1..200 (default 50) | `200 {items: [ProposalPublic]}` — `status ∈ (applied, undone)`, newest first, each with `apply_audit_id` / `undo_audit_id` |

`ProposalPublic` = `{id, question, answer, kind, target, target_label, summary,
before, after, status, created_at, applied_at, undone_at, apply_audit_id,
undo_audit_id}`; `kind` ∈ `account.update | transaction.update |
forecast_scenario.update`, `status` ∈ `pending | applied | dismissed | undone`. The `POST /assistant/proposals` row (M189) reuses the docs/57 validation registry with **no model in the loop**: `proposal_kind` is a plain string the registry owns (unknown kind → `assistant_proposal_invalid`, same family as a model hallucination), shape errors → `validation_error`, and the caller may only ever leave a `pending` card — `apply`/`dismiss`/`undo` stay user-session routes. Those three transitions run their whole transaction (proposal-row `FOR UPDATE` included) off the event loop in a worker thread (docs/01 §Sessions and the event loop, security F1), with the lock wait bounded per transaction. `POST /assistant/ask` hops its whole turn the same way (security F4): the provider send is a synchronous `httpx.post(timeout=60)`, so run on the event loop it held the whole API — `/health` included — for as long as whatever `llm_base_url` pointed at took to answer, up to 60 s per ask.
Rate limit: `12/minute` on `/assistant/ask` (`ASSISTANT_ASK_LIMIT`), on
`POST /assistant/proposals` (`ASSISTANT_PROPOSE_LIMIT`), and one shared
`12/minute` bucket across apply/dismiss/undo (`ASSISTANT_TRANSITION_LIMIT`;
the path id never mints a bucket per card) — `429 {detail: "Too many
attempts", code: "rate_limited"}` when enabled. The propose and transition
ceilings are new (security F3): slowapi decorates per route, so the old
"the propose route inherits ask's posture" claim held nothing in reserve.
The propose ceiling runs TWICE over (security F3-b): per client IP (skipped
when the request carries no forwarded hop) and per **authenticated tenant**,
because the MCP connector's calls arrive from the platform's own container
address — see docs/59 §8.

Errors (docs/57 §4–§5): `assistant_proposal_invalid` (422 — unparseable model
reply, a connector proposal whose `proposal_kind` the registry rejects, or
failed validation, **nothing written**), `assistant_unavailable`
(503), `assistant_busy` (503 — a transition's proposal-row lock wait hit its
per-transaction bound; retry), `assistant_proposal_not_found` (404), `assistant_proposal_not_pending`
(409), `assistant_proposal_stale` (409), `assistant_undo_conflict` (409),
`assistant_proposal_not_undoable` (409), `not_found` (404 — the target row is
gone at apply/undo), `transaction_is_split` (409),
`transaction_in_pending_batch` (409), `encryption_required` (422), plus the
shared LLM seam's `llm_upstream_error` (502), `invalid_llm_base_url` (422),
`privacy_mode_blocks_external_api` (403) and `demo_account_restricted` (403 —
every POST on the demo tenant, `POST /assistant/proposals` included;
`GET /assistant/history` passes as a read). A caller without a valid token gets
the ordinary `401 invalid_token`; over the MCP surface the same route is
reachable only to a `read_propose` session (`403 mcp_read_only` /
`403 mcp_read_propose` otherwise, docs/40 §2).

## Error envelope

Every error response is consistent:

```json
{ "detail": "human-readable message", "code": "machine_readable_code" }
```

Examples: `{detail, code: "duplicate_transaction"}`, `{detail, code: "batch_not_pending_review"}`, `{detail, code: "privacy_mode_requires_local_llm"}`, `{detail, code: "privacy_mode_blocks_external_api"}` (both M7), `{detail, code: "category_in_use"}`, `{detail, code: "fx_date_not_gap"}`, `{detail, code: "invalid_date_range"}`.

**The client branches on `code`, NEVER on `detail` (M113).** `detail` is
human copy — rewordable at any time; `code` is the contract field. The web
client's envelope type is `frontend/src/api/client.ts::ApiError`
(`constructor(status, detail, code)` calls `super(detail)`), so
`err.message` and `apiErrorMessage(err)` are ALWAYS the `detail`. Testing a
`code` token against them (`err.message.includes("duplicate_tag")`) compares
two different envelope fields: the branch is dead and the raw `detail` ships,
while the token sits in `detail` only by coincidence — one copy edit flips it.
The one predicate is `hasErrorCode(err, code)` in `client.ts`
(`err instanceof ApiError && err.code === code`); M112's
`isMissingImporterProfile` (Imports page) is a thin wrapper over it, and the
six `Tags` / `Settings` / `TagManagerModal` sites were converted in M113.
Anything that is not the envelope (a network failure, a proxy's HTML body)
has no `code` and matches nothing. Regression:
`frontend/tests/m113DetailVsCode.test.tsx` (every site pinned in both
directions — detail without the token + code with it renders the friendly
copy; a detail that *contains* the token under another `code` must not).


Additional error codes raised across the API (machine-readable `code` values with HTTP status): `lookup_unavailable` (404), `fx_unavailable` (404), `fx_rate_missing` (422), `already_watched` (409), `system_category` (409), `institution_in_use` (409), `account_in_use` (409), `security_in_use` (409), `category_cycle` (422), `invalid_category` (422, `GET /reports/spending?category_id=` — neither an integer ≥ 1 nor the literal `uncategorized`, docs/12 §Category drill-down), `invalid_regex` (422), `pattern_too_long` (422), `invalid_amount` (422), `validation_error` (422), `invalid_cursor` (422, `GET /notifications/inbox` — cursor fails base64url/JSON/`v:1`/`at`/`k` decode, docs/35), `report_route_deprecated` (410, the orphan `/reports/*` route — `/reports/networth-snapshot`, the last of M137's four — when `SERVER_REPORTS_DEPRECATED=true`; docs/33 §Server-first computing), `duplicate_email` (409, `PATCH /users/me/profile` only since M87 — `POST /auth/register` never answers it), `password_too_long` (422), `invalid_credentials` (401), `already_paired` (409), `transfers_category_not_found` (404), `no_matching_transfer` (404), `suggestion_not_pending` (409), `unsupported_format` (422), `missing_importer_profile` (422), `parse_error` (422), `invalid_profile` (422 — `detail` is ONE actionable line built by `csv_profile.profile_error_message`, e.g. `Invalid profile: credit column mapping is empty — set the Credit column or switch to single-amount mode`; pydantic's own record (`1 validation error for CsvProfile … input_value=…`) is never surfaced), `file_too_large` (413), `too_many_rows` (413), `invalid_drip_price` (422), `account_not_investment` (422), `oversell` (422), `invalid_corporate_action` (422), `demo_account_restricted` (403), `demo_mcp_read_only` (422, `POST /auth/mcp-tokens` from the demo tenant with an explicit `policy: "full"` — M81, docs/40 §1), `demo_disabled` (404, `POST /auth/demo` and `POST /auth/demo/reset` with `SEED_DEMO_ACCOUNT=false`), `current_password_required` (401), `duplicate_budget` (409), `duplicate_tag` (409), `tag_in_use` (409), `missing_token` (401), `invalid_mcp_token` (401), `mcp_token_expired` (401), `mcp_token_cap` (400), `forbidden` (403), `rate_limited` (429), `llm_upstream_error` (502), `llm_test_invalid_config` (400), `invalid_llm_base_url` (422), `privacy_mode_requires_local_llm` (409), `invalid_recurring_intent_tag_ids` (422, `PATCH /users/me` — a nominated `recurring_intent_tag_ids` entry the caller does not own; the column is JSON so the FK lives at the write seam, M174), `privacy_mode_blocks_external_api` (403), `csv_line_too_long` (422), `csv_too_complex` (422), `csv_parse_error` (422), `ofx_parse_error` (422), `fx_future_date` (422), `invalid_month` (422, `GET /reports/cashflow-calendar` — `month` is not a real `YYYY-MM` calendar month in 1900–2200, docs/36), `export_missing_scoping` (500), `export_registry_drift` (500), `export_invalid_vault` (422, `GET /export/zip` + `GET /export/ofx` — `vault` not `locked`/`unlocked`, docs/11), `export_invalid_schedule` (422, `PUT /export/schedule` — unknown `frequency` or out-of-range field), `backup_invalid_schedule` (422, `PUT /backup/schedule`), `backup_invalid_period` (422), `backup_too_large` (422), `backup_invalid_envelope` (422), `restore_confirm_required` (422), `restore_invalid_payload` (422), `restore_unknown_table` (422), `restore_forbidden_table` (422), `restore_invalid_row` (422), `restore_integrity_error` (422), `sync_provider_unknown` (404, unknown provider slug on
`GET /sync/providers/{slug}/institutions` or `POST /sync/agreements`), `sync_agreement_missing` (404,
unknown or another tenant's agreement id), `sync_agreement_exists` (409, a second live agreement for the
same `(user, provider)`), `sync_agreement_revoked` (409, pulling after revoke), `sync_auth_required` (409,
the agreement needs re-consent/MFA), `sync_account_mapping_incomplete` (422, the adapter did not map every
requested account), `sync_provider_unavailable` (502, adapter transport/upstream failure). `invalid_totp_code` (401, docs/38 — wrong/expired/replayed/malformed TOTP code), `invalid_recovery_code` (401, unknown or already-used recovery code), `invalid_or_expired_challenge` (401, missing/tampered/expired/wrong-purpose challenge token), `totp_not_enrolled` (409, confirm/disable with no factor), `totp_not_enabled` (401, code posted after the factor was disabled), `invalid_passkey` (401, failed WebAuthn assertion), `invalid_passkey_response` (422, failed WebAuthn registration verification), `passkey_cap` (400, 10 live passkeys), `passkey_exists` (409, credential id already registered), `no_passkeys` (404 — **retired** by M87, docs/42: `POST /auth/passkeys/login/options` is always 200), `docs_auth_required` (401, `GET /docs` / `/redoc` / `/openapi.json` without a session access token or a live MCP token — M87, docs/42), `oidc_disabled` (404), `oidc_pending_expired` (401), `oidc_unknown_account` (404 — only reachable as a redirect query parameter on the browser path) `channel_not_configured` (409, enabling a notification channel whose transport has no server config), `privacy_opt_in_required` (409, enabling a notification channel under privacy mode without the explicit opt-in), `delivery_not_retryable` (409, retry on a notification delivery that is not `dead`), `invalid_webhook_url` (422, webhook target is not an `http(s)://host`), `webhook_target_forbidden` (403, webhook target fails the address policy). M90 household sharing (docs/43 §7): `sharing_not_linked` (401, every `/partner/*` **data** read with no active link — `GET /partner/state` answers 200 `linked: false` instead, M92; **404** on `POST /sharing/leave` with no partnership for the caller **and on owner `PATCH /sharing/scope` / `POST /sharing/revoke` with no active row and nothing ended**, M98 F-1/F-2 — same `{detail, code}` body, different status so a mutating path can never trigger the client's refresh → /login dance), `sharing_invite_invalid` (401, unknown/tampered accept token), `sharing_invite_expired` (401), `sharing_invite_used` (409, single-use token; **also the OWNER-side accept-race loser**, M98 F-6 — the owner's seat was consumed by the winning accept), `sharing_invite_cancelled` (409), `sharing_partner_exists` (409, one active link per owner — at create and on the accept pre-check), `sharing_partner_occupied` (409, the invitee already shares another household — including the accept race loser **when the partner seat refuses**), `sharing_self_invite` (403, own address or own token), `sharing_invite_exists` (409, duplicate pending address), `sharing_invite_cap` (400, 5 outstanding invites), `sharing_missing` (404, unknown/foreign invite id on the cancel route), `sharing_not_active` (409, cancel of a non-pending invite), `sharing_account_missing` (404, scope id not owned by the owner, or a partner `account_id` outside the scope). M109 forecasting (docs/45 §4): `scenario_not_found` (404, unknown **or another tenant's** id — deliberately the same answer, so there is no existence oracle), `scenario_limit_reached` (409, a 26th scenario), `scenario_exists` (409, create/copy supplying an id this tenant already holds), `unknown_event_type` (422, an `event.type` outside the five v1 types — raised deliberately INSTEAD of `validation_error` so the envelope names the actual mistake).

**M/docs57 plain-English assistant (docs/57) — every code `/assistant/*` adds:** `assistant_proposal_invalid` (422, unparseable model reply — no proposal row, no audit row, no domain write), `assistant_unavailable` (503, no model configured), `assistant_busy` (503, a transition's proposal-row lock wait hit its per-transaction `LOCK_WAIT_LIMIT` — another decision on this card is in flight, retry), `assistant_proposal_not_found` (404), `assistant_proposal_not_pending` (409, apply/dismiss on a decided card), `assistant_proposal_stale` (409, the live row no longer equals `before`), `assistant_proposal_not_undoable` (409, undo outside `status=applied`), `assistant_undo_conflict` (409, the live row no longer equals the applied `after`). Per-endpoint contract: § Plain-English assistant above and docs/57 §4–§5.

**M107 split transactions (docs/44 §4.3) — every code the four `/transactions/{id}/split` routes and the split guards add:** `split_account_not_allowed` (422), `split_nested` (422), `split_too_few_legs` (422), `split_too_many_legs` (422), `split_zero_parent` (422), `split_leg_zero` (422), `split_leg_sign` (422), `split_sum_mismatch` (422), `encryption_required` (422 — **existing code, new trigger**: a child sent without `amount_cipher` while `users.encryption_required` is on), `already_split` (409), `not_split` (409), `transaction_is_split` (409), `split_leg_immutable` (409). Per-code contract, the 13-step validation order and which route raises what: § Split transactions above and docs/44 §4.3.