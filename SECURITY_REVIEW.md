# Security review — 2026-08-24

Read-only review of the working tree during active development (`portfolio.py`,
`services/valuation.py` were mid-edit; re-verify those after they land).
Nothing was changed by this review.

## High

1. **No rate limiting / lockout on auth** — `backend/app/api/auth.py:92-109`.
   `/auth/login`, `/register`, `/demo` are unthrottled → credential stuffing,
   and each login burns bcrypt cost 12 (CPU amplification DoS). Add slowapi /
   nginx `limit_req` at minimum.
2. **SSRF via user-controlled `llm_base_url`** — `backend/app/llm/base.py:47`:
   `httpx.post(f"{base_url.rstrip('/')}/chat/completions")`. Any registered
   user can PATCH `/users/me` with e.g. `http://169.254.169.254/…`,
   `http://db:5432`, or LAN admin panels and trigger it via
   `POST /suggestions/run` (background task). BYO-LLM is the design, but there
   is zero scheme/host validation (no https allowlist + private-CIDR block).
   The user's API key is also sent as Bearer to whatever URL they set.
3. **Insecure config defaults fail open** — `backend/app/core/config.py:16,27-28`:
   `jwt_secret="dev-secret-change-me"` and `seed_demo_account=True` +
   known demo password boot "fine" when `.env` is missing. Prod with a
   forgotten env var silently signs tokens with a public secret and exposes
   `/auth/demo` to anyone. Fail-fast when secret == default.
4. **ReDoS via user regex rules** — `backend/app/api/rules.py:36` validates
   compile only; `backend/app/services/rules_engine.py:19,27` runs
   `re.compile(p, IGNORECASE).search(payee)` over up to 500 transactions × N
   rules per pipeline run. `(a+)+$` hangs the worker thread. Python `re` has no
   timeout — bound pattern length/complexity or run matches under a watchdog.

## Medium

5. **CSV formula & structure injection in exports** —
   `backend/app/tax/reports.py:102-137` and
   `backend/app/services/reports.py:196-209`: user-controlled symbols/category
   names/payees joined raw (no quoting; no `=+-@\t` neutralization). A security
   named `=WEBSERVICE(...)` executes when opened in Excel; commas/newlines also
   corrupt rows.
6. **Unbounded upload** — `backend/app/api/imports.py:83`:
   `await file.read()` loads whole file into memory; no size cap, no cap on
   parsed row count. nginx's default 1 MB `client_max_body_size` accidentally
   mitigates through :8080, but compose publishes api directly on :8000,
   bypassing that.
7. **Tokens in localStorage + no CSP** — `frontend/src/api/client.ts:20-35`;
   any XSS exfiltrates a 30-day refresh token. No `Content-Security-Policy`,
   `X-Frame-Options/frame-ancestors`, `X-Content-Type-Options` on the frontend
   nginx (`frontend/Dockerfile`). Consider httpOnly cookie refresh + CSP.
8. **Plaintext `llm_api_key` at rest** — `backend/app/models/users.py:34`.
   Export/login paths strip it correctly, but DB compromise leaks every key.
9. **Auth schema gaps** — `backend/app/schemas/auth.py:7-9`: `email` is plain
   `str` (no `EmailStr`); bcrypt silently truncates passwords >72 bytes while
   128 is accepted (`core/security.py:15`). 409 on register leaks account
   existence (common tradeoff — confirm intentional).

## Low / hardening

- Compose publishes Postgres `fintrack:fintrack` on `0.0.0.0:5432` and api on
  `0.0.0.0:8000` (`docker-compose.yml:8-9,35-36`) — bind to `127.0.0.1`.
- Backend image runs as root (`backend/Dockerfile` — no `USER`).
- Defense-in-depth ownership checks missing on re-fetches:
  `categorization.py:258` (batch txn select lacks `user_id`),
  `suggestions.py:99-100,129-130` (txn/category by id without tenancy check).
  Currently unreachable cross-tenant, but one refactor away.
- Refresh rotation lacks reuse-detection alarm: presenting an already-revoked
  token just 401s (`auth.py:116`); best practice is revoking all of the user's
  sessions on reuse.
- `_owned_suggestion` returns 409 instead of 404 for foreign IDs
  (`suggestions.py:33-37`) — inconsistent with every other router (masks
  enumeration, though).
- `payee` ILIKE doesn't escape `%`/`_` (`transactions.py:87`);
  backup passphrase has no minimum length (`Settings.tsx:433`);
  `list_batches` materializes all rows to count (`imports.py:172`).

## Solid (verified, no action)

Parameterized queries throughout (no SQLi); consistent per-router tenancy
scoping incl. export; refresh tokens stored SHA-256-hashed with rotation;
HS256 pinned + `type` claim; HTML report escapes via `html.escape`
(`tax/reports.py:140+`); privacy-lock ContextVar seam correctly bound in async
dep context; client-side backup crypto is textbook-correct (PBKDF2-SHA256
600k iters, random salt/IV, AES-GCM); no `dangerouslySetInnerHTML`/`eval`;
`.env` gitignored, no secrets tracked.

## Suggested fix order

