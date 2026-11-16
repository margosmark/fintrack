# 38 — Auth Hardening: TOTP 2FA, Passkeys, Optional OIDC SSO (M74)

Second-factor authentication for self-hosted installs: per-user TOTP (RFC 6238)
with single-use recovery codes, WebAuthn passkeys (registration + passwordless
login), and an optional, config-gated OIDC SSO flow against self-hosted
providers (Authelia / Keycloak class). Every path issues the SAME session the
password login issues — one `_issue_pair`, one httpOnly refresh cookie, one
`TokenResponse` shape — so the MCP `BackendClient` cookie contract (docs/08
"Refresh delivery") is untouched by construction.

## Context

Two-factor auth and SSO are the table stakes of the 2026 self-hosted finance
category: shipped 2FA + passkeys is one of the most-upvoted open requests in the
adjacent OSS space, optional OIDC is the top open request against Firefly III,
and r/selfhosted operators assume an app behind their Authelia/Keycloak will
offer SSO. Until now fin_tracker had exactly one factor (password) and no SSO
path.

Constraints this spec has to hold:

- **Spec-first (AGENTS §7):** this doc pins every endpoint, payload field,
  table, error code and verification step BEFORE any code exists.
- **One session issuance path.** Password login, TOTP login, recovery login,
  passkey login and OIDC confirm all call the same `_issue_pair` +
  `_set_refresh_cookie` helpers in `backend/app/api/auth.py`. No second token
  contract, no token in any JSON body (audit M1), no new cookie carrying a
  refresh token.
- **Standard error envelope** (`{detail, code}`) on every non-redirect failure;
  login-burst 429 pattern preserved on every new login-adjacent route.
- **Demo tenant exempt from 2FA** — the demo door stays one click.

### Decisions (locked)

1. **TOTP is verified server-side with SHA-1 / 6 digits / 30 s step and a
   ±1-step window** (RFC 6238 defaults every authenticator app ships). No new
   dependency: `app/services/totp.py` implements HOTP/TOTP over stdlib `hmac`.
2. **Codes are single-use.** The factor row stores `last_used_step`; a code
   whose accepted step is `<= last_used_step` is rejected
   (`401 invalid_totp_code`). Recovery codes are stored as SHA-256 hashes and
   claimed with a conditional `UPDATE ... WHERE used_at IS NULL` — the database
   decides the winner, exactly like refresh-token rotation.
3. **Enrollment is two-step.** `setup` only stashes an encrypted PENDING secret
   (the active factor, if any, keeps protecting the account); `confirm` verifies
   a real code against the pending secret, activates it and mints a fresh set of
   10 recovery codes (the previous set is destroyed).
4. **Login is a challenge, not a fork.** `POST /auth/login` stays one endpoint:
   for an account with an active factor it returns
   `{challenge: "totp", challenge_token, expires_in}` (200) instead of a token
   pair. The challenge token is a 5-minute signed JWT carrying
   `{sub, type: "challenge", method}`; the code is then posted to a dedicated
   endpoint that mints the session. A challenge proves "password verified
   moments ago", so no session exists until the second factor lands.
5. **Passkeys are a first-class factor and a login path.** Registration needs a
   live session (it is a credential add); passwordless login resolves the
   account by email first and answers the SAME challenge envelope for every
   address — **M87 / docs/42**: the old `404 no_passkeys` vs `200` split was
   an account-existence oracle (the `register` 409 it leaned on is gone too),
   so an unknown or keyless email now gets a throwaway challenge bound to a
   user id that resolves to nothing, and only the assertion step fails. A passkey
   login issues the session directly, with no TOTP step: a WebAuthn assertion
   with user verification IS the second factor, and phishing-resistant ones at
   that.
6. **rpID and expected origin come from config, never from the request's
   `Origin` header.** Trusting the header would let a stolen 15-minute access
   token enroll a passkey minted on an attacker origin, turning a short-lived
   token into a persistent login. `APP_ORIGIN` pins both.
7. **OIDC is off by default and link-only.** No local account ⇒ no session
   (`oidc_unknown_account` redirect); the operator's IdP can never create a
   user. An email match creates the link only after an EXPLICIT confirmation
   click; once `oidc_links` holds `(issuer, subject) → user`, later logins are
   the familiar one-click SSO.
8. **No token ever appears in a URL.** The OIDC callback hands its result to the
   SPA through one short-lived httpOnly cookie (`oidc_pending`, SameSite=Lax —
   Lax is mandatory so a top-level cross-site navigation can both carry the
   `state` cookie in and set this one out). The SPA reads it via
   `GET /auth/oidc/preview` and exchanges it with `POST /auth/oidc/confirm`.
9. **TOTP secrets are encrypted at rest** with `encrypt_secret`
   (`app/core/crypto.py`). The Fernet key is `APP_ENCRYPTION_KEY` when
   configured and falls back to the legacy sha256(`JWT_SECRET`) derivation when
   not (t_d3176af6) — so an existing install keeps decrypting with no migration,
   while a deployment that SETS `APP_ENCRYPTION_KEY` decouples at-rest
   encryption from token signing and rotating `JWT_SECRET` no longer orphans
   stored factors. Same treatment `llm_api_key` gets. Back `APP_ENCRYPTION_KEY`
   up WITH the database; `last_used_step` and the recovery-code hashes are
   unaffected either way.
