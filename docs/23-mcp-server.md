# 23 — MCP Server

Standalone Python process that exposes every fin_tracker REST endpoint as a typed MCP tool. Proxies all calls to the running FastAPI backend at `FIN_TRACKER_URL`; adds no business logic.

## Architecture

```
MCP Client (Claude Desktop, etc.)
    │
    │  Bearer <mcp_token>
    ▼
MCP Server (port 8001, streamable-http)
    │
    │  McpAuthMiddleware validates token → user_id
    │  POST /auth/internal-issue-token → backend JWT
    │
    │  Bearer <backend_jwt>
    ▼
FastAPI Backend (port 8000)
```

### Auth flow

1. **MCP token creation** — user calls `POST /auth/mcp-tokens` with a name; receives an opaque token (shown once).
2. **MCP client connects** — sends the token as `Authorization: Bearer <mcp_token>` to the MCP server's `/mcp` endpoint.
3. **McpAuthMiddleware** — intercepts the request, calls `POST /auth/validate-mcp-token` on the backend to resolve `token → user_id`.
4. **Internal issue-token** — MCP server calls `POST /auth/internal-issue-token`
   (protected by `MCP_INTERNAL_SECRET`, compared with `secrets.compare_digest`)
   to get a short-lived backend JWT for the resolved user.
5. **Tool call proxied** — every subsequent tool call carries the backend JWT; the backend enforces tenancy as usual.

Two more facts ride along with step 3, set as request-scoped contextvars by the
same middleware and read by the surface middleware below: the token's `policy`
and `is_demo` for the resolved user. Their defaults (`full` / `false`) are the
unrestricted ones, which is what keeps stdio byte-identical to its pre-M80
behaviour.

**Fail closed (M47, docs/29 F4, audit M10):** the client never falls back to
the static `FIN_TRACKER_TOKEN` / auto-login JWT, which would execute another
user's tenant:

- per-user token **issuance** fails (backend 403, network error, unset
  `MCP_INTERNAL_SECRET`) → `RuntimeError`, no request is sent;
- the issued per-user JWT is **rejected (401)** → the tool result is
  `{"error": 401, "detail": {"detail": "Backend rejected the per-user token for user_id=N; refusing to fall back to another identity", "code": "mcp_unauthorized"}}`
  — no refresh, no retry, nothing else on the wire;
- only a request that already ran as the shared token may retry it, and only
  after a successful refresh (same identity — mode 2's documented
  auto-refresh).

