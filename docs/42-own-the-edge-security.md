# 42 — Own-the-edge security (M87)

## Context

Mark's posture decision (PM + the M83 review): **the app owns its own edge
security.** The Pangolin PIN in front of `princessandmark.com` must NOT be
assumed — fin_tracker will launch on a VPS with no edge gate, so every M83
finding that the PIN had been masking is live attack surface:

| Finding | Was | Now |
|---|---|---|
| M83 F-4/F-5 — HSTS too weak, no Permissions-Policy / COOP / CORP | P3 | P1 (S5) |
| M83 F-7 — account-existence oracles (`409 duplicate_email`, `404 no_passkeys`) | P2 | P1 (S1) |
| M83 F-6 — passkey sign-counter reset (clone check disarmed) | P3 | P1 (S2) |
| M83 F-8 — `CORS_ORIGINS` still the dev value on the deployed box | P3 | P1 (S4) |
| reviewer's `/docs` + `/openapi.json` reachability probe | — | new (S3) |

Spec-first was waived by the card (pure backend/nginx), but everything below
is pinned so a later card cannot drift it: exact bodies, exact status codes,
exact headers, and the tests that hold them.

## Approach

### 1 — S1a: `POST /auth/register` answers the same thing always

**Decision (the card offered `always 409` or `always 200`; we take `200`):**
registration is a *request*, not a *result*, so it always answers
`200` + `RegisterAccepted`, and the account **is created** when the address was
free. The card's `409-always` alternative would need an email-verification
recovery flow to be usable, and this install must run with **no SMTP at all**
(`docs/37` — SMTP is optional and may be unset), so there is nothing to send
and no flow to defer to. With no verification email, "sign in with the
password you just chose" is the only recovery path that works offline.

```
POST /api/v1/auth/register          200  (was 201 / 409)
{
  "status": "accepted",             // Literal["accepted"]
  "detail": "Registration accepted: if this address was free, an account now
             exists with the password you just chose. Sign in to continue."
}
```

Contract:

- **Byte-identical** across branches — same status, same body. This is the
  whole point, so a test asserts `free.json() == taken.json()`, not just that
  the shapes match.
- **Free address** → `users` row + the 10 `DEFAULT_CATEGORIES` are committed
  (unchanged behaviour), **but no session**: no `access_token`, no
  `refresh_token`, no `Set-Cookie`. A session on one branch and an error on
  the other is the oracle wearing a different status code.
- **Taken address** → no writes, and `_dummy_bcrypt(payload.password)` spends
  the same one bcrypt verify the fresh branch spends in `hash_password`, so
  the branches do not separate by timing either.
- **Branch-independent failures are untouched**: `422 validation_error`
  (malformed email), `422 password_too_long` (>72 bytes), `429 rate_limited`
  (5/minute). All three fire identically whatever the address is, so they
  leak nothing.
- `409 duplicate_email` **remains valid** on `PATCH /users/me/profile`
  (docs/20) — that path is authenticated and the caller already knows their
  own address.

Recovery: the caller signs in (`POST /auth/login`) with the password just
posted. Wrong account? `401 invalid_credentials`, exactly what login already
answers.

### 2 — S1b: `POST /auth/passkeys/login/options` never says "no such account"

**Was** `404 {"code": "no_passkeys"}` for both "unknown email" and "known but
keyless", `200` otherwise — a two-value oracle that survived on the *login*
side after register was the only other path.

**Now**: `200` + `PasskeyOptionsResponse` for **all three** cases — account
with passkeys, account without, no account:

```jsonc
{ "challenge_token": "<JWT-shaped purpose-bound token>",
  "options": { "challenge": "...", "rpId": "...",
               "allowCredentials": [ { "id": "<b64u>", "type": "public-key" } ],
               "userVerification": "preferred" } }
```

- **Account with passkeys** → the real credentials, real token (the binding
  itself unchanged — only now sealed, see SEC-1 below).
- **Keyless (either flavour)** → a server-generated throwaway: a random
  32-byte descriptor (so `allowCredentials` is *present and same-shaped* —
  the library omits an empty list, which would separate the bodies by key
  set) and a challenge token whose subject is `THROWAWAY_USER_ID = 0`.
  `users.id` is a serial starting at 1, so `db.get(User, 0)` resolves nothing.