10. **The demo tenant cannot enroll anything and is never challenged.** Every
    enrollment/disable route is a mutating route behind `get_current_user`, so
    the shared guard answers `403 demo_account_restricted`; login additionally
    skips the challenge for `demo@fintracker.local` (defense in depth — the
    public demo password must never acquire a second factor), and OIDC link
    confirmation rejects the demo account with `403 forbidden`.
11. **Server-signed JWTs are RS256 across two key families** (t_d3176af6,
    `app/core/jwt_keys.py`). The `access` key signs `type=access` bearer
    tokens; the `session` key signs `type=challenge` (2FA step-up) and
    `type=oidc_pending` (the OIDC hand-off) — the two families that escalate
    into a session. Splitting them means an access-key leak cannot forge a
    long-lived session and a session-key leak cannot mint bearer tokens.
    Every token carries a `kid` pinned to its family, and verification hardcodes
    `RS256` (never reads `alg` from the token), so `alg:none` and
    algorithm-confusion are impossible. `JWT_SECRET` no longer signs any JWT.
    Keys come from PEM files (`JWT_{ACCESS,SESSION}_{PRIVATE,PUBLIC}_KEY_PATH`,
    provisioned by `scripts/generate_jwt_keys.py`); empty paths generate an
    ephemeral in-process keypair (dev/test only — a deployment MUST set the
    files so tokens survive restarts / multiple workers).

## Configuration

New keys in `backend/app/core/config.py::Settings` (all mirrored in
`.env.example`):

| Key | Default | Meaning |
| --- | --- | --- |
| `TOTP_ISSUER` | `fin_tracker` | Issuer label in `otpauth://` URLs and authenticator apps |
| `APP_ORIGIN` | `http://localhost:5173` | Public origin of the SPA (`scheme://host[:port]`, no trailing slash). Source of the WebAuthn rpID (its hostname) AND the expected WebAuthn origin AND the post-OIDC-redirect base |
| `OIDC_ENABLED` | `false` | Master switch; every OIDC route except `GET /auth/oidc/config` answers `404 oidc_disabled` while false |
| `OIDC_ISSUER` | `""` | Issuer URL, e.g. `https://auth.example.com` (no trailing slash). Operator-trusted, like `LLM_BASE_URL` |
| `OIDC_CLIENT_ID` | `""` | Client id registered at the provider |
| `OIDC_CLIENT_SECRET` | `""` | Client secret (never leaves the server, never logged) |
| `OIDC_REDIRECT_URI` | `""` | Registered redirect URI; empty derives `{APP_ORIGIN}/api/v1/auth/oidc/callback` |
| `OIDC_SCOPES` | `openid email profile` | Space-separated scope string |
| `JWT_ACCESS_PRIVATE_KEY_PATH` / `JWT_ACCESS_PUBLIC_KEY_PATH` | `""` | PEM files for the `access` signing keypair (RS256). Empty = ephemeral dev keypair; a deployment sets both (together) |
| `JWT_ACCESS_KID` | `access-1` | `kid` stamped on / required in access-token headers |
| `JWT_SESSION_PRIVATE_KEY_PATH` / `JWT_SESSION_PUBLIC_KEY_PATH` | `""` | PEM files for the `session` signing keypair (challenge + OIDC hand-off). Empty = ephemeral dev keypair |
| `JWT_SESSION_KID` | `session-1` | `kid` stamped on / required in session-token headers |
| `APP_ENCRYPTION_KEY` | `""` | Fernet key for at-rest secret encryption. Empty = legacy sha256(`JWT_SECRET`) derivation; setting it decouples encryption from token signing |
| `APP_ENCRYPTION_KEY_FALLBACKS` | `""` | Comma-separated OLD Fernet keys, tried on decrypt only, so a key rotation never strands ciphertext |

`APP_ORIGIN` is a NEW key on purpose: `NOTIFY_APP_BASE_URL` is unset in the
deployed `.env` (verified — it falls back to `http://localhost:5173`), so
deriving the WebAuthn rpID from it would silently pin `localhost` in
production. Deployment sets `APP_ORIGIN=https://fintrac.princessandmark.com`.

### Key provisioning in a compose deployment (t_adfb7881)

`docker-compose.yml` mounts the repo-root `./secrets` directory (gitignored)
**read-only** into the api container at `/run/secrets/jwt` — the only way key
material reaches the process: it is never committed (gitignore `secrets/` +
`*_private.pem`) and never baked into the image (the api build context is
`./backend`, which cannot see the repo root). The `mcp` service deliberately
gets no such mount (H6: it must never hold key material).

**The `.env` paths must be container-visible, not host paths.** The api
process lives in the container, so the four `JWT_{ACCESS,SESSION}_{PRIVATE,PUBLIC}_KEY_PATH`
values point at files under the mount:

