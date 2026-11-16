# 59 — Bring-your-own-AI connector (ChatGPT / Claude / Codex)

Kilter's BYO-AI adoption: the user plugs **their own AI app** into their
fintrack data. The external assistant reads through the MCP agent surface
(docs/40, docs/23) and **proposes** changes; proposals land as the pending
cards of the plain-English assistant (docs/57) and mutate data only after the
user confirms in the web app — with the same `assistant_audit` + undo
machinery every other proposal uses.

Milestone **M189** · task `t_bb0033d2` (epic `t_cc17bc90`).

Acceptance criteria (card): token scope enforcement tests (a read token
cannot write); the proposal path requires confirmation before mutation; audit
log entries for every external read/write; a revoke test; local gates green.

## 1. The connector token — three scopes

The token is still an MCP token (docs/40 §1): minted per connector, shown
once, 90-day expiry, revocable. The policy vocabulary gains one value so a
connector can be given **exactly** read + propose and nothing else.

| `policy` | sees on `tools/list` | direct domain write | propose | mintable by |
|---|---|---|---|---|
| `read_only` | 53 read tools | refused | refused | every tenant incl. demo |
| `read_propose` (new) | 53 read tools + `proposals_create` (54) | refused | allowed | every tenant **except demo** |
| `full` | all 107 tools | allowed | allowed | every tenant except demo |

- **Mint** — `POST /api/v1/auth/mcp-tokens` `{"name": "chatgpt", "policy": "read_propose"}`
  → `201 {id, name, token, policy, created_at, expires_at, last_used_at}`;
  `token` is the only time the raw value is returned. Codes unchanged:
  `400 mcp_token_cap` (10 active per user), `422 validation_error`.
  Demo tenant: only `read_only` is mintable — explicit `full` **or**
  `read_propose` → `422 demo_mcp_read_only` (the guard becomes
  "anything but `read_only` is refused", never a silent downgrade).
- **Validate** — `POST /api/v1/auth/validate-mcp-token` (Bearer = raw token)
  → `200 {user_id, email, policy, token_id, is_demo}`; `401 missing_token` /
  `invalid_mcp_token` / `mcp_token_expired`; `429 rate_limited`. The MCP
  server filters the toolset from this response — identity and scope resolve
  server-side, never in the client (docs/40 §2).
- **Revoke** — `DELETE /api/v1/auth/mcp-tokens/{id}` → `204`, **hard delete**
  (`404 not_found` for a missing/foreign id). The next validation of that
  token is `401 invalid_mcp_token` (the row is gone — not `mcp_token_expired`),
  so the connector's session dies at its next call. No soft-revived row.
- Storage: `mcp_tokens.policy` gains `read_propose` in the model enum and the
  `ck_mcp_tokens_policy` CHECK constraint (migration `0060_mcp_read_propose`,
  drop + re-add — a constraint swap, no table rewrite; `read_propose` fits the
  existing `varchar(16)` column).

Scope enforcement stays **seam-only**, exactly like `read_only` today
(docs/40 §1): the backend keeps its ordinary authorization; the MCP process
refuses before the call leaves it.

## 2. Scope enforcement at the seam (`mcp_server/policy.py`)

- `PROPOSE_TOOLS = frozenset({"proposals_create"})` — the write class stays
  write (`is_write_tool("proposals_create") is True`: it is a `POST` that
  persists a row; it is **not** added to `COMPUTE_ONLY_TOOLS`, whose pinned
  invariant is "writes nothing").
- `MARKER_READ_PROPOSE = "read+propose token"` — `restriction_marker()` emits
  it for `policy == "read_propose"` (and still `read-only token` for anything
  else that is not `full`, fail-closed on unknown values; the demo marker
  appends as today). Every tool a restricted session sees carries the marker
  in `annotations.title`.
- Toolset filter (`server.py::_filtered_list`): a `read_propose` session keeps
  its write-class tool `proposals_create` in the list; every other write tool
  is dropped for it, as `read_only` drops everything.
