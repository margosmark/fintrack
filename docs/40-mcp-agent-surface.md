# 40 — MCP agent surface (M80): token policy, tool filtering, write audit, executed quickstart

## Context

fin_tracker already ships an MCP server (`backend/mcp_server/`, docs/23): 107 typed
tools proxying 1:1 to the REST API, streamable-http + stdio, opaque `mcp_tokens`
auth (M24). The competitive research (`/home/hermes/wave5-research/wave5-competitive-research.md`)
shows the churn vector has moved from app→app to app→DIY-agent: users cancel
paid trackers to prompt a bot into rebuilding them. The differentiator we can
claim that a DIY stack cannot is **money safety at the agent seam**:

1. a *scoped* credential — a read-only MCP token whose toolset does not even
   contain the mutations, so a confused agent physically cannot spend/write;
2. a *receipt* — every tool call lands in an `mcp_audit` row, so "the bot did
   it" is answerable with a query instead of a vibe.

This milestone is spec-first (AGENTS §7): this doc pins every shape before the
code exists.

## Approach

Four deliverables, all inside the **existing** MCP server + FastAPI backend
(no new service, no new secret, no new auth path — the only auth is still
`mcp_tokens`):

1. **Per-token policy** — `mcp_tokens.policy` ∈ {`full`, `read_only`, `read_propose`}; minted at
   creation, returned by list/validate, revocable like any other token.
2. **Toolset filtering + annotations** — one MCP `ServerMiddleware` rewrites
   `tools/list` for restricted sessions and refuses restricted `tools/call`s.
3. **Write audit** — `mcp_audit` rows written by the MCP server through the
   existing `MCP_INTERNAL_SECRET` seam, read back through a first-party route.
4. **Docs that teach** — an executed-and-verified quickstart at
   `/help/agent-integration`, an audit/config guide at `/help/configuration`,
   static twins, and the home-page section notes.

---

## 1. Token policy

### Table change (`mcp_tokens`)

| column | type | notes |
|--------|------|-------|
| `policy` | `VARCHAR(16)` NOT NULL, `DEFAULT 'full'` | `sa.Enum(McpTokenPolicy, native_enum=False)` → CHECK constraint, values `full` \| `read_only` \| `read_propose` (AGENTS §2 enum rule: no table rewrite). The third value arrived with M189 (docs/59): `0060_mcp_read_propose` swaps `ck_mcp_tokens_policy` — drop + re-add, no table rewrite — and `read_propose` still fits the `VARCHAR(16)` column |

`McpTokenPolicy(StrEnum)` lives in `app/models/mcp_tokens.py`; migration
`0034_mcp_policy_audit` adds the column (server default `full`, so existing
rows keep working) **and** creates `mcp_audit` (§3). Alembic revision ids are
≤32 chars (AGENTS §6).

Policy semantics — enforced only at the MCP seam (§2), never by a new backend
rule:

- `full` — today's behaviour, unchanged.
- `read_only` — the connected client sees **only read tools**; a call to a
  write tool is refused by the MCP server with a 403 envelope *before* any
  bytes reach the backend.
- `read_propose` (M189, docs/59) — the BYO-AI connector tier: the read tools
  **plus exactly one write-class tool**, `proposals_create`, which persists an
  inert pending proposal card (no domain row moves until the user confirms it
  in the web app, docs/57 §4). Every other write tool is refused with
  `403 mcp_read_propose` before any bytes reach the backend; `proposals_create`
  itself is still refused for `read_only` (`mcp_read_only`) and always for the
  demo tenant (`demo_account_restricted`).

### API shapes (docs/08)

```
POST /api/v1/auth/mcp-tokens
Authorization: Bearer <backend jwt>
{"name": "laptop-agent", "policy": "read_only"}  # policy optional: "full" (default) | "read_only" | "read_propose"
```