```bash
JWT_ACCESS_PRIVATE_KEY_PATH=/run/secrets/jwt/jwt_access_private.pem
JWT_ACCESS_PUBLIC_KEY_PATH=/run/secrets/jwt/jwt_access_public.pem
JWT_SESSION_PRIVATE_KEY_PATH=/run/secrets/jwt/jwt_session_private.pem
JWT_SESSION_PUBLIC_KEY_PATH=/run/secrets/jwt/jwt_session_public.pem
```

A host path (`/home/developer/fin_tracker/secrets/...`) does not exist inside
the container; `_load_family` hard-fails on the first token operation rather
than silently signing with a fresh ephemeral key, so a bad mount or a
mis-typed path breaks loudly instead of invalidating tokens on every restart
again. `tests/test_jwt_mount_contract.py` pins the compose mount, the
container-path convention in `.env.example`/this doc, and the hard-fail itself
— a bad mount fails the gate, not production.

**Provisioning steps (deploy host):**

```bash
cd /home/developer/fin_tracker/backend
uv run python scripts/generate_jwt_keys.py --out ../secrets   # writes 4 PEMs + prints an APP_ENCRYPTION_KEY
# then set the four *_KEY_PATH lines above (container paths!) + APP_ENCRYPTION_KEY in .env
docker compose up -d api                                      # recreates api with the mount
```

- Run the generator **before** the next `docker compose up -d`, or `mkdir
  secrets` first: compose auto-creates a missing bind source **root-owned**,
  which the generator (running as your user) cannot write into.
- The PEMs must be readable by uid 1000 (`appuser` inside the container) —
  the generator's `chmod 600` on the private keys is fine because the deploy
  user is uid 1000 on example.internal.
- Rotate a family by generating new keys, moving the old KID's files into the
  FALLBACKS posture manually (deploy-time: keep old public keys reachable if
  in-flight tokens must verify — current code verifies against the configured
  file only, so rotation is a re-login event), then `docker compose up -d api`.
- **`APP_ENCRYPTION_KEY` is settable with zero migration.** `crypto.decrypt_secret`
  tries the primary key → each `APP_ENCRYPTION_KEY_FALLBACKS` entry → the
  legacy sha256(`JWT_SECRET`) derivation, so every ciphertext written before
  the key existed stays readable the moment it is set; new writes use the
  primary. Back it up WITH the database — losing it makes stored TOTP seeds /
  LLM keys unreadable.

Without any of the four paths set (dev/test), the api generates an ephemeral
in-process keypair and logs an `EPHEMERAL` warning per family — tokens then
do NOT survive a restart or a second worker, which is the pre-provisioning
state this section exists to eliminate in production.

## Models (migration `0031_auth_second_factor`)

All four tables live in `backend/app/models/auth_factors.py`, every row scoped
by `user_id` (AGENTS §2 tenancy), timestamps from `TimestampMixin`.

### `totp_factors`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `user_id` | int FK → `users.id`, UNIQUE (`uq_totp_factors_user`) | one factor per user |
| `secret` | text NULL | Fernet ciphertext of the ACTIVE base32 secret; NULL until `confirm` |
| `pending_secret` | text NULL | Fernet ciphertext of the secret awaiting confirmation |
| `last_used_step` | bigint NULL | highest accepted TOTP timestep; replay guard |

`enabled` ⇔ `secret IS NOT NULL`; `pending` ⇔ `pending_secret IS NOT NULL`.

### `recovery_codes`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `user_id` | int FK → `users.id`, indexed | |
| `code_hash` | text | SHA-256 hex of the normalized code (`XXXXX-XXXXX` → 10 chars upper) |
| `used_at` | timestamptz NULL | single-use marker; NULL = live |
| `created_at` / `updated_at` | timestamptz | `TimestampMixin` |

`UNIQUE (user_id, code_hash)`. Exactly 10 live rows exist after each `confirm`;
`confirm` deletes the user's previous rows first.

### `webauthn_credentials`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `user_id` | int FK → `users.id`, indexed | |
| `credential_id` | bytea UNIQUE (`uq_webauthn_credentials_credential_id`) | raw credential id |
| `public_key` | bytea | COSE-encoded public key (as returned by the verifier) |
| `sign_count` | bigint | last accepted counter, monotonic |
| `transports` | text NULL | comma-joined transports hint (e.g. `internal,hybrid`) |
| `name` | text | user label, default `"Passkey"` |
| `last_used_at` | timestamptz NULL | stamped on every accepted assertion |

Max **10 live passkeys per user** (`400 passkey_cap`, mirroring
`MCP_TOKEN_ACTIVE_CAP`).

### `oidc_links`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `user_id` | int FK → `users.id`, indexed | |
| `issuer` | text | normalized `OIDC_ISSUER` |
| `subject` | text | `sub` claim from the verified ID token |
| `created_at` | timestamptz | |

`UNIQUE (issuer, subject)` — one provider identity maps to exactly one local
account. Email is only ever the MATCHING key for creating a link, never the
identity itself.