- **Where the cases are uniform:** the status channel (200, 200, 200 — the
  retired `404 no_passkeys` split WAS the status-code oracle) and the token
  (SEC-1 below). They part company at the ceremony: `POST /auth/passkeys/login`
  answers `401 invalid_or_expired_challenge` (throwaway token) or
  `401 invalid_passkey` (fabricated assertion for a credential that does not
  exist).
- **Known residual AT the responder (documented, accepted — t_a199b88f):**
  the `allowCredentials[0].id` VALUE still discloses whether an address has
  ≥1 registered passkey, so the responder is NOT a uniform channel. Real
  ids are read from the DB — stable across requests, length following the
  stored credential (a 16-byte credential → 22 base64url chars) — while both
  keyless flavours get a fresh `secrets.token_bytes(32)` throwaway per
  request (43 chars, **unstable**): one request separates enrolled from
  keyless by id length, two by stability. Why kept: email-first
  non-discoverable login must name the address's real credential ids, and
  an empty `allowCredentials` (discoverable-only) would strand non-resident
  credentials — a product-gated switch, explicitly out of scope here (PM,
  t_a199b88f). Scope of the leak: it confirms only the passkey-holding
  subset (⇒ the address has an account); a keyless account and a ghost are
  indistinguishable from each other, and no credential material and no auth
  bypass are exposed. Pinned as documented by
  `test_passkey_login_options_id_channel_reveals_enrollment_as_documented` —
  closing the channel flips that test and this note must move in the same
  change.
- **SEC-1: the token payload does not separate the cases either.**
  `challenge_token` is RS256-signed, NOT encrypted — a holder decodes its
  claims without the key, so a clear `sub` said "this address has an account
  with a passkey" in one request, no ceremony. The `passkey_login` subject is
  now sealed with the at-rest key at a fixed width
  (`security.py::BLINDED_SUBJECT_METHODS`): a keyless reader sees the same
  claim set, the same value types and the same byte length for all three
  cases — no positive-integer `sub` — and the binding opens only inside the
  signature-verified decode path. Pinned by
  `test_email_first_challenge_token_subject_is_blinded`.
- The `404 no_passkeys` code is retired from this route; `10/minute` rate
  limit unchanged.
- **Known UX cost** (accepted): a browser ceremony that cannot succeed now
  fails with `NotAllowedError` → `getPasskey` returns `null` → the sign-in
  button stops silently instead of showing "no passkey registered for this
  email". Silence leaks nothing; the password path is unaffected.

### 3 — S2: a `0` sign count is never adopted

WebAuthn §6.1.3: `signCount == 0` means *"this authenticator does not
maintain a signature counter"*.

**What the code does now:**

```python
if new_count and new_count <= row.sign_count:      # unchanged: refuse + log
    raise api_error(401, "Passkey authentication failed", "invalid_passkey")
if new_count:                                      # NEW: 0 never overwrites
    row.sign_count = new_count
row.last_used_at = datetime.now(UTC)               # stamped either way
```

**Honest reading of M83 F-6 (measured, not assumed).** The old line
(`row.sign_count = new_count` unconditionally) could only have erased a real
counter if a `0` ever *reached* it — and with the pinned dependency it does
not: `webauthn==3.0.1` applies the clone rule inside
`verify_authentication_response` itself (`(new > 0 or stored > 0) and new <=
stored` ⇒ raise), so an assertion carrying `0` against a stored `7` is
rejected *before* the app's bookkeeping, and the only reachable `0 → 0` write
changes nothing. The card's premise — that one non-counting sign-in disarmed
clone detection — is therefore not reproducible through this code path today.

The rule is still worth having and is now explicit, for three reasons: it is
what docs/38 had been documenting (code and spec disagreed), it makes the
invariant local instead of depending on a dependency's internals, and it keeps
working if the verification seam is ever swapped. The test pins the
**observable** contract, which holds under either layer:

- `test_zero_sign_count_never_erases_a_stored_counter` — 7 lands → `0`
  refused (`401 invalid_passkey`) and the stored value stays **7** → `6`
  refused and still **7** → `8` accepted and becomes **8** (the rule does not
  latch shut);
- `test_a_never_counting_authenticator_stays_usable` — a credential that has
  always reported `0` keeps signing in, and stores `0` over `0`.

No live curl probe exists for S2 (it needs a real authenticator); the two
tests above are its evidence.

### 4 — S3: the API map is authenticated-only