- `refusal_envelope()` becomes three-branched (all `403`, all before the proxy
  runs — no backend call, no JWT issued, receipt recorded):

  | session calls | envelope `code` | detail wording |
  |---|---|---|
  | any session that is demo | `demo_account_restricted` | backend wording verbatim (docs/40 §2) |
  | `read_only` calls anything write-class (incl. `proposals_create`) | `mcp_read_only` | existing wording ("Mint a full token … to write") |
  | `read_propose` calls a write tool **outside** `PROPOSE_TOOLS` | `mcp_read_propose` (new) | "This MCP token can query and propose, but {name} would modify data directly. Propose the change instead (proposals_create), or mint a full token (POST /auth/mcp-tokens with policy=\"full\") to write." |
  | `read_propose` calls `proposals_create` | — (allowed) | — |

- Session-lifecycle (`AUTH_TOOLS`) refusals unchanged
  (`auth_tools_unavailable_on_mcp`).
- Annotations: `proposals_create` is `readOnlyHint: false`,
  `destructiveHint: false` (a non-destructive write, like `accounts_create`).
  Read-only sessions never see it, so their "every listed tool is read-only"
  pin keeps holding.

## 3. The propose path

### 3.1 REST — `POST /api/v1/assistant/proposals`

The external AI has already done the reasoning, so this route runs the
**docs/57 validation registry with no LLM call**: same `proposal_kind` /
`proposal_target` / `proposal_summary` / `proposal_fields` grammar, same
`_validate_change` refusal codes, same pending row. It lives beside
`/assistant/ask` on `app/api/assistant.py` and shares its router/auth
(`401 invalid_token` when the bearer is missing/invalid).

Request (`extra="forbid"` — an unknown field is `422 validation_error`):

```json
{
  "proposal_kind": "transaction.update",
  "proposal_target": "txn:123",
  "proposal_summary": "Reassign the Loblaws run to groceries",
  "proposal_fields": {"category_id": "7"},
  "question": "Categorize my Loblaws run",
  "answer": "Transaction #123 at Loblaws is groceries — no category set yet."
}
```

- `proposal_kind`: **plain `str`** in the schema — the registry, not Pydantic,
  owns the vocabulary, so an unknown kind is one code family
  (`422 assistant_proposal_invalid`), the same as an LLM that hallucinated a
  kind (docs/57 §5). The value `"none"` is refused here too: a connector only
  calls this tool when it has a concrete change.
- `proposal_summary` / `proposal_fields`: same rules as docs/57 (trimmed,
  non-empty, field keys ⊆ `ALLOWED_FIELDS[kind]`).
- `question` and `answer` each `1..2000` chars — what the user asked
  **their** AI, and the AI's explanation of the change. (`answer` was an open
  `non-empty` until security F2, t_108c24f3 — an unbounded field on a row
  that is never deleted.) Both are stored on the card (the columns are
  `NOT NULL`), shown in History like any other proposal.

Response: `201` + `ProposalPublic` (docs/57 §3) with `status: "pending"`.

Errors: `422 validation_error` (shape: missing/overlong field, extra key),
`422 assistant_proposal_invalid` (registry: unknown kind/target/field, empty
`proposal_fields`, a no-op change where `after == before`, kind `"none"`),
`429 rate_limited` (the route's own `12/minute` human-paced ceiling, docs/57
§3b — security F3), `401 invalid_token`. **Nothing about the target row is written** — the only
row this call creates is the proposal itself.

### 3.2 MCP tool — `proposals_create`

```python
@mcp.tool()
def proposals_create(proposal_kind: str, proposal_target: str,
                     proposal_summary: str, proposal_fields: dict,
                     question: str, answer: str) -> dict:
    """Propose a change to your data. Lands as a pending card; nothing is
    modified until you confirm it in the fintrack web app."""
    return client.request("POST", "/api/v1/assistant/proposals", json={...})
```