#3 (fail-fast secrets) → #1 (rate limit) → #6 + #5 (upload cap, CSV escaping)
→ #2 / #4 (need a small design decision: URL allowlist / regex guard).

## Resolution log (2026-08-25)

All findings resolved in the security-hardening wave (slices: BackendHardening, InfraHardening, FrontendHardening + integration, plus a CI follow-up adding a non-default JWT_SECRET to the workflow env — the fail-fast validator correctly rejects secret-less settings loads). Verification: backend `ruff format --check && ruff check && pytest -q` → 235 passed; frontend `tsc --noEmit && eslint --max-warnings 0 && build` clean; live smoke through nginx :8080 (evidence below).

| Finding | Resolution |
| --- | --- |
| H1 No rate limiting | **Fixed** — slowapi Limiter (in-memory, rightmost-XFF keying) in `core/ratelimit.py`; login 10/min, register/demo 5/min; 429 `{detail: "Too many attempts", code: "rate_limited"}`; `RATE_LIMIT_ENABLED` toggle read live in lifespan. Live: 10× 401 then 429. |
| H2 SSRF via llm_base_url | **Accepted by owner (BYO-LLM)** — no URL guard; the design targets the user's own Ollama on a private LAN (192.168.1.6). Revisit if multi-tenant. |
| H3 Insecure defaults | **Fixed** — `Settings` model_validator fails fast on `jwt_secret == "dev-secret-change-me"`; `.env.example` ships a placeholder; tests inject their own secret. |
| H4 ReDoS | **Fixed** — `regex` lib with per-call 0.1 s timeout (`TimeoutError` → logged non-match) in `rules_engine.py`; >256-char regex patterns → 422 `pattern_too_long`. Note: the `regex` engine optimizes some catastrophic patterns (`(a+)+$` completes instantly); `(a|aa)+$` is the pinned regression case. |
| M5 CSV injection | **Fixed** — shared `csv_safe` (`'` prefix on `= + - @ \t \r` string cells); both writers rebuilt over `csv.writer`/StringIO (also fixes comma/newline row corruption). Live: security `=SUM(A1)` exports as `'=SUM(A1)`. |
| M6 Unbounded upload | **Fixed** — >5 MB → 413 `file_too_large`; >2000 parsed rows → 413 `too_many_rows`; both before any staging. Live-verified both codes. |
| M7 Tokens in localStorage + no CSP | **Half fixed / half deferred** — CSP (`default-src 'self'; script-src 'self'` + the two sha256 hashes that allow index.html's inline theme/status-bar scripts, `frame-ancestors 'none'`, style unsafe-inline for framer-motion), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin` on every nginx response (verified on `/`, `/assets/*`); the hashes are content-addressed and `frontend/scripts/csp-hash-check.mjs` runs in the image build, so a drift between the header and `dist/index.html` fails the build instead of shipping blocked scripts; **cookie half deferred: httpOnly migration** (owner call — breaking change for the SPA auth flow). |
| M8 Plaintext llm_api_key | **Fixed** — Fernet at rest keyed `sha256(jwt_secret)` (`core/crypto.py`); encrypt on `PATCH /users/me`, decrypt at consumption (`llm/base.py`); legacy plaintext decrypts as passthrough. Caveat: rotating JWT_SECRET invalidates stored keys → users re-enter (single-user, acceptable). Export already strips the column. |
| M9 Auth schema gaps | **Fixed** — `EmailStr` on register; >72-byte passwords → 422 `password_too_long` (bcrypt truncation). 409 `duplicate_email` confirmed intentional enumeration tradeoff (comment in `auth.py`). |
| L Compose 0.0.0.0 binds | **Fixed** — db `127.0.0.1:5432`, api `127.0.0.1:8000` (web stays :8080). `ss -tlnp` verified. |
| L Root container | **Fixed** — backend image runs as `appuser` (uid 1000); `whoami` verified in-container; alembic+uvicorn healthy. |
| L Tenancy depth | **Fixed** — `categorization.py` batch txn + account selects and `suggestions.py` accept-path txn/category lookups now filter `user_id` (accept → 404 `not_found` on miss; reject keeps tolerant fallbacks). |
| L Refresh reuse | **Fixed** — presenting an already-revoked token revokes ALL the user's refresh tokens before the 401. **Behavior change**: the rotation-issued successor also dies (pinned in `test_auth.py`); docs/01, docs/02, docs/08 updated. |
| L `_owned_suggestion` 409 | **Fixed** — foreign/missing id → 404 `not_found` (was 409); status≠pending stays 409. `test_cat_suggestions_queue.py` + docs updated. |
| L ILIKE wildcards | **Fixed** — payee filter escapes `\ % _` with `escape="\\"`; literal `%`/`_` matching pinned by test. |
| L Backup passphrase | **Fixed** — `minLength={8}` on both inputs, submit disabled while shorter, inline hint (browser-verified). |
| L `list_batches` count | **Fixed** — `select(func.count())`; no row materialization. |

Notes: slowapi storage is per-process in-memory — fine for the single-container deploy; swap to Redis if uvicorn ever runs multi-worker. `regex.Timeout` does not exist in regex ≥2024 — the raised type is builtin `TimeoutError`.