**FastAPI-side gate, not an nginx rule** — chosen because it holds at the
origin as well as behind the edge (an nginx-only rule disappears the moment
something else fronts the app, exactly the mistake this card is fixing).

- `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` — the framework's
  open routes are gone.
- Re-registered as `GET /docs`, `GET /redoc`, `GET /openapi.json`, each with
  `Depends(require_docs_access)`.
- `require_docs_access` (`app/api/deps.py`):
  1. `settings.api_docs_public` → allow (local dev; `.env.example` sets it,
     production leaves it unset → **fails closed**);
  2. `Authorization: Bearer <session access token>` → allow (same JWT
     `get_current_user` decodes; a decode failure is caught and falls
     through — never a 500);
  3. `Authorization: Bearer <MCP token>` → hash-lookup allow if the row
     exists and is unexpired (same rule `POST /auth/validate-mcp-token`
     applies);
  4. otherwise `401 {"detail": "Authentication required to read the API
     documentation", "code": "docs_auth_required"}`.
- **nginx** (`frontend/Dockerfile`): `location ~ ^/(docs|redoc|openapi\.json)$`
  proxies to `api:8000` *before* the SPA fallback — without it all three
  paths returned `index.html` with a 200, and the gate would be invisible
  through the edge. The location declares **no `add_header`**, so it inherits
  the server-level hardening including the CSP.
- **Known consequence** (accepted, documented): the inherited CSP has no CDN
  in `script-src`, so Swagger UI's JS/CSS from `cdn.jsdelivr.net` is blocked
  *at the edge* — the page is served but does not execute there.
  `/openapi.json` is fully usable through the edge (JSON is not affected), and
  the interactive UI is for local dev (`API_DOCS_PUBLIC=true`, no CSP on the
  uvicorn port) or the API port itself. Widening `script-src` to fit Swagger
  would weaken CSP for the whole origin; not worth it.

### 5 — S4: production `CORS_ORIGINS`

The deployed `.env` on example.internal still carried the dev value, so the site's own
origin was not on the allow-list while `http://localhost:5173` was. Fixed
**in place on the box** (a `.env` is never committed) and `.env.example` now
documents the prod shape and why it matters:

```
CORS_ORIGINS=https://fintrac.princessandmark.com
```

### 5b — Host-header allowlist (`ALLOWED_HOSTS`, opt-in)

`CORSMiddleware` was the only middleware in `app/main.py`, so a client that can
reach the listener directly picks its own `Host` header — and per the LAN
exposure note above there is no host firewall or `DOCKER-USER` source
restriction.

`TrustedHostMiddleware` is now mounted **only when `ALLOWED_HOSTS` is
non-empty**, parsed by `Settings.allowed_host_list` (the same
comma-separated-string convention as `CORS_ORIGINS`, documented in
`.env.example`). The default is empty, deliberately: a wrong allowlist makes
Starlette answer **400 to every request**, the proxied healthcheck included, so
enabling it implicitly would be a worse outage than the gap it closes. An
upgrade therefore cannot break an existing deployment.

This is defence-in-depth rather than a closed vulnerability — no route builds a
URL from the `Host` header; the only Host-adjacent consumers are the
`app_origin`-derived redirect URIs, which come from settings. Tests:
`backend/tests/test_trusted_host.py`.

### 6 — S5: response-header hardening (nginx, `frontend/Dockerfile`)

| Header | Value | Was |
|---|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains; preload` | `max-age=31536000` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=()` | absent |
| `Cross-Origin-Opener-Policy` | `same-origin` | absent |
| `Cross-Origin-Resource-Policy` | `same-origin` | absent |

Emitted `always` at **server level** and repeated inside
`location = /index.html` and `location /assets/` — nginx's `add_header` is
location-scoped, so those two blocks would otherwise drop every inherited
header (they already carried CSP; they now carry the rest too). Additive
only: nothing was removed or narrowed, so smoke stays green.

**The `includeSubDomains` cost, stated plainly:** HSTS now binds *every*
subdomain of this site family to HTTPS. A future HTTP-only sibling subdomain
on this host breaks until it serves TLS (or its own `max-age=0`).
The http→https redirect itself still comes from the TLS terminator, outside
this repo.

## Touch points

- `backend/app/api/auth.py` — `register`, `RegisterAccepted`, `_dummy_bcrypt`
  reuse; `REGISTER_ACCEPTED_DETAIL`