**Cookie state (M84):** the shared `httpx.Client` holds **no cookies**. Its
jar is a `_NoStoreCookies` (a `httpx.Cookies` whose `extract_cookies` is a
no-op) installed by `new_http_client()`, because one process-wide jar replayed
one tenant's `refresh_token` onto the next tenant's `/auth/refresh` — the F-1
cross-tenant credential leak (docs/40 §2). A response's refresh cookie is read
off that response's raw `Set-Cookie` headers and stored in the same scoped slot
as the access token (`ContextVar` under HTTP, the session's own on stdio);
`_refresh_cookie()` uses only that caller-bound token, or emits no cookie at
all.

### Files

| File | Purpose |
|------|---------|
| `backend/mcp_server/__init__.py` | Package marker |
| `backend/mcp_server/server.py` | MCPServer with all 107 tool definitions + `AgentSurfaceMiddleware` (toolset filter, refusals, receipts) |
| `backend/mcp_server/client.py` | HTTP client with auth token management; no-store cookie jar + request/session-scoped refresh slot (M84) |
| `backend/mcp_server/auth.py` | McpAuthMiddleware (ASGI middleware, default-deny on `/mcp`) + the `policy` / `token_id` / `is_demo` contextvars |
| `backend/mcp_server/policy.py` | Read-vs-write classification derived from `server.py` itself (AST), the compute-only allowlist, refusal envelopes, `tools/list` annotations |
| `backend/mcp_server/audit.py` | `args_hash`, the `response_code` derivation table, and the best-effort receipt POST to `/auth/internal-mcp-audit` |
| `backend/mcp_server/uploads.py` | Path jail for the `file_path` tools (audit H6): `MCP_UPLOAD_DIR` containment, extension allowlist, 5 MB cap |
| `backend/mcp_server/__main__.py` | Entry point: `python -m mcp_server` |

## MCP Token Management

### Create token

```
POST /api/v1/auth/mcp-tokens
Authorization: Bearer <backend_jwt>
Content-Type: application/json

{"name": "my-laptop", "policy": "read_only"}   // policy optional: "full" (default) | "read_only" | "read_propose"
```

(`mcp_tokens.created_at`/`updated_at` carry `server_default=now()` — added by
revision `0025_mcp_tokens_ts_defaults`, mirroring the 0022 tags fix.)

Response (201):
```json
{
  "id": 1,
  "name": "my-laptop",
  "token": "a1b2c3...",  // shown ONCE — save it
  "policy": "read_only",
  "created_at": "2026-08-30T12:00:00Z"
}
```

An unknown `policy` value is a `422 validation_error` — the vocabulary is the
enum, not a free-text field (M80, docs/40).

**M81 — the demo tenant.** `demo@fintracker.local` may mint and revoke its own
tokens (the two demo-write-guard exemptions, docs/20/40): an omitted `policy`
comes back as `read_only`, and an explicit policy other than `read_only` —
`"full"` **or** `"read_propose"` — is refused with
`422 demo_mcp_read_only` rather than downgraded (M189). A demo session therefore
always reaches this server as `policy=read_only, is_demo=true`, which is what
the middleware's toolset filter and `demo_account_restricted` refusal read.

### List tokens

```
GET /api/v1/auth/mcp-tokens
Authorization: Bearer <backend_jwt>
```

Response (200):
```json
{
  "items": [{"id": 1, "name": "my-laptop", "policy": "read_only", "created_at": "..."}],
  "total": 1
}
```

Note: the raw token is never returned after creation.

### Revoke token

```
DELETE /api/v1/auth/mcp-tokens/{token_id}
Authorization: Bearer <backend_jwt>
```

Response: 204 No Content.

## Agent surface: policy & receipts (M80, docs/40)

One `ServerMiddleware` (`AgentSurfaceMiddleware`, registered on `mcp.middleware`
— the SDK's context tier, so it sees `ctx.method` and the raw `ctx.params`)
owns two jobs.

**Toolset policy.** `policy.py` classifies each tool by parsing `server.py`:
the literal HTTP method of the tool's `client.request(...)` call decides it
(GET/HEAD/OPTIONS ⇒ read), minus seven allowlisted POSTs that write nothing
(`auth_login`, `auth_refresh`, `auth_logout`, `auth_demo`, `projections_run`,
`users_test_llm`, `imports_suggest_profile` — each carries its reason in the
module). Anything it cannot classify counts as a **write** (fail closed). The
result: **107 tools = 53 read + 54 write** (docs/59 adds `proposals_create`,
write-class but the one tool a `read_propose` connector may call).

- **Restricted session** = `policy != "full"` **or** `is_demo`. Its
  `tools/list` response has the write tools **removed** (not annotated), and
  every surviving tool carries `annotations.readOnlyHint: true` plus a
  `title` marker (`"<name> · read-only token"`, `"<name> · read+propose
  token"`, `"<name> · demo tenant is read-only"`, or a ` · ` join).
  Exception (M189, docs/59 §2): a non-demo `read_propose` session keeps
  **`proposals_create`** — 54 tools (53 read + that one write-class tool) —
  because it only ever persists an inert pending card; demo keeps 53 even if
  a `read_propose` policy value ever reached it.
- **Unrestricted session**: all 107 tools, annotated — read ⇒
  `readOnlyHint: true`; write ⇒ `readOnlyHint: false` with
  `destructiveHint: true` only for DELETE-method tools.
- **`tools/call` guard**: a restricted session calling a write tool gets an
  `isError` result whose text is the house envelope **before the proxy runs** —
  `{"error": 403, "detail": {"detail": "…", "code": "mcp_read_only"}}`, the
  `read_propose` tier's own `{"error": 403, …, "code": "mcp_read_propose"}`
  (wording points the agent at `proposals_create`, docs/59 §2), or the
  demo tenant's own `{"error": 403, …, "code": "demo_account_restricted"}` with
  the backend's exact wording (the guard in `app/api/deps.py` is untouched).
  No backend call, no JWT issuance.
- **Auth tools are stdio-only (M84)**: `auth_register`, `auth_login`,
  `auth_demo`, `auth_refresh`, `auth_logout` (`policy.AUTH_TOOLS`) are refused
  on the **streamable-HTTP path** with
  `{"error": 403, "detail": {"detail": "…", "code": "auth_tools_unavailable_on_mcp"}}`,
  checked before the policy guard and likewise before the proxy runs. Under
  HTTP the identity is fixed by the MCP token, so a login/refresh/logout can
  only ever act on somebody else's session -- which is exactly what the shared
  httpx cookie jar turned `auth_refresh` into (F-1, the cross-tenant credential
  leak). **stdio keeps all five** (one user, no cross-tenancy); they stay in
  `tools/list` on both transports, so the tool inventory below still lists them.
  Full rationale in docs/40 section 2.

**Receipts.** Every `tools/call` — refusals included — is recorded by posting
to `POST /auth/internal-mcp-audit` (`X-Internal-Secret`, no user JWT): one row
in `mcp_audit` with `user_id`, `mcp_token_id`, `tool`, `args_hash` (sha256 of
the canonical arguments JSON — arguments can carry a password), `response_code`
and `duration_ms` measured around the handler. The POST is **best effort**: a
failure is logged and swallowed, because an unreachable audit sink must never
change a tool's result. Read them back at `GET /api/v1/mcp-audit` (own rows,
newest first). No new environment variable: the receipt reuses
`MCP_INTERNAL_SECRET` and the client's existing HTTP transport.

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `FIN_TRACKER_URL` | No | `http://localhost:8000` | Backend API URL |
| `FIN_TRACKER_TOKEN` | No | — | Static backend JWT (mode 1) |
| `FIN_TRACKER_EMAIL` | No | — | Auto-login email (mode 2) |
| `FIN_TRACKER_PASSWORD` | No | — | Auto-login password (mode 2) |
| `MCP_INTERNAL_SECRET` | Yes (mode 3) | — | Shared secret for internal token issuance |
| `MCP_UPLOAD_DIR` | No | `/home/appuser/mcp_uploads` | Jail root for the `file_path` tools |
| `MCP_HOST` | No | `127.0.0.1` | Bind address for `--transport streamable-http` (`--host` flag overrides) |

The compose `mcp` service passes **exactly** `FIN_TRACKER_URL`,
`MCP_INTERNAL_SECRET` and `MCP_UPLOAD_DIR` — it has no `env_file: .env`, so
`JWT_SECRET`, `LLM_API_KEY`, `POSTGRES_PASSWORD` etc. never enter that
container (audit H6: they were readable via `/proc/self/environ`). Grep of
`mcp_server/` proves those three plus the mode-1/2 variables (deliberately
unset there — this service runs mode 3 only) are the only env inputs the
process reads; it never imports `app.core.config`, so no `Settings` fail-fast
applies here.

### Auth modes (mutually exclusive — enforced at startup)

1. **Static token** — set `FIN_TRACKER_TOKEN`; used on every request. Good for testing.
2. **Auto-login** — set `FIN_TRACKER_EMAIL` + `FIN_TRACKER_PASSWORD`; logs in at startup, stores tokens, auto-refreshes on 401 (same identity only).
3. **MCP token** — client sends Bearer token; middleware resolves to user; client gets backend JWT via internal endpoint. **Recommended for production.**

**Mode matrix (stdio vs HTTP):**

| Mode | stdio (`mcp run mcp_server/server.py`, local dev) | streamable-http (`python -m mcp_server --transport streamable-http`, compose) |
|------|---------------------------------------------------|-----------------------------------------------------------------------------|
| 1 static token | works — every call uses it | **unusable**: the middleware 401s any bearer that is not an MCP opaque token, and `MCP_INTERNAL_SECRET` + `FIN_TRACKER_TOKEN` together is a startup `AuthModeError` |
| 2 auto-login | works — refresh on 401 keeps the same identity | **unusable**: same two reasons as mode 1 |
| 3 MCP token | **unusable**: there is no middleware over stdio, `current_user_id` is never set, so calls carry no resolved identity (unauthenticated → 401, unless mode 1/2 supplies a token — which is what stdio is for) | works — the only supported HTTP configuration; requires `MCP_INTERNAL_SECRET`, otherwise issuance fails closed at the first call |

**Auth tools by transport (M84):** the five session-lifecycle tools
(`auth_register`, `auth_login`, `auth_demo`, `auth_refresh`, `auth_logout`)
work on **stdio in every mode** and are refused on **streamable-http in every
mode** with `{"error": 403, …, "code": "auth_tools_unavailable_on_mcp"}` —
they are a property of the transport, not of the auth mode or the token's
policy. Under HTTP the identity is already fixed by the MCP token (mode 3), so
a login there has no identity to grant and a refresh there can only spend
somebody else's session cookie. See docs/40 §2.

**Enforcement (audit M11):** `BackendClient.__init__` counts configured modes
(`MODE_ENV_VARS` in `client.py`; a mode counts if **any** of its variables is
set — a half-set auto-login pair still counts — and values are never included
in messages) and raises `AuthModeError` when more than one is configured. The
module-level singleton means this fires at process start (`python -m mcp_server`
and `mcp run` both import `mcp_server.server` → `mcp_server.client`) with a
message naming only the variable names involved. Zero modes is allowed (stdio
against unauthenticated endpoints like `health_check`).

## File-path tools (upload jail, audit H6)

Three tools take a `file_path`: `trades_import`, `imports_suggest_profile`,
`imports_upload`. They never open arbitrary caller paths — every path goes
through `mcp_server/uploads.py::open_upload`:

1. `expanduser` + `Path.resolve(strict=True)` (must exist, symlinks followed
   before judging containment);
2. the resolved path must be a **file strictly inside `MCP_UPLOAD_DIR`**
   (default `/home/appuser/mcp_uploads`; compose bind-mounts the host's
   `./mcp_uploads` there — `mkdir mcp_uploads` before `docker compose up` so
   the directory is user-owned);
3. extension allowlist per tool: `trades_import` → `.csv` (the backend parses
   investment CSV only), `imports_suggest_profile` → `.csv` (backend 422s
   non-CSV with `not_csv`), `imports_upload` → `.csv`, `.ofx`, `.qfx`
   (`detect_importer` sniffs OFX/QFX/CSV);
4. size ≤ **5 000 000 bytes** — the same cap as `app/api/imports.py` and
   `app/api/trades.py`, re-checked on the opened fd;
5. symlink-race guard — `O_NOFOLLOW` on the final component, must be a
   regular file, `(st_dev, st_ino)` + re-resolve verified after open.

Rejections raise MCP `ToolError` → `is_error` tool result whose text is the
house envelope `{"detail", "code"}` as JSON, never a traceback:

| code | when |
|------|------|
| `file_not_found` | path does not exist (or vanished mid-open) |
| `path_not_allowed` | resolves outside `MCP_UPLOAD_DIR`, or changed while opening |
| `unsupported_extension` | extension not in the tool's allowlist |
| `file_too_large` | over 5 000 000 bytes |
| `not_a_regular_file` | directory / special file |
| `invalid_path` | unresolvable, ELOOP, EACCES |
| `upload_dir_unavailable` | `MCP_UPLOAD_DIR` missing/non-absolute/uncreatable |

**UX:** copy the statement export into `MCP_UPLOAD_DIR` (compose: the host's
`./mcp_uploads`), then call the tool with that in-jail path. Inline/base64
content was rejected as the design: a 5 MB CSV cannot travel through the
model's tool-call payload, while `file_path` + jail keeps the documented
"upload a file" flow intact and confines the server-side filesystem.

## Transport & exposed paths

- **Bind (audit L19):** `python -m mcp_server --transport streamable-http`
  binds `127.0.0.1` by default; override with `--host <addr>` or
  `MCP_HOST=<addr>` (flag wins). It never binds `0.0.0.0` implicitly; compose
  additionally publishes it as `127.0.0.1:8001`. Compose passes
  `--host 0.0.0.0` (M80) so nginx can reach it across the compose network —
  the published host port stays loopback-bound either way, and the auth
  middleware stays default-deny.
- **Public path (M80):** the web proxy adds `location /mcp/`, which rewrites
  `^/mcp/(.*)$ → /$1` and forwards to the MCP container, so the public
  streamable-http URL is **`https://<host>/mcp/mcp`** while the container keeps
  serving exactly `/mcp`. The upstream is written as a VARIABLE with
  `resolver 127.0.0.11 valid=10s` (Docker's DNS) rather than a literal host:
  compose gives the `mcp` container a new IP every time it is recreated, and a
  literal would be resolved ONCE when nginx starts — stale until someone
  restarts `web`, and a startup failure if the name is unknown. Verified
  locally against nginx 1.26 with the exact generated config (`nginx -t`
  clean, `/mcp/mcp` → upstream `/mcp`). Buffering is off for the
  `text/event-stream` responses; auth there is still the bearer token, never
  the proxy.
- **Host header / DNS rebinding (M80):** the proxy forwards the client's real
  `Host`, which for the public path is the deployment domain — never loopback.
  mcp's transport auto-enables a rebinding check with a loopback-only
  allowlist (`127.0.0.1:*`, `localhost:*`, `[::1]:*`), which answers **421
  Misdirected Request** to every proxied request and cannot be told about an
  arbitrary domain, so `create_auth_app()` passes
  `TransportSecuritySettings(enable_dns_rebinding_protection=False)`
  explicitly and keeps `host` honest for the transport. The boundary is the
  token instead: `McpAuthMiddleware` is default-deny and `validate-mcp-token`
  authenticates every request including `initialize`, there is no cookie or
  other ambient credential for a browser to replay cross-origin, and no CORS
  headers are emitted for `/mcp`. A regression test pins the behavior:
  `test_seam_accepts_the_public_host_the_proxy_sends`.
- **Default-deny middleware (audit L20):** `McpAuthMiddleware` serves exactly
  `MCP_ENDPOINT_PATH = "/mcp"` — the one route
  `mcp.streamable_http_app()` registers (SDK default
  `streamable_http_path="/mcp"`, no custom/OAuth/SSE routes are configured
  without an auth server provider) — and returns
  `{"detail": "Not found", "code": "not_found"}` **404** for every other
  path, so a route added later is never default-reachable.

## Docker

```yaml
mcp:
  build: ./backend
  # No env_file: JWT_SECRET / LLM_* / COOKIE_* never enter this container —
  # the process reads only the three vars below.
  environment:
    FIN_TRACKER_URL: http://api:8000
    MCP_INTERNAL_SECRET: ${MCP_INTERNAL_SECRET:-}
    MCP_UPLOAD_DIR: /home/appuser/mcp_uploads
  volumes:
    # Upload jail: drop a .csv/.ofx/.qfx here on the host.
    - ./mcp_uploads:/home/appuser/mcp_uploads
  depends_on:
    api:
      condition: service_healthy
  ports:
    - "127.0.0.1:8001:8001"
  # --host 0.0.0.0: the web proxy reaches this over the compose network (M80);
  # the published host port below stays loopback-bound either way.
  command: >-
    python -m mcp_server --transport streamable-http --host 0.0.0.0 --port 8001
  restart: unless-stopped
```

## Tool Inventory (107 tools)

| Router | Count | Tools |
|--------|-------|-------|
| Auth | 6 | health_check, auth_register, auth_login, auth_demo, auth_refresh, auth_logout |
| Users | 6 | users_me, users_update_me, users_change_password, users_update_profile, users_delete_account, users_test_llm |
| Categories | 5 | categories_list, categories_create, categories_get, categories_update, categories_delete |
| Institutions | 5 | institutions_list, institutions_create, institutions_get, institutions_update, institutions_delete |
| Accounts | 6 | accounts_list, accounts_create, accounts_get, accounts_update, accounts_delete, accounts_ledger |
| Transactions | 10 | transactions_list, transactions_create, transactions_update, transactions_delete, transactions_similar, transactions_transfer_suggestions, transactions_accept_transfer, transactions_detect_transfer, transactions_link_transfer, transactions_unlink_transfer |
| Securities | 12 | securities_list, securities_create, securities_get, securities_update, securities_delete, securities_lookup, securities_overview, securities_fundamentals, securities_holdings_get, securities_holdings_update, securities_holdings_import, securities_refresh |
| Trades | 10 | trades_list, trades_create, trades_get, trades_update, trades_delete, trades_dividend_suggestions, trades_record_dividend, trades_import, trades_import_commit, trades_import_discard |
| Prices | 5 | prices_list, prices_upsert, prices_get, prices_update, prices_delete |
| FX | 3 | fx_get, fx_board, fx_create |
| Portfolio | 4 | portfolio_positions, portfolio_exposure, portfolio_breakdown, portfolio_refresh |
| Reports | 8 | reports_spending, reports_spending_categories, reports_cashflow, reports_net_worth, reports_performance, reports_capital_gains, reports_dividends, reports_category_tag_matrix |
| Projections | 2 | projections_run, projections_defaults |
| ACB | 2 | acb_list, acb_rebuild |
| Imports | 8 | imports_suggest_profile, imports_upload, imports_list_batches, imports_get_batch, imports_get_batch_rows, imports_commit_batch, imports_discard_batch, imports_revert_batch |
| Export | 1 | export_full |
| Search | 1 | search_global |
| Watchlist | 3 | watchlist_list, watchlist_add, watchlist_remove |
| Suggestions | 4 | suggestions_run, suggestions_list, suggestions_accept, suggestions_reject |
| Rules | 5 | rules_list, rules_create, rules_get, rules_update, rules_delete |
| Assistant | 1 | proposals_create |

SSE endpoint `securities/stream/prices` excluded — doesn't map to MCP request/response model.

The same table drives the policy split (M80, extended M189): 53 read /
54 write. The count is
asserted by `tests/test_mcp_agent_surface.py::test_every_tool_is_classified`,
which fails when a tool is added without the classification following it.

## MCP Client Configuration

### Claude Desktop

```json
{
  "mcpServers": {
    "fin-tracker": {
      "url": "http://your-server:8001/mcp",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer <mcp-token>"
      }
    }
  }
}
```

### Stdio (local development)

```bash
cd backend && mcp run mcp_server/server.py
```

No auth middleware in stdio mode — the MCP client process runs locally, so
configure mode 1 or mode 2 (see the mode matrix above); mode 3 cannot resolve
without the middleware.

## Verification

1. Start backend: `docker compose up -d api`
2. Run migration: `cd backend && uv run alembic upgrade head`
3. Create MCP token: `POST /auth/mcp-tokens` with a valid backend JWT
4. Start MCP server: `docker compose up -d mcp`
5. Connect MCP client with the token in the Authorization header
6. Call `health_check` — verify JSON response matches `GET /api/v1/health`
7. Call `accounts_list` directly → verify JSON matches `GET /api/v1/accounts`
   (identity comes from the MCP token). `auth_demo` over this HTTP transport
   is now **refused**: `isError` with
   `{"error": 403, …, "code": "auth_tools_unavailable_on_mcp"}` and no API
   request on the wire (M84) — over stdio it still returns tokens.
8. Call `reports_spending` with `from_date`/`to_date` → verify report data
9. Call `securities_lookup symbol="AAPL"` → verify yfinance result
10. Jail: put a CSV in `./mcp_uploads`, call `trades_import` with its in-jail
    path → preview; call it with `/etc/passwd` (or any path outside the jail)
    → `is_error` result with `{"code": "path_not_allowed"}` and no traceback
11. Default-deny: `curl -i http://127.0.0.1:8001/anything` → 404
    `{"code": "not_found"}`; `curl -i http://127.0.0.1:8001/mcp` → 401
    missing-bearer challenge (endpoint served)
12. **M80 read-only:** mint a token with `{"policy": "read_only"}`, connect,
    `tools/list` → 53 tools, none mutating, each `title` ends in
    `· read-only token`; `tools/call accounts_create` → `isError` with
    `{"error": 403, …, "code": "mcp_read_only"}` and no API request on the wire;
    `tools/call proposals_create` → same refusal (a read token cannot propose)
13. **M80 receipts:** the run in step 12 leaves one `mcp_audit` row per call
    (refusals included) — `GET /api/v1/mcp-audit` shows `response_code: 403`
    for the refused call; a second account sees none of those rows
14. **M80 public path:** `curl -i https://<host>/mcp/mcp` reaches the MCP
    container (401 challenge without a bearer), and
    `/api/v1/auth/internal-mcp-audit` through the same proxy → 403 at nginx
12. Mode exclusivity: `FIN_TRACKER_TOKEN=x MCP_INTERNAL_SECRET=y python -m mcp_server`
    → starts and exits with `AuthModeError` naming both variables
13. Unit tests: `uv run pytest -q tests/test_mcp_jail.py tests/test_mcp_server_client.py tests/test_mcp_server_middleware.py`

15. **M189 BYO-AI connector (docs/59):** mint a token with
    `{"policy": "read_propose"}` → `tools/list` shows **54** tools (53 read +
    `proposals_create`, the only `proposals_*` name on any scope);
    `tools/call proposals_create` → mode-3 proxy to
    `POST /api/v1/assistant/proposals`, which lands a **pending** card and
    leaves the target row untouched (receipt 200); `tools/call accounts_create`
    → `isError` with `{"error": 403, …, "code": "mcp_read_propose"}` and no
    API request on the wire; there is no apply/dismiss/undo tool to confirm
    the card from this side — confirmation is the web app (docs/57 §4);
    `DELETE /auth/mcp-tokens/{id}` → the next call is `401 invalid_mcp_token`.
    Pinned by `tests/test_byo_ai_connector.py` + `tests/test_mcp_agent_surface.py`.