## TOTP service (`backend/app/services/totp.py`)

Pure functions, no I/O, no app state:

- `new_secret() -> str` — 20 random bytes, base32 without padding (RFC 4648,
  uppercase, `=` stripped).
- `otpauth_url(secret, email, issuer) -> str` —
  `otpauth://totp/{urlencoded issuer}:{urlencoded email}?secret=…&issuer=…&algorithm=SHA1&digits=6&period=30`
- `totp_code(secret, at: datetime) -> str` — HOTP over `floor(unix/30)`:
  HMAC-SHA1 → dynamic truncation → 6 digits.
- `verify_totp(secret, code, at, last_used_step) -> int | None` — returns the
  accepted step when `code` matches step `t-1`, `t` or `t+1` AND
  `step > last_used_step`; else `None` (wrong, expired, or replayed — one code
  for all three outcomes so the client has a single failure path).
- `new_recovery_codes(n=10) -> list[str]` — 10 chars each from
  `ABCDEFGHJKMNPQRSTVWXYZ23456789` (no `0/O/1/I/L/U`), formatted
  `XXXXX-XXXXX` (~49 bits per code; online guessing is bounded by the 10/min
  limit anyway, so SHA-256 storage is proportionate — same hash choice as
  refresh/MCP tokens).
- `normalize_recovery(code) -> str` — uppercase, strip spaces/dashes.
- `hash_recovery(code) -> str` — SHA-256 hex.

## Routes

Prefix `/api/v1` for all. `S` = `429 rate_limited` limit. Errors use the
`{detail, code}` envelope. `Auth` = requires `get_current_user` (therefore the
demo write guard applies to mutating methods).

### TOTP

| Method/Path | Auth | S | Request → Response |
| --- | --- | --- | --- |
| `GET /auth/totp/status` | Auth | — | → `200 {enabled: bool, pending: bool}` |
| `POST /auth/totp/setup` | Auth | 5/min | → `200 {secret, otpauth_url}` — upserts `pending_secret`, leaves an active factor alone |
| `POST /auth/totp/confirm` | Auth | 5/min | `{code}` → `200 {recovery_codes: [str ×10]}` — activates the pending secret, replaces all recovery codes |
| `POST /auth/totp/disable` | Auth | 5/min | `{code}` (a TOTP code OR a recovery code) → `204` — deletes factor + recovery codes |

Errors: `401 invalid_totp_code` (confirm/disable: wrong, expired, or replayed),
`401 invalid_recovery_code` (disable: unknown or already-used recovery code),
`409 totp_not_enrolled` (confirm/disable with nothing to act on),
`403 demo_account_restricted` (demo tenant), `429 rate_limited`.

`code` is accepted as a plain string on every route: a malformed code (not
`^\d{6}$`) answers `401 invalid_totp_code`, never `422`, so clients see one
failure shape.

### Login + challenge

| Method/Path | Auth | S | Request → Response |
| --- | --- | --- | --- |
| `POST /auth/login` | — | 10/min | `{email, password}` → `200` **either** the usual `TokenResponse` **or** `200 {challenge: "totp", challenge_token, expires_in: 300}` when the account has an active factor (demo tenant excluded) |
| `POST /auth/login/totp` | — | 10/min | `{challenge_token, code}` → `200 TokenResponse` + refresh cookie |
| `POST /auth/login/recovery` | — | 10/min | `{challenge_token, code}` → `200 TokenResponse` + refresh cookie |

The union is a pydantic discriminated-by-shape pair (`TokenResponse` has no
`challenge` field; `LoginChallenge.challenge` is `Literal["totp"]`), so the
OpenAPI schema stays honest and existing clients that receive a pair are
untouched.

Errors: `401 invalid_credentials` (unchanged), `401
invalid_or_expired_challenge` (missing/tampered/expired/wrong-purpose token, or
the user/factor vanished mid-challenge), `401 invalid_totp_code`, `401
invalid_recovery_code`, `401 totp_not_enabled` (factor disabled between
challenge and verify), `429 rate_limited`.

Recovery login consumes the code, keeps the remaining codes valid, and does not
re-mint — the user re-rolls from Settings after using one.

### Passkeys (WebAuthn)

rpID = `urlparse(APP_ORIGIN).hostname`; expected origin = `APP_ORIGIN`
(single value). Algorithms: `ES256 (-7)`, `RS256 (-257)`, `EdDSA (-8)`.
`attestation: "none"` on registration — the server verifies structure,
origin, rpID hash and (on assertion) the signature; it does not build a
trust path to a manufacturer.