Proxy shape (mode 3): `POST /auth/internal-issue-token` → per-user JWT →
`POST /api/v1/assistant/proposals`. Success receipt: `200` (docs/40 §3 derives
the code from the tool result, not the backend's `201`); backend `≥400`
receipts carry the backend status.

Tool counts after this change (tests are the pinned source; docs/40 and
docs/23 are reconciled in the same wave — they were still saying 105 from
before M124):

| session | listed tools |
|---|---|
| `full` | **107** = 53 read + 54 write |
| `read_only` | 53 (all read) |
| `read_propose` | 54 (53 read + `proposals_create`) |
| demo | 53 (read_only toolset + demo marker) |

### 3.3 Confirmation gate — the MCP surface has no apply

The **only** assistant tool on the MCP surface is `proposals_create`.
There is deliberately no `proposals_apply` / `proposals_dismiss` /
`proposals_undo` / `proposals_history` tool: an external session cannot
confirm its own proposal, in any policy tier — `tools/list` is pinned to
contain no other `proposals_*` name on every scope.

Confirmation is the user's own web session (docs/57 §4):

- `POST /api/v1/assistant/proposals/{id}/apply` → re-reads the target
  (`409 assistant_proposal_stale` if it moved), stages the domain write **plus**
  the `assistant_audit` row (before/after) and commits once → `200 ProposalPublic`,
  `status: "applied"`.
- `POST /api/v1/assistant/proposals/{id}/undo` → restores `before`
  (`409 assistant_undo_conflict`), audited the same way → `status: "undone"`.
- `404 assistant_proposal_not_found` for missing/foreign ids.

That is the whole AC: propose (connector) → pending → apply (user) → audit →
undo, with the target untouched between propose and apply.

## 4. Audit — every external read and write

- **`mcp_audit`** (docs/40 §3): the seam records a receipt for **every**
  `tools/call` on the HTTP path — reads (`200`), writes (`200`), seam
  refusals (`403`), backend failures (backend status), unexpected (`500`) —
  with `{user_id, mcp_token_id, tool, arguments_hash, status, duration_ms}`.
  `arguments_hash` is canonical and value-hiding; the receipt proves *that*
  and *when* a connector touched something, never the data itself. Read
  receipts are what pins the "every external **read**" half of the AC.
- **`assistant_audit`** (docs/57 §6): apply/undo of a connector-proposed card
  audit exactly like an asked-proposal — before/after captured, `undo` audited
  too.
- Correlation: connector proposals carry the `mcp_token_id` of the proposing
  token in `mcp_audit`; join with the proposal row by user + time. (A `source`
  column on `assistant_proposals` is a non-goal — see §6.)

## 5. Egress — stated plainly in the UI

- **This feature performs no server-side egress.** The external app connects
  to fintrack's MCP endpoint; fintrack answers on that connection. The only
  party that takes the user's data off-machine is the AI provider the user
  chose (OpenAI, Anthropic, …) — because that is who the user asked to read
  it.
- Where fintrack itself does make outbound calls (the docs/57 assistant's LLM
  calls, market/FX fetches), the standing rules are untouched: gateway policy
  for LLM egress (docs/07), the privacy lockdown seams (docs/11),
  `mcp_server/policy.py` for anything the MCP process would send.
- **UI copy** (pinned verbatim; goes in the `agent-integration` help guide
  next to the connector steps — shipped with the bold lead sentence as the
  section heading and the remainder as its body):

  > **Your data stays between you and your AI.** fintrack makes no outbound
  > calls for this feature — your AI app connects to fintrack, asks, and gets
  > answers over that same connection. The only party that receives your data
  > is the AI provider you chose (OpenAI, Anthropic, …), because that is who
  > you asked to read it. Nothing else leaves this machine.

## 6. Touch points

| file | change |
|---|---|
| `backend/app/models/mcp_tokens.py` | `McpTokenPolicy.read_propose`, CHECK constraint string |
| `backend/alembic/versions/0060_mcp_read_propose.py` | drop + re-add `ck_mcp_tokens_policy` (next live head at implementation) |
| `backend/app/api/auth.py` | demo mint guard: refuse anything ≠ `read_only` |
| `backend/mcp_server/policy.py` | `PROPOSE_TOOLS`, `MARKER_READ_PROPOSE`, marker/envelope branches |
| `backend/mcp_server/server.py` | `proposals_create` tool; `_filtered_list` exemption |
| `backend/app/api/assistant.py` | `POST /proposals` route (owns by docs/57 card, extended here) |
| `backend/app/services/assistant.py` | `propose()` = `_validate_change` + pending-row persist (shares the core with `ask()`) |
| `backend/tests/test_mcp_agent_surface.py` | count pins 106→107 (`53 read + 54 write`), scope tests, propose-receipt test |
| `backend/tests/test_mcp_tokens.py` | mint `read_propose` round-trip, demo refusal, revoke test |
| `backend/tests/test_byo_ai_connector.py` | end-to-end: propose → pending → no mutation → user apply → audit → undo |
| `frontend/src/pages/helpGuides.ts` | BYO-AI section + §5 copy |
| `docs/40`, `docs/23` | tool counts (incl. the pre-existing 105→107 drift), `read_propose` tier, refusal-code table |
| `docs/08`, `docs/57` | `POST /assistant/proposals` request/response/errors |
| `docs/10` | milestone `M189` |

## 7. Verification (AC → observable)

1. **Read token cannot write** — `read_only` + `transactions_create` → `403
   mcp_read_only`, no backend call (existing); **new**: `read_only` +
   `proposals_create` → same refusal, receipt `403`, tool absent from
   `tools/list` (53 listed).
2. **Proposal requires confirmation before mutation** — MCP `proposals_create`
   → `201`, proposal `pending`, **target row byte-identical** (before/after on
   the card equal the row's actual values); no apply/dismiss/undo tool exists
   on any scope; `apply` only via the user's web session mutates + writes
   `assistant_audit`; `undo` restores the before-values.
3. **Audit for every external read/write** — receipts asserted for a read
   call (`accounts_list` → `200` with `user_id` + `mcp_token_id`), a write
   call, a propose call, and a refusal; `assistant_audit` rows asserted on
   apply and undo.
4. **Revoke** — mint → validate `200` → `DELETE` `204` → row gone (hard
   delete) → validate `401 invalid_mcp_token`.
5. **Gates** — `ruff format --check && ruff check`, `pytest -q` full suite
   green, `tsc --noEmit && eslint --max-warnings 0 && npm run build`,
   `docker compose up -d --build`, smoke through `http://localhost:8080`.

## 8. Non-goals

- No `source` column on `assistant_proposals` in this wave — connector
  proposals are attributed via `mcp_audit.mcp_token_id` + timestamps.
- No MCP tool for listing/applying/history of proposals (the web has them).
- `full` keeps its direct-write tools (docs/40 M80 agent surface unchanged);
  the documented connector posture is `read_propose`.
- The demo tenant cannot mint `read_propose` (stays query-only).
- ~~No per-call rate limiter beyond the standing ones (the propose route
  inherits `POST /assistant/ask`'s middleware posture).~~ **Wrong as written
  (security F3):** slowapi decorates per route and `app/main.py` has no
  rate-limit middleware, so the propose route inherited nothing. It now has
  its own `human_paced_limit("12/minute")`, as do apply/dismiss/undo (one
  shared bucket, docs/57 §4).
- **What the propose ceiling is keyed on (security F3-b, t_828c243a):** two
  buckets, on purpose — `POST /assistant/proposals` runs **12/min per client
  IP** (the trusted `X-Forwarded-For` hop, skipped entirely when a request
  carries no hop at all, because then there is no client to name) **and
  12/min per authenticated tenant** (`app.core.rls.tenant_ctx`, set only from
  a verified access or MCP policy token). The connector path is the reason
  for the second one: `mcp_server` calls the API from the platform's own
  container address, so an IP-only key there would be ONE bucket for every
  tenant on the instance — a looping connector would answer `429` for
  everybody else's first propose (measured: a REST-exhausted bucket → MCP
  propose 429). With the tenant bucket, one tenant's burst spends only its
  own budget, and it is also the ceiling a client cannot reset: minting a
  fresh `X-Forwarded-For` per call still hits the same 12 slots. The same
  tenant's web and connector proposes DO share one bucket — that is the
  per-tenant ceiling working, not the platform-wide one.