> **`mcp_server/client.py::BackendClient.request()` owns the `Authorization`
> header.** A caller-supplied `headers=` dict (today: only the auth tools'
> refresh cookie) is merged FIRST and the resolved token's `Authorization` is
> set AFTER the merge, with an explicit `headers.pop("Authorization", None)`
> as belt-and-braces. Merging the other way round let a caller-supplied key win
> and silently re-point a call at another tenant's context. `request()` is the
> single choke point every MCP tool call passes through, so the invariant is
> enforced there rather than trusted to each call site.
→ **201**
```json
{"id": 7, "name": "laptop-agent", "token": "<hex64>",
 "policy": "read_only", "created_at": "...", "expires_at": "...", "last_used_at": null}
```
- unknown policy value → **422** `validation_error` (Pydantic `Literal`, the
  existing `validation_error` handler — no new error code).
- **Demo tenant (M81).** The shared demo write guard exempts exactly this route
  and `DELETE /api/v1/auth/mcp-tokens/{id}`, so the demo tenant can mint and
  revoke its own `mcp_tokens` rows — nothing else in the guard moves
  (`DEMO_MUTATION_EXEMPT_PATHS` / `DEMO_MUTATION_EXEMPT_PREFIXES`,
  `app/api/deps.py`). The mint is **policy-forced**:
  - `policy` omitted → `read_only` (not `full`);
  - any explicit policy other than `read_only` — `{"policy": "full"}` **or**
    `{"policy": "read_propose"}` → **422** `demo_mcp_read_only`
    (`{"detail": "The demo tenant can only mint read-only MCP tokens", "code":
    "demo_mcp_read_only"}`) — refused rather than silently downgraded, so the
    answer never depends on whether the client sent the field.
  The request schema therefore types `policy` as `McpTokenPolicy | None` with
  `None` = "not sent"; the handler resolves it (`app/api/auth.py::create_mcp_token`).
- `GET /api/v1/auth/mcp-tokens` items gain `policy`
  (`McpTokenPublic.policy`); `DELETE /api/v1/auth/mcp-tokens/{id}` unchanged —
  revocation works identically for both policies **and** for the demo tenant
  (it is one of the two demo-guard exemptions above).

### validate response (consumed by the MCP middleware)

```
POST /api/v1/auth/validate-mcp-token   →  {user_id, email, policy, token_id, is_demo}
```

`is_demo` is `user.email == DEMO_EMAIL` — the same predicate
`app/api/deps.py` uses for the demo write guard, resolved server-side so the
MCP process never guesses identity from an address. `token_id` is the
`mcp_tokens.id` (for the audit row); it is **not** a credential.

---

## 2. Toolset filtering and annotations

### Where

A single `ServerMiddleware` (`mcp/server/context.py` protocol:
`async def __call__(ctx, call_next)`) registered on `mcp.middleware` in
`backend/mcp_server/server.py`. It observes every inbound request; this
milestone only acts on `tools/list` and `tools/call`. Verified against the
installed SDK (mcp 1.x): `tools/list` reaches the middleware as a
`dict {"tools": [...]}` and a filtered dict returned from the middleware is
what the client receives; `tools/call` params arrive as
`{"name", "arguments", "_meta"}`.

Request-scoped identity stays in `contextvars` (same mechanism as
`current_user_id`, docs/23): `McpAuthMiddleware.dispatch` reads `policy`,
`token_id` and `is_demo` from the validate response and sets
`current_mcp_policy` / `current_mcp_token_id` / `current_is_demo`, resetting
them in its existing `finally`. Defaults for stdio (`full`, `None`, `False`)
keep today's stdio behaviour byte-identical.

### Read vs write classification (`backend/mcp_server/policy.py`)

Single source of truth = the tool code itself. `policy.py` parses
`mcp_server/server.py` with `ast` and reads the literal HTTP method of each
`@mcp.tool()`'s `client.request("GET" | "POST" | ...)` call:

- **read tool** — method `GET`/`HEAD`/`OPTIONS`, **or** a POST on the
  documented compute/session allowlist below (these write no tenant data);