| Method/Path | Auth | S | Request → Response |
| --- | --- | --- | --- |
| `GET /auth/passkeys` | Auth | — | → `200 {items: [{id, name, created_at, last_used_at, transports}], total}` |
| `POST /auth/passkeys/options` | Auth | 10/min | → `200 {challenge_token, options}` — `PublicKeyCredentialCreationOptions` (base64url fields, `excludeCredentials` populated) |
| `POST /auth/passkeys` | Auth | 10/min | `{challenge_token, credential, name?}` → `201 {id, name, created_at, transports}` |
| `DELETE /auth/passkeys/{id}` | Auth | — | → `204`, `404 not_found` (foreign id is 404, never a probe-able 403) |
| `POST /auth/passkeys/login/options` | — | 10/min | `{email?}` → **200** `{challenge_token, options}` (`PublicKeyCredentialRequestOptions`) for EVERY input — **M87 (docs/42)**. **Email-first** (non-blank `email`, M87): a real credential list for an account that has one; a throwaway challenge (random 32-byte descriptor) for an account without one and for no account at all, so `allowCredentials` is present and same-shaped either way. **Subject sealed (SEC-1):** `challenge_token` is signed, not encrypted, so this purpose's subject is sealed with the at-rest key at a fixed width (`security.py::BLINDED_SUBJECT_METHODS`) — a keyless reader sees the same claim set, types and byte length whichever case minted the token (no positive-integer `sub`), and only the server's decode path opens the binding. **Passwordless** (no `email`, or blank): purpose `passkey_discoverable`, `allowCredentials: []` — the wire form of "offer every discoverable credential you hold for this rpId" — `userVerification: "required"`, token bound to `THROWAWAY_USER_ID = 0`; the account is resolved later, ONLY from the verified assertion, so this challenge names no account |
| `POST /auth/passkeys/login` | — | 10/min | `{challenge_token, credential}` → `200 TokenResponse` + refresh cookie. Two purposes, two account-resolution rules: **`passkey_login`** (email-first) — the account is the token's subject, SEALED at mint and opened only server-side (SEC-1: a keyless reader of the token learns nothing about which case minted it), and the credential must belong to it (a foreign credential resolves to no row); **`passkey_discoverable`** (passwordless) — the account is looked up by credential id alone (never a client-supplied address), accepted only after the signature verifies against that row's public key, and only when the assertion's `userHandle` names that row's user. Unknown credential, missing/mismatched `userHandle`, cross-account assertion, bad signature/origin/rpID, and sign-count regression all land on the uniform `401 invalid_passkey` |

`challenge_token` here is the same 5-minute signed JWT, purpose
`"passkey"` (registration), `"passkey_login"` (email-first sign-in) or
`"passkey_discoverable"` (passwordless sign-in); the `passkey_login` subject
rides SEALED inside those same claims (SEC-1 — a holder reads the purpose,
never the account), and the WebAuthn challenge itself
is embedded in the token's signed claims, so verification is stateless (no
server-side challenge store) and a challenge cannot be replayed with a
different response. `_decode_login_challenge` accepts exactly the two sign-in
purposes — a registration token (or any other purpose) posted to
`POST /auth/passkeys/login` is `401 invalid_or_expired_challenge`, so one
ceremony's token can never start another.

**Single-use challenges.** A SUCCESSFUL sign-in marks the challenge's own
digest spent until JWT expiry (`_spend_challenge`, in-process with a lock —
uvicorn runs a single worker), so a captured token + assertion pair cannot
mint a second session inside its window; the sign-count clone rule alone only
catches that on an authenticator that counts. Expired tokens were already
refused by the JWT `exp`; a spent challenge replays as
`401 invalid_or_expired_challenge`.

Errors: `401 invalid_or_expired_challenge`, `401 invalid_passkey` (assertion:
bad signature, wrong origin, wrong rpID hash, sign-count regression —
logged as a clone signal — unknown credential id, missing/mismatched
`userHandle`), `422 invalid_passkey_response` (registration:
malformed/wrong-origin credential), `400 passkey_cap` (10 live credentials),
`409 passkey_exists` (credential id already registered), `403 demo_account_restricted`
(registration/delete), `429 rate_limited`. No credential id, handle, signature
or challenge bytes are ever logged — rejections log the user id (or nothing)
and the exception class only.

`sign_count` update rule (docs/38 decision, **made explicit in M87 — see
docs/42 §3**, including the measured note that `webauthn` applies the same
clone rule inside verification first): `new > stored` → store `new`; `new == 0` (or both 0) → leave
the stored value AS IS (UV-only authenticators report 0 forever — writing it
back would erase a real baseline and disarm clone detection); `0 < new <=
stored` → reject `401 invalid_passkey` and leave the counter untouched. The
`404 no_passkeys` code is retired (M87, docs/42).

### OIDC SSO (config-gated)