- `backend/app/schemas/auth.py` — `RegisterAccepted`
- `backend/app/api/auth_second_factor.py` — `passkey_login_options`,
  `THROWAWAY_USER_ID`, sign-count adoption in `passkey_login`
- `backend/app/api/deps.py` — `require_docs_access`
- `backend/app/core/config.py` — `api_docs_public`
- `backend/app/main.py` — `docs_url=None` + the three gated routes
- `frontend/Dockerfile` — nginx header blocks + docs location
- `.env.example` — `API_DOCS_PUBLIC`, prod `CORS_ORIGINS` shape
- `frontend/src/pages/Register.tsx`, `frontend/src/auth/AuthContext.tsx`,
  `frontend/src/api/client.ts`, `frontend/src/types/index.ts` — register
  renders "accepted → sign in", no session stored
- Tests: `test_auth.py` (responder consistency), `test_auth_passkeys.py`
  (uniform status/shape responder + pinned id-channel residual, zero-count
  rule), `test_api_docs_gate.py`
  (the gate, all three credential states), plus the register/login split in
  every suite that used `register` as a sign-in
- Deployed (not committed): example.internal `.env` `CORS_ORIGINS`

## Verification

- `uv run pytest -q` green; the three new/pivotal tests:
  - `test_register_responder_is_consistent_for_free_and_taken_addresses`
    — `free.json() == taken.json()`, both 200, no cookie, first password
    still signs in, second password does not.
  - `test_passkey_login_options_responder_shape_is_uniform` — 200/200/200,
    equal envelope shapes, throwaway ids never equal the real credential id,
    and a throwaway challenge only ever produces a 401; plus
    `test_passkey_login_options_id_channel_reveals_enrollment_as_documented`
    — pins the accepted responder residual (§2): real id stable across
    requests with the stored credential's length, throwaways a fresh 43-char
    value per request, keyless-vs-ghost indistinguishable.
  - `test_zero_sign_count_never_erases_a_stored_counter` — 7 → 0 keeps 7 →
    6 refused → still 7 (red against the pre-fix code).
  - `test_api_docs_gate.py` — 401 + envelope for anonymous and for a garbage
    bearer (never 500), 200 for a session token and for a live MCP token,
    401 again once that token is expired, 200 when `api_docs_public` is on.
- **Live, through `http://localhost:8080` / the deployed origin:**
  - pre/post curl evidence for register (201/409 → 200/200 identical bodies),
    passkey options (404/200 → 200/200), `/docs` + `/openapi.json`
    (200 open → 401 envelope; 200 with a token), header scan before/after,
    CORS preflight from the real origin (allowed) and from `localhost:5173`
    (not allowed).
  - `scripts/smoke.sh` still 11/11 after the rebuild.
  - M84 acceptance re-run: `docs/assets/m84/jar_leak_probe.py` must still
    return `REFUSED` — the leak fix is independent of this card and must not
    regress.

**Run against the deployed stack after `docker compose up -d --build` —
2026-10-02, all green.**

- `bash docs/assets/m87/probe.sh post_fix` → transcript at
  `docs/assets/m87/post_fix_evidence.txt`: register 200/200 with byte-identical
  bodies, passkey options 200/200 (same envelope, random descriptors),
  `/docs` and `/openapi.json` 401 `docs_auth_required` anonymous / 200 with a
  session token, HSTS with `includeSubDomains; preload`, Permissions-Policy,
  COOP and CORP present, preflight 200 from the real origin and 400 from
  `localhost:5173`, cleanup 204.
- `bash docs/assets/m87/probe2.sh` (appended to the same transcript): a LIVE
  MCP token reads `/openapi.json` (200) and `/docs` (200 HTML); a garbage
  bearer is 401 — never 500; through uvicorn directly `:8000/docs`,
  `/redoc` and `/openapi.json` are all 401 with the envelope.
- `bash scripts/smoke.sh` → 11/11 (step 3 now registers 200 with **no**
  `Set-Cookie`, then signs in for its throwaway token; the login burst hits
  429 on attempt 9, ≤ 11).
- `docs/assets/m84/jar_leak_probe.py` → `RESULT: REFUSED` (M84 still holds).
- Browser check of the register flow: `/register` → "Registration accepted" →
  "Sign in" → login → Dashboard for that account; screenshot at
  `docs/assets/m87/ui_register_accepted.png`; test account deleted (204).