- **write tool** — everything else, and any tool that cannot be classified
  (fail closed: unknown ⇒ write).

`COMPUTE_ONLY_TOOLS` (each pinned with its reason in `policy.py`):

| tool | why it writes nothing |
|------|----------------------|
| `auth_login`, `auth_refresh`, `auth_logout`, `auth_demo` | session lifecycle; under mode 3 the identity comes from the MCP token anyway |
| `projections_run` | pure computation over posted inputs |
| `users_test_llm` | connectivity probe |
| `imports_suggest_profile` | parses an uploaded CSV, returns a suggestion |

`auth_register` is deliberately **not** on it (it creates a user). Current
counts: **107 tools = 53 read + 54 write** (46 GET tools plus the 7
allowlisted POSTs; the other 54 proxy a mutating method). `proposals_create`
(M189, docs/59) is write-class — it persists a row — but is the one tool a
`read_propose` session may still call. A test fails if any tool is
unclassified or if an allowlist entry stops existing, so the table cannot go
stale.

### `tools/list` rewrite

Restriction = `policy != "full"` **or** `is_demo` (both at once ⇒ both filters
apply). Since M81 the demo tenant can only mint `read_only` tokens (§1), so in
practice demo sees the read-only toolset — but the `is_demo` branch stays
independent of what its token says: it is what picks the app's own
`demo_account_restricted` refusal wording and the `demo tenant is read-only`
marker, and (M189) the filter exemption for `proposals_create` additionally
requires `not is_demo`, so the seam never leans on the mint guard for its
toolset.

For a **restricted** session:

- every write tool is **removed** from the list (not merely annotated) — a
  read-only token / the demo tenant does not see `transactions_create`;
  the one exemption (M189, docs/59 §2): a `read_propose` **non-demo** session
  keeps `proposals_create` — 54 tools = 53 read + that one write — while
  `read_only` and demo sessions still drop it (53);
- each surviving tool gets `annotations`:
  `{"readOnlyHint": true}` plus a visible
  `title` marker: `"<name> · read-only token"`, `"<name> · read+propose token"`,
  `"<name> · demo tenant is read-only"`, or
  `"<name> · read-only token · demo tenant is read-only"` (combinations join
  with ` · `).

For an **unrestricted** session every tool keeps its place and still carries
correct annotations: read → `{"readOnlyHint": true}`; write →
`{"readOnlyHint": false, "destructiveHint": true}` for DELETE-method tools,
`{"readOnlyHint": false, "destructiveHint": false}` otherwise.

`destructiveHint` is emitted **only for write tools** (`policy.py::tool_annotations`
adds it inside the `if write:` branch): a read tool cannot destroy anything, so
the wire never carries `destructiveHint: false` next to `readOnlyHint: true`
(M81 — earlier drafts of this sentence claimed the pair on reads; the pinned
test is `test_marker_and_annotations`, which asserts
`tool_annotations("accounts_list", None) == {"readOnlyHint": true}`).

### `tools/call` guard

Before `call_next`, if the session is restricted and the named tool is a
write tool, the middleware returns an `isError` tool result whose text is

```json
{"error": 403, "detail": {"detail": "<reason>", "code": "mcp_read_only"}}
```

with `<reason>` naming the tool (`"This MCP token is read-only; accounts_create
would modify data. Mint a full token to write."`). The envelope is
three-branched since M189 (docs/59 §2), evaluated in this order — demo first,
then the tier, always before the proxy runs:

| session calls | `code` | note |
|---|---|---|
| any demo session | `demo_account_restricted` | backend wording verbatim |
| `read_only` → any write tool (incl. `proposals_create`) | `mcp_read_only` | existing wording |
| `read_propose` → a write tool outside `PROPOSE_TOOLS` | `mcp_read_propose` | suggests `proposals_create`, or minting `full` |
| `read_propose` → `proposals_create` | — (allowed) | proxies; receipt recorded like any write |