| Method/Path | Auth | S | Behavior |
| --- | --- | --- | --- |
| `GET /auth/oidc/config` | — | — | `200 {enabled: bool}` — always 200 so the login page can decide without an error path |
| `GET /auth/oidc/login` | — | 5/min | `302` → provider `authorization_endpoint` with `client_id, response_type=code, scope, redirect_uri, state, nonce`; sets `oidc_state` = `state\|nonce` (httpOnly, `SameSite=Lax`, `Secure` iff `COOKIE_SECURE`, path `/api/v1/auth`, 10 min) |
| `GET /auth/oidc/callback` | — | 10/min | Validates `state` against the cookie, exchanges `code` at the token endpoint, verifies the ID token (JWKS signature, `iss`, `aud`, `exp` ±60 s, `nonce`), resolves the user by exact email match → sets `oidc_pending` (httpOnly, Lax, 10 min, path `/api/v1/auth`) and `302 {APP_ORIGIN}/login/oidc`. Any failure → `302 {APP_ORIGIN}/login/oidc?error=<code>` and the state cookie is cleared |
| `GET /auth/oidc/preview` | — | — | Reads `oidc_pending` → `200 {email, linked: bool}` (`linked` = a row already exists for `(issuer, sub)`) |
| `POST /auth/oidc/confirm` | — | 5/min | Reads `oidc_pending` → creates the `oidc_links` row if absent → `200 TokenResponse` + refresh cookie, clears `oidc_pending` |

Discovery: `GET {OIDC_ISSUER}/.well-known/openid-configuration` and the JWKS
document are fetched through `app/services/oidc.py` (httpx, module-level
cache with 1-hour TTL) — the ONLY place the auth code performs network I/O,
and it exists only while `OIDC_ENABLED=true`. All URLs come from operator
config (trusted, same posture as `LLM_BASE_URL`); no user-supplied URL is ever
fetched. ID-token algorithms accepted: `RS256`, `ES256`.

Redirect error codes (query parameter, browser navigation only — these are not
envelopes because the browser is following a 302): `oidc_state_mismatch`,
`oidc_provider_error` (discovery/token endpoint failure),
`oidc_token_invalid` (bad signature / `iss` / `aud` / `exp` / `nonce`),
`oidc_email_required` (token carries no `email` claim),
`oidc_unknown_account` (no local user with that email).

Envelope errors: `404 oidc_disabled`, `401 oidc_pending_expired` (preview or
confirm without a live `oidc_pending` cookie), `403 forbidden` (demo email),
`429 rate_limited`.

Session issuance on `confirm` is byte-for-byte the password-login path:
`_issue_pair(db, user)` + `_set_refresh_cookie(response, …)` →
`{user, access_token, token_type: "bearer"}` with `refresh_token` excluded
from the body (audit M1).

## Rate limits (all `429 {"detail": "Too many attempts", "code": "rate_limited"}`)

| Routes | Limit |
| --- | --- |
| `POST /auth/login`, `/auth/login/totp`, `/auth/login/recovery`, `/auth/passkeys/login/options`, `/auth/passkeys/login` | 10/min |
| `POST /auth/totp/setup`, `/auth/totp/confirm`, `/auth/totp/disable`, `/auth/oidc/login`, `/auth/oidc/confirm` | 5/min |
| `POST /auth/passkeys/options`, `/auth/passkeys`, `/auth/oidc/callback` | 10/min |
| `POST /users/me/change-password`, `PATCH /users/me/profile`, `DELETE /users/me` | 10/min (`CREDENTIAL_MUTATE_LIMIT`) |

The self-service credential rows were added because each one runs a full
bcrypt-cost `verify_password` against the CURRENT password. Every *other*
credential-touching route was already limited; these three were not, so a
stolen-but-undetected access token bought an unthrottled, offline-speed oracle
over the account's existing password — the same hole the login limiter exists
to close, left open on the far side of authentication. 10/min is far above what
a person actually does. Note `PATCH /users/me/profile` only reaches the
password check when `email` actually changes; a name-only patch never runs the
hash, so it is not the expensive path, but it shares the route's bucket.

Buckets are per client key + route function (`ratelimit.py` `key_style`), same
as the existing login bucket — the login-burst pattern is preserved, not
replaced. Tests opt in by flipping `limiter.enabled` exactly like
`tests/test_mcp_tokens.py`.

## Frontend

- `frontend/src/lib/webauthn.ts` — base64url ⇄ `ArrayBuffer` helpers,
  `createPasskey(options)` and `getPasskey(options)` wrappers around
  `navigator.credentials`, translating the browser's DOMException into the
  app's error style (user-cancelled → silent, other → message).
- `frontend/src/api/client.ts` — `login()` now resolves to
  `AuthResponse | LoginChallenge` (branch on `challenge`), plus
  `loginWithTotp`, `loginWithRecovery`, `passkeyLoginOptions`, `passkeyLogin`,
  `totpStatus`, `totpSetup`, `totpConfirm`, `totpDisable`, `listPasskeys`,
  `passkeyRegisterOptions`, `passkeyRegister`, `deletePasskey`,
  `oidcConfig`, `oidcPreview`, `oidcConfirm`.
- `frontend/src/auth/AuthContext.tsx` — `login` returns
  `{status: "ok"} | {status: "totp", challengeToken}`; new
  `completeTotpLogin(challengeToken, code)`, `completeRecoveryLogin`,
  `passkeyLogin(email)`, `oidcConfirm()` (each stores the access token and
  applies the user exactly like `login` does). `passkeyLogin` trims the
  address: non-blank → `passkeyLoginOptions(email)` (email-first), blank →
  `passkeyLoginOptions(undefined)` (discoverable); a dismissed authenticator
  prompt resolves `false` (no error), a server refusal rejects.