For the demo tenant the
code is the **existing** backend code, unchanged:
`{"error": 403, "detail": {"detail": "The demo account is read-only; changes are not allowed", "code": "demo_account_restricted"}}` —
`app/api/deps.py::_assert_demo_mutation_allowed` and its QA-validated rules are
untouched; this is an expansion of *visibility*, not a change to the guard.
No request leaves the process, so there is no path around it.

### Session-lifecycle tools are stdio-only (M84)

`policy.AUTH_TOOLS` = `{auth_register, auth_login, auth_demo, auth_refresh,
auth_logout}`. On the **streamable-HTTP path** — any `tools/call` served while
`auth.in_mcp_request` is set — each of them is refused with the house envelope,
checked **before** the policy guard (so a read-only token sees the transport
answer, not `mcp_read_only`; both are 403 and neither reaches the backend):

```json
{"error": 403, "detail": {"detail": "Auth tools are not available over the MCP HTTP transport: the session identity is already fixed by your MCP token, so a login/refresh/logout here could only act on another tenant's session. Use the web app for session management.", "code": "auth_tools_unavailable_on_mcp"}}
```

**403, not 422** — this is a refusal at the MCP seam, exactly like
`mcp_read_only` and `demo_account_restricted`, and it keeps `mcp_audit`'s
`CODE_REFUSED = 403` row ("refused at the MCP seam") honest. The receipt is
recorded like any other refusal (`docs/40 §3`). The tools stay **visible** in
`tools/list` (107 tools, unchanged counts) — they are refused on call, not
hidden, so the tool inventory in `docs/23` stays byte-identical.

**stdio is unchanged**: `in_mcp_request` defaults to `False` and is only set by
`McpAuthMiddleware`, which does not exist on the stdio transport. There, one
long-lived session serves one user, there is no cross-tenancy, and
`auth_login` → next tool call → `auth_refresh` keeps working (docs/23 mode
matrix, modes 1/2).

**Why this closes F-1 (the cross-tenant credential leak).** `BackendClient` is
one module-level `httpx.Client` shared by every tenant. httpx extracts each
response's `Set-Cookie` into that client's jar and replays it on any later
request whose path matches the cookie's `Path` — so `/auth/refresh` sent
whichever tenant had logged in last. Reviewer's live repro: tenant B's MCP
token called `auth_refresh` and got an access token with `sub=124` (tenant A).

Three changes, all in `backend/mcp_server/client.py`, kill it:

1. **The jar cannot store.** `_NoStoreCookies(httpx.Cookies)` overrides
   `extract_cookies` to a no-op, installed after `Client.__init__` (which
   re-wraps whatever it is handed in a plain `httpx.Cookies`) via
   `new_http_client()`. The shared jar is therefore *always* empty — tests
   build their mock-transport client through the same helper so the suite runs
   against the real jar policy.
2. **The refresh cookie is read off the response, never the jar.**
   `_absorb_refresh_cookie()` parses this response's raw `Set-Cookie` headers
   (`refresh_cookie_from_response()`) into the caller's **scoped**
   `refresh_token` slot: a `ContextVar` while `in_mcp_request` is set (dead
   when the request ends), the session's own slot on stdio. Raw headers rather
   than `resp.cookies`, because an explicit clear (logout's empty
   `Max-Age=0`) is dropped by the cookiejar as expired and "no token left"
   must be distinguishable from "no cookie in this response".
3. **`_refresh_cookie()` consults only the caller-bound token** — the explicit
   argument, else the caller's own scoped slot — and otherwise omits the header
   entirely (the API then answers 401). The jar preference
   (`cookie_refresh_token() or refresh_token`) is hard-deleted; the method it
   called no longer exists.

Net: identity and cookie state are both request-scoped under HTTP and
session-scoped on stdio, and there is no process-wide credential store left to
leak.

---

## 3. Write-path audit (`mcp_audit`)

### Table (docs/02)

| column | type | notes |
|--------|------|-------|
| `id` | `PK` | |
| `user_id` | `INT NOT NULL`, FK `users.id`, indexed | a deleted account leaves no audit rows (joins `_USER_OWNED_MODELS`, docs/20/30 cascade) |
| `mcp_token_id` | `INT NULL`, **no FK** | deliberately unowned: revoking a token must not delete its receipts, and a NULL means "not minted from a token" (stdio/session) |
| `tool` | `VARCHAR(100) NOT NULL` | MCP tool name |
| `args_hash` | `VARCHAR(64) NOT NULL` | sha256 hex of `json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)` — arguments can hold passwords, so only a hash is stored |
| `response_code` | `INT NOT NULL` | see the derivation table below |
| `duration_ms` | `INT NOT NULL` | wall time of the whole tool call, measured in the middleware |
| `created_at` | `timestamptz NOT NULL`, `server_default=now()` | |

Index: `ix_mcp_audit_user_created (user_id, created_at DESC)`.

`response_code` derivation (pinned, deterministic):

| value | when |
|-------|------|
| `403` | refused by the MCP seam (read-only / read+propose policy, demo guard) |
| backend status | the tool result JSON carries a top-level `"error": <int>` — exactly what `BackendClient.request` produces for ≥400 |
| `400` | `isError` result whose JSON is the house envelope `{"detail","code"}` (an input rejected at the MCP seam, e.g. the file jail) |
| `500` | any other `isError` result (unexpected exception) |
| `200` | otherwise |

### Write path

The MCP server posts one row per `tools/call` to a new **internal** endpoint —
internal because the MCP process has no database access by design (docs/23
"three env vars"):

```
POST /api/v1/auth/internal-mcp-audit
X-Internal-Secret: <MCP_INTERNAL_SECRET>
{"user_id": 12, "mcp_token_id": 7, "tool": "accounts_create",
 "args_hash": "<64 hex>", "response_code": 403, "duration_ms": 3}
```
→ **204**. Errors: `403 forbidden` (missing/wrong secret, `compare_digest`),
`404 not_found` (unknown user), `422 validation_error`, `429 rate_limited`
(**120/minute**, same bucket size as its two internal siblings; the bucket is
per route function, so this does not tighten `internal-issue-token`).