- `frontend/src/pages/Login.tsx` — password submit switches to a code step when
  the response carries a challenge: 6-digit input, "Use a recovery code" toggle,
  and a "Use a recovery code instead" hint. Adds a **Sign in with a passkey**
  button — enabled with or without a typed address (the password button is the
  peer, so passwordless must not be gated on typing an email): with an address
  it runs the email-first ceremony and a dismissed prompt stays silent (M87
  contract); with no address it starts the **discoverable** ceremony (blank
  email → `passkeyLoginOptions(undefined)`), and when nothing is offered — or
  the server refuses the assertion — it renders the fallback hint
  ("Enter your email above to sign in with a passkey for a specific account,
  or use your password.") alongside the server's detail. When
  `GET /auth/oidc/config` says enabled, a **Sign in with SSO** button
  navigates the browser to `/api/v1/auth/oidc/login` (a 302, not an XHR).
- `frontend/src/pages/OidcLogin.tsx` — route `/login/oidc`: `?error=` renders
  the mapped message; otherwise it GETs `/auth/oidc/preview` and renders the
  confirmation card ("Continue as {email}" / "Link this provider to your
  account") → `POST /auth/oidc/confirm` → app session. `oidc_pending_expired`
  renders a "sign-in link expired, try again" state.
- `frontend/src/components/settings/SecuritySection.tsx` — new Settings card
  (mounted from `Settings.tsx` with a two-line import): TOTP status + enroll
  (secret/otpauth → QR-free manual entry + confirm code → 10 recovery codes
  shown once with copy), disable (code required), passkey list + register +
  delete. Demo tenant: the card renders disabled with the reference notice,
  matching `AccountSection`.
- `frontend/src/types/index.ts` — `LoginChallenge`, `TotpStatus`,
  `PasskeyInfo`, `OidcPreview`.

## Export, backup & restore (docs/11 boundary)

The four tables are **auth plumbing, never content** — they are excluded from
every export path and preserved by every restore:

- `app/api/export.py::EXCLUDED_TABLES` carries `totp_factors`,
  `recovery_codes`, `webauthn_credentials`, `oidc_links` with justifications,
  so `GET /export/full`, the ZIP export, and the encrypted browser backup all
  omit them (the key set is registry-minus-exclusions, docs/11 step 2 — no
  second list exists to drift). `TOTP secret`, `pending_secret` and
  `code_hash` are additionally in `tests/test_export_registry.py::FORBIDDEN_COLUMNS`
  as defense in depth: if the exclusion ever broke, the payload row scan fails
  closed.
- `app/api/backup.py::RESTORE_FORBIDDEN_TABLES` — a snapshot carrying any of
  the four → `422 restore_forbidden_table`, validated before the first write
  (importing second-factor material or replaying a severed SSO link must be
  impossible, not merely skipped).
- `app/api/backup.py::RESTORE_KEEP_MODELS` — the restore wipe
  (`delete_user_rows(keep=…)`) keeps the live rows, exactly as it keeps
  `refresh_tokens`/`mcp_tokens`: a restore must not sign the user out, kill
  MCP access, or silently drop a second factor / passkey / SSO link. Since
  the tables are excluded from the snapshot, keeping the live rows is the
  only way they survive at all.
- `app/models/__init__.py` re-exports all four classes — the export registry
  resolves models from the package attributes, and an unimported model trips
  the `500 export_registry_drift` fail-closed guard for the WHOLE export.

`POST /users/delete` and demo reset still wipe them: deletion wants the
credential rows gone, and the demo tenant cannot enroll (docs/20, docs/38
demo exemption), so a demo wipe is always a no-op.

## Touch points

- `backend/app/models/auth_factors.py` — `TotpFactor`, `RecoveryCode`,
  `WebAuthnCredential`, `OidcLink`.
- `backend/alembic/versions/0031_auth_second_factor.py` — the four tables +
  constraints.
- `backend/app/services/totp.py` — RFC 6238 primitives + recovery codes.
- `backend/app/services/oidc.py` — discovery, code exchange, ID-token
  verification (the only auth-side network I/O).
- `backend/app/api/auth.py` — login returns the challenge union; shared
  `_issue_pair` unchanged.
- `backend/app/api/auth_second_factor.py` — TOTP + passkey routes.
- `backend/app/api/auth_oidc.py` — OIDC routes.
- `backend/app/schemas/auth.py` — `LoginChallenge`, `TotpSetupResponse`,
  `TotpConfirmRequest/Response`, `CodeRequest`, `PasskeyOptionsResponse`,
  `PasskeyRegisterRequest`, `PasskeyLoginRequest`, `PasskeyPublic`,
  `PasskeyPage`, `OidcConfigResponse`, `OidcPreviewResponse`.
- `backend/app/core/security.py` — `create_challenge_token`,
  `decode_challenge_token` (`type == "challenge"`, `method` claim checked).
- `backend/app/core/config.py`, `.env.example` — settings above.
- `backend/app/main.py` — mount the two new routers.
- `docs/08` — auth route table + error-code list + the stale "refresh accepts a
  body token" sentence corrected to the code's cookie-only behavior.
- `docs/02` — the four new tables.
- `docs/10` — M74 milestone entry.
- `docs/20` — pointer from the self-service account doc to this spec.
- `docs/11` — exclusion list + `restore_forbidden_table` set + restore `keep=`
  (this spec owns the "why"; docs/11 owns the export/backup procedure).
- `backend/app/models/__init__.py` — re-export the four model classes
  (export registry resolution).
- `backend/app/api/export.py::EXCLUDED_TABLES`,
  `backend/app/api/backup.py::{RESTORE_FORBIDDEN_TABLES, RESTORE_KEEP_MODELS}`.

## Verification

Behavior tests (backend, against real Postgres — `tests/test_auth_totp.py`,
`tests/test_auth_passkeys.py`, `tests/test_auth_oidc.py`):

- **RFC 6238 correctness:** `totp_code` reproduces the RFC's SHA-1 test vectors
  (secret `12345678901234567890`, times 59 / 1111111109 / 1234567890) — the
  algorithm is pinned by the spec's own vectors, not by our own implementation.
- **Enroll → login:** setup (pending, ciphertext at rest) → confirm (valid
  code) → `login` returns the challenge → `login/totp` with a fresh valid code
  → 200 pair + httpOnly cookie; wrong code, replayed code and a code from
  10 steps ago all → `401 invalid_totp_code`.
- **Recovery single-use:** `login/recovery` with code #1 → 200 pair; the same
  code again → `401 invalid_recovery_code`; code #2 still works.
- **Challenge integrity:** tampered token, expired token (crafted `exp` in the
  past), wrong purpose, and another user's token all →
  `401 invalid_or_expired_challenge`.
- **Demo exempt:** `POST /auth/demo` issues a pair with no challenge;
  `POST /auth/totp/setup` as demo → `403 demo_account_restricted`; demo login
  via `POST /auth/login` never returns a challenge.
- **Passkey register → auth:** synthetic authenticator (P-256, `fmt: none`
  attestation object built with `cbor2`) registers → credential row →
  `login/options` → assertion verifies → 200 pair; a tampered `origin` and a
  re-signed-with-wrong-key assertion → `401 invalid_passkey`; deleting the
  credential removes the login path.
- **Passwordless (discoverable) sign-in:** `login/options` with no `email`
  returns an envelope with `allowCredentials: []` and a `passkey_discoverable`
  token; a resident assertion (`userHandle` = `str(user.id)`, UP+UV flags) →
  200 pair with NO address supplied anywhere. Rejections, all uniform:
  unknown credential id, `userHandle` missing / naming another user,
  an email-first token carrying a discoverable assertion and vice versa
  (wrong purpose), a replayed (already-spent) challenge, an expired/tampered
  token, a no-UV assertion on the passwordless path (`require_user_verification`),
  and a non-increasing sign count. Email-first behaviour (M87 uniform 200,
  throwaway challenge, silent dismissal in the UI) stays pinned by the
  pre-existing tests. Frontend: `loginSecondFactor.test.tsx` pins the enabled
  button, the blank-email → `passkeyLoginOptions(undefined)` routing, the
  silent dismissal with an address typed, and the fallback-hint copy on both
  the dismissed-prompt and server-refusal paths.
- **OIDC mocked callback → session:** in-process `httpx.MockTransport`
  provider (discovery + JWKS + token endpoint, RS256 ID token signed with a
  test key) → `/auth/oidc/login` 302 carries `state`/`nonce` → `/auth/oidc/callback`
  302 sets `oidc_pending` → preview `linked:false` → confirm → 200 pair +
  refresh cookie + `oidc_links` row; second login reports `linked:true`; a
  second confirm does not duplicate the link; unknown email → `error=oidc_unknown_account`;
  mismatched state → `error=oidc_state_mismatch`; disabled → `404 oidc_disabled`.
- **MCP regression:** `tests/test_mcp_server_client.py` stays green unchanged —
  the cookie contract is the acceptance, not an add-on.
- **Rate limits:** with `limiter.enabled`, the 11th `/auth/login/totp` in a
  minute → `429 rate_limited`.
- **Export/restore boundary:** `tests/test_export_registry.py` passes with the
  four tables in `JUSTIFIED_EXCLUSIONS` (registry minus exclusions == snapshot
  keys; `secret`/`pending_secret`/`code_hash` never appear in any payload row)
  and `tests/test_backup.py` rejects each of the four in a snapshot with
  `422 restore_forbidden_table` while `test_restore_coverage_matches_the_export_registry`
  proves what restore wipes is exactly what it re-inserts (the four are kept,
  not wiped).

Gates (AGENTS §4): doc reconciliation (this file + docs/08/02/10/20) → ruff →
pytest (isolated DB) → tsc + eslint + vitest → `npm run build` → docker rebuild
→ smoke through `http://localhost:8080`.