Recording is **best effort**: a failed audit POST is logged at WARNING and the
tool result is still returned (an unreachable audit sink must never break a
user's tool call). Every call in the reference run below lands a row because
the backend that just served the call is the same one receiving it.

`mcp_server/audit.py::record_audit(...)` is the only writer; it posts with
`X-Internal-Secret` and **no** Authorization header (it is not a user-scoped
call), and does nothing at all when there is no resolved MCP user (stdio,
modes 1/2) or no secret.

### Read path

```
GET /api/v1/mcp-audit?limit=50&offset=0      # bearer = the account's own rows
```
→ **200**
```json
{"items": [{"id": 3, "mcp_token_id": 7, "tool": "accounts_create",
            "args_hash": "…", "response_code": 403, "duration_ms": 3,
            "created_at": "…"}],
 "total": 12}
```
Newest first. Tenancy: `user_id == current_user.id` on every query (AGENTS
§2); no cross-user read exists. `mcp_audit` is user data with no secrets, so
the model registry exports it automatically (docs/11 step 2 — no
`EXCLUDED_TABLES` entry) and backup restore carries it; the demo reset wipes it
with the rest of the tenant (`_USER_OWNED_MODELS`).

---

## 4. Docs & positioning

- **`/help/agent-integration`** (`frontend/src/pages/helpGuides.ts` + static
  twin `site/guides/agent-integration.html`): the worked recipe — mint a
  read-only token over the real `POST /auth/mcp-tokens` flow, connect with a
  copy-paste `mcpServers` config (streamable-http URL **and** stdio), list
  tools, hit the 403, read the receipt. M189 (docs/59) added the BYO-AI
  steps: the `read_propose` recipe, the confirmation-gate explanation, and
  the pinned egress copy ("Your data stays between you and your AI"). Every command in it is executed against
  the running stack first; the executed transcript is committed under
  `docs/assets/m80/`.
- **`/help/configuration`** (+ twin `site/guides/configuration.html`): the
  reference page for tokens, policies, the audit trail, and how to inspect
  `mcp_audit` (`GET /mcp-audit`, plus the psql one-liner for self-hosters).
- **Home page section notes**: `site/index.html` gained an "Drive it from your
  AI agent" block linking both guides, and its guide cards gained the two new
  entries (M80 scope). The t_fe44260e landing rebuild removed the agents block
  and the guide-card grid from `index.html`; both guides remain at
  `site/guides/{agent-integration,configuration}.html` (and `/help` in-app),
  unlinked from the landing page. `docs/32` § Layout is the current structure
  of record.
- No marketing-site migration, no billing, no OAuth (out of scope).

---

## Touch points

| path | change |
|------|--------|
| `backend/app/models/mcp_tokens.py` | `McpTokenPolicy` enum + `policy` column |
| `backend/app/models/mcp_audit.py` | new model |
| `backend/app/models/__init__.py` | export `McpAudit` |
| `backend/alembic/versions/0034_mcp_policy_audit.py` | column + table + index |
| `backend/app/schemas/mcp_tokens.py` | `policy` on create/public/created; M81: `policy: McpTokenPolicy \| None` so "omitted" is distinguishable from an explicit `full` |
| `backend/app/api/auth.py` | policy on mint/list, validate response fields, `POST /auth/internal-mcp-audit`; M81: demo mint forces `read_only` (422 `demo_mcp_read_only` for explicit `full`) |
| `backend/app/api/deps.py` | M81: the demo write guard's 4th/5th exemptions — MCP token mint (exact path) + revoke (path prefix) |
| `backend/app/api/mcp_audit.py` + `main.py` | `GET /mcp-audit` |
| `backend/app/api/users.py` | `McpAudit` in `_USER_OWNED_MODELS` (cascade + demo reset) |
| `backend/mcp_server/auth.py` | three contextvars from the validate response |
| `backend/mcp_server/policy.py` | AST classification, allowlist, refusal envelopes |
| `backend/mcp_server/audit.py` | best-effort internal POST |
| `backend/mcp_server/server.py` | register the middleware; filter/annotate/refuse/record |
| `frontend/Dockerfile` | `deny all` for the new internal route (M9 rule) + `location /mcp/` → the mcp container (public `…/mcp/mcp`) |
| `docker-compose.yml` | `web` now also `depends_on: mcp` |
| `frontend/src/pages/helpGuides.ts`, `site/guides/*.html`, `site/index.html` | guides + home notes |
| `backend/tests/test_auth_security_hardening.py` | M81: exemption-set pins + the demo mint/refuse/revoke contract |
| `backend/app/models/mcp_tokens.py` | M189: `McpTokenPolicy.read_propose` + widened `ck_mcp_tokens_policy` string |
| `backend/alembic/versions/0060_mcp_read_propose.py` | M189: drop + re-add the CHECK (chains off `0059_assistant`) |
| `backend/app/api/auth.py` | M189: demo mint guard refuses any explicit policy ≠ `read_only` (`full` **or** `read_propose`) |
| `backend/mcp_server/policy.py` | M189: `PROPOSE_TOOLS`, `MARKER_READ_PROPOSE`, three-branch `refusal_envelope` |
| `backend/mcp_server/server.py` | M189: `proposals_create` tool; filter exemption `read_propose and not is_demo` |
| `backend/app/api/assistant.py` + `backend/app/services/assistant.py` | M189: `POST /assistant/proposals` + `propose()` (docs/59 §3.1; extends the docs/57 router/service) |
| `backend/tests/test_mcp_tokens.py`, `backend/tests/test_mcp_agent_surface.py`, `backend/tests/test_byo_ai_connector.py` | M189 AC tests: scope enforcement, propose → apply → undo, audit, revoke |
| `docs/59-byo-ai-connector.md` | M189 spec (this doc's `read_propose` tier is its §1-2) |
| `docs/40` (this), `docs/23`, `docs/02`, `docs/08`, `docs/10` (M80), `docs/11`, `docs/32`, `docs/01` | reconciliation in the same commit |

**Hard invariants respected:** no second auth path (still `mcp_tokens` only);
`MCP_INTERNAL_SECRET` is reused, never duplicated; the demo write guard in
`deps.py` keeps its method-scoped boundary — M81 adds exactly two exemptions
for the demo tenant's OWN credential rows (mint, revoke), enumerated in
`DEMO_MUTATION_EXEMPT_PATHS`/`_PREFIXES` and pinned by
`test_demo_write_exemptions_are_enumerated_never_inherited`, while every other
demo mutation still answers 403 `demo_account_restricted`; no new
service/container; `mcp_server/` still reads
only `FIN_TRACKER_URL`, `MCP_INTERNAL_SECRET`, `MCP_UPLOAD_DIR` plus the
deliberately-unset mode 1/2 vars (docs/23 env table needs no new row).

## Verification

1. **Classification** — `tests/test_mcp_agent_surface.py::test_every_tool_is_classified`:
   107 tools, 53 read / 54 write, no unclassified tool, allowlist entries all
   exist.
2. **Token round trip** (API) — mint `policy="read_only"` → 201 echoes it →
   `GET /mcp-tokens` shows it → `POST /validate-mcp-token` returns
   `{policy: "read_only", token_id, is_demo}` → unknown policy → 422. Note
   the validate call is **internal**: it is called by the MCP container on the
   compose network, and nginx answers `403` to it on the public origin (M9
   rule, docs/08) — so a public-origin check after revoking goes through
   `/mcp/mcp` instead, which answers `401`.
3. **MCP round trip** (in-process, `TestClient(create_auth_app())` + raw
   JSON-RPC over streamable-http): restricted `tools/list` contains no write
   tool, every tool carries `readOnlyHint` + the marker `title`;
   `tools/call transactions_create` → `isError` with
   `{"error": 403, …, "code": "mcp_read_only"}` and **no** backend call; an
   unrestricted session lists all 107.
4. **Audit** — the same run records one payload per call (asserted against the
   captured recorder), and `POST /auth/internal-mcp-audit` → `GET /mcp-audit`
   round-trips a row end to end; wrong secret → 403; other tenant's rows are
   invisible.
5. **Executed quickstart** — the literal `/help/agent-integration` recipe run
   against the deployed stack (compose `api`+`mcp`+`web`+`site` on example.internal,
   through the public proxy on `:8080`, not a direct container call): mint
   read-only token → real MCP client `initialize` + `tools/list` (53 tools,
   none of them mutating, every one annotated) → `health_check` → 200 →
   `accounts_create` → 403 `mcp_read_only` envelope → `GET /mcp-audit` →
   exactly two rows, `{200, 403}` → revoke → a fresh `/mcp/mcp` request with
   the dead token → 401 → account deleted. Transcript committed to
   `docs/assets/m80/quickstart-transcript.txt` with the script that produced
   it (`docs/assets/m80/quickstart.py`); guide-page screenshots to
   `docs/assets/m80/help-agent-integration.png` (the recipe as rendered),
   `docs/assets/m80/help-configuration.png` (the audit/config reference) and
   `docs/assets/m80/help-index.png` (the guide index carrying both entries).
6. **Gates** — AGENTS §4 sequence (ruff format/check, pytest vs Postgres,
   `tsc --noEmit`, eslint, `npm run build`, docker rebuild, smoke through
   `http://localhost:8080`, secret scan, push). The generated nginx config is
   `nginx -t`-validated against the real nginx binary before the image is
   built (a broken `web` image takes the whole site down, not just this
   feature), and `/mcp/mcp` is proven to reach the upstream as `/mcp`.
7. **Demo MCP surface (M81)** — API: demo session JWT → `POST
   /auth/mcp-tokens {"name": "demo-agent"}` (no `policy`) → **201** with
   `policy: "read_only"`; the same call with `{"policy": "full"}` → **422**
   `demo_mcp_read_only`; `DELETE /auth/mcp-tokens/{id}` → **204**;
   `POST /validate-mcp-token` on the minted token → `{policy: "read_only",
   is_demo: true}`; a demo `POST /accounts` is still **403**
   `demo_account_restricted` (nothing else in the guard moved). Wire, through
   the public proxy: demo login → mint → `POST /mcp/mcp` `initialize` +
   `tools/list` → **53** tools with every title ending in
   `· read-only token · demo tenant is read-only`; `tools/call
   transactions_create` → `isError` carrying
   `{"error": 403, …, "code": "demo_account_restricted"}`; `tools/call
   health_check` → 200. Pinned by
   `tests/test_auth_security_hardening.py::test_demo_mcp_token_routes_are_the_only_new_exemption`
   and `test_demo_write_exemptions_are_enumerated_never_inherited`.
8. **M84 cross-tenant credential leak (F-1)** —
   `tests/test_mcp_server_client.py`: `test_shared_jar_never_stores_a_response_cookie`
   (a login's `Set-Cookie` never reaches the shared jar and is never replayed),
   `test_refresh_cookie_comes_from_the_caller_not_the_jar` (tenant A's token
   planted in the jar: tenant B's refresh still sends B's own token, and with
   no token of its own the header is omitted entirely),
   `test_refresh_cookie_is_read_off_the_response_not_the_jar` (rotation
   absorbs the successor off the response; the next rotation spends it, jar
   still empty), `test_logout_clear_cookie_empties_the_slot`,
   `test_refresh_cookie_never_lands_in_request_logs`. In
   `tests/test_mcp_agent_surface.py`: `test_auth_tools_are_refused_on_the_http_path`
   (all five refused, `backend_calls == []`, one 403 receipt each),
   `test_auth_tool_refusal_is_transport_scoped_not_policy_scoped`,
   `test_auth_tools_still_work_outside_an_http_request` (stdio unchanged, login
   lands in the session slot, jar still empty). Live: the reviewer's own
   `jar_leak_probe.py` re-run against the deployed stack — pre-fix verdict
   `CROSS-TENANT TOKEN LEAK (sub=124)`; post-fix verdict `REFUSED` (exit 3,
   every auth tool answered the 403 envelope, no `access_token` anywhere in
   the transcript). Both transcripts are committed:
   `docs/assets/m84/jar-leak-probe.txt` (the probe, verdict switched) and
   `docs/assets/m84/mcp-refusal-smoke.txt` (auth_refresh receipt
   `response_code=403, duration_ms=0` next to `accounts_list` 200 on the same
   token, plus `scripts/smoke.sh` 11/11).

9. **M189 BYO-AI connector (docs/59)** — scope enforcement: a `read_only`
   session calling `proposals_create` → `403 mcp_read_only`, `backend_calls ==
   []`, one 403 receipt, and the tool absent from its 53-tool `tools/list`; a
   `read_propose` session lists **54** tools (53 read + `proposals_create`,
   the only `proposals_*` name on any scope), proxies `proposals_create`
   through mode 3 (`internal-issue-token` → `POST /api/v1/assistant/proposals`,
   receipt 200), and refuses any other write with `403 mcp_read_propose`
   before the proxy; demo + a `read_propose` policy value still lists 53.
   REST (no LLM anywhere on this path): `POST /assistant/proposals` → **201**
   pending card with the target byte-identical and zero audit rows; `apply`
   (the user's own session) mutates + writes `assistant_audit` in the same
   transaction; `undo` restores byte-for-byte; every refusal
   (`validation_error`, `assistant_proposal_invalid`, `invalid_token`,
   `demo_account_restricted`) writes nothing; revoke → next validate is `401
   invalid_mcp_token`. Pinned by `tests/test_byo_ai_connector.py` (18),
   `tests/test_mcp_agent_surface.py` and `tests/test_mcp_tokens.py`.
