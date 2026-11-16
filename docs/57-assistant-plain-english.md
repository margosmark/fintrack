# 57 — Plain-English assistant with proposed-change cards (Kilter adoption)

## Context

Kilter's assistant UX, adopted for fintrack: you ask a question in plain
English, get an answer **about your own data**, and — when the answer implies a
data change — a **Proposed Change card** with a *Now vs With-change* delta that
you apply with one click. Every applied change lands in a History list and is
undoable (round-trip restore). Nothing the model says is ever written on its
own: the model proposes, the server validates, the user applies.

This is a **new LLM send path**, so it is a docs-first change (AGENTS §7):
this document is the contract, and `docs/04` §"What qualifies as a send",
`docs/11` "What leaves the machine" and `docs/07` §Provider-wait ledger are all
amended in the same change to name it.

Deliberate scope fence:

- **Q&A + proposals over three surfaces**: accounts, transactions/holdings and
  the plan (`users.forecast_scenarios`). Holdings are answer-only in v1 (no
  position mutation kind) — the registry below is a CLOSED set and grows by
  editing this document first.
- **Single-turn**: no chat memory. Each `POST /assistant/ask` is independent.
- **No new providers, no key in the frontend**: every model call goes through
  `app/llm/base.py::post_chat_completions` (the one LLM socket) behind
  `app/llm/egress.py`'s policy — private/loopback destinations only, `H1` key
  routing, privacy mode enforced at send time.
- **The demo tenant cannot mutate** (no `DEMO_MUTATION_EXEMPT_PATHS` entry):
  every `POST /assistant/*` answers `403 demo_account_restricted` there, and
  `GET /assistant/history` passes as the read it is. See "Demo" below.

## Approach

### 1. Data model (docs/02 §assistant)

Two tables, both tenanted (`user_id` FK → `_USER_OWNED_MODELS`, RLS-secured in
the same migration):

**`assistant_proposals`** — the card's lifecycle.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | PK | |
| `user_id` | FK `users.id`, `NOT NULL`, index | tenancy (AGENTS §2) |
| `question` | varchar(2000) | the ask, verbatim |
| `answer` | text `NOT NULL` | the model's prose answer |
| `kind` | varchar(32) `NOT NULL` | one of the three registry kinds below |
| `target` | varchar(96) `NOT NULL` | `<prefix>:<id>`, e.g. `account:12` |
| `target_label` | varchar(200) `NOT NULL` | human label for the card (account name, payee, scenario name) |
| `summary` | varchar(300) `NOT NULL` | the model's one-line change description |
| `before` | JSON `NOT NULL` | field → value (as asked; verified equal at apply) |
| `after` | JSON `NOT NULL` | field → proposed value |
| `status` | enum `pending \| applied \| dismissed \| undone`, `native_enum=False` | card state |
| `applied_at` / `undone_at` / `dismissed_at` | timestamp, nullable | set by the matching transition |
| `created_at` / `updated_at` | timestamp `NOT NULL` | `TimestampMixin` (mandatory, docs/02) |

**`assistant_audit`** — append-only, one row **per mutation** (`apply` and
`undo` both write one), each carrying the complete before/after of the fields
it moved. Never updated, never deleted except by the account cascade.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | PK | |
| `user_id` | FK `users.id`, `NOT NULL`, index | tenancy |
| `proposal_id` | FK `assistant_proposals.id`, `NOT NULL` | audit → proposal; **before** the proposal in `_USER_OWNED_MODELS` so the cascade order holds |
| `action` | enum `apply \| undo`, `native_enum=False` | |
| `kind`, `target` | varchar | denormalised so the log reads without joins |
| `before`, after | JSON `NOT NULL` | serialized values; `Decimal` → string (AGENTS §2: Decimals serialize as strings) |
| `created_at` / `updated_at` | timestamp `NOT NULL` | mixin; `updated_at` rides along only to satisfy the house invariant |

Indexes: `ix_assistant_proposals_user_created (user_id, created_at DESC)`,
`ix_assistant_audit_user_created (user_id, created_at DESC)` (the `mcp_audit`
shape), plus each column's own `user_id` index.

### 2. The proposal registry (closed set)

`backend/app/services/assistant.py::KIND_TARGET_PREFIX` maps each `kind` to
its target prefix and `ALLOWED_FIELDS` to its **allowed fields**; a field name
outside the allowed set is a refusal (§5), never a silent drop.

| `kind` | target | allowed fields | value rules |
| --- | --- | --- | --- |
| `account.update` | `account:<accounts.id>` | `name`, `starting_balance`, `llm_autopilot` | `name`: non-blank, ≤120 chars, and **refused** when `account.name_cipher` is set or `user.encryption_required` (the assistant has no vault key — a rename would strand the ciphertext, `encryption_required`); `starting_balance`: `null` or decimal string → `Decimal` quantized `0.01`, `\|v\| < 10^16`; `llm_autopilot`: boolean |
| `transaction.update` | `transaction:<transactions.id>` | `category_id`, `note` | `category_id`: `null` or a category **owned by the caller** (404-style mismatch = refusal); `note`: `null` or ≤500 chars. Staging: the target must belong to a `committed` batch (`transaction_in_pending_batch` path) — refused otherwise. Split parent (`is_split`) refused with `409 transaction_is_split`; a split child may take these two fields (docs/44 §5 `_CHILD_FROZEN_FIELDS`). `user.encryption_required` without both ciphers on the row → refusal (mirrors `PATCH /transactions/{id}`) |
| `forecast_scenario.update` | `forecast_scenario:<users.forecast_scenarios[].id>` | `name`, `events` | `name`: 1–80 chars, non-blank (`ScenarioIn`); `events`: raw list validated by `app/schemas/forecast.py::validate_events` (the five docs/45 v1 event types, same envelope code) |

`proposal_target`'s prefix must match the kind's prefix — the model cannot
point an `account.update` at a transaction.

The **before** snapshot is captured from the live row at `ask` time; `apply`
re-reads the row and refuses with `409 assistant_proposal_stale` when the live
value of any touched field no longer equals `before` (a target that vanished
answers `404 not_found`, §4).
That is what makes the card's *Now* column honest: applying never clobbers an
edit the user made after asking.

### 3. `POST /api/v1/assistant/ask`

Request `{question: str (1..2000)}` (`extra="forbid"`).

The route builds the **context** (below), releases the request session
(`release_before_provider_wait` — docs/07 §Provider-wait ledger, `release`),
sends one schema-constrained chat completion through `build_provider(user)` →
`post_chat_completions`, validates the reply, and persists the proposal row.

The whole turn — context read, provider send, proposal write, response —
runs in a worker thread (`api/_offloop.blocking`, docs/01 § "Sessions and
the event loop", security F4 t_828c243a). `post_chat_completions` is a
synchronous `httpx.post` with a 60 s timeout, so on the single-worker loop it
held `/health` and every other route for as long as the endpoint took to
answer: an 8.1 s ask was enough for a health `ReadTimeout`, and any
registered user may set `llm_base_url` to an endpoint of their choosing.

**Context JSON** (everything the model is allowed to know, cache-only reads,
no egress other than the LLM call itself):

```json
{
  "today": "2026-11-10",
  "reporting_currency": "CAD",
  "accounts": [
    {"id": 12, "name": "Chequing", "type": "chequing", "currency": "CAD",
     "starting_balance": "1500.00", "llm_autopilot": false,
     "cash_balance_cad": "2431.07"}
  ],
  "holdings": [
    {"account_id": 3, "account_name": "TFSA", "symbol": "VFV.TO",
     "name": "Vanguard S&P 500 Index ETF", "quantity": "10.0000",
     "price": "160.11", "market_value_cad": "1601.10"}
  ],
  "recent_transactions": [
    {"id": 8841, "date": "2026-11-08", "payee": "Loblaws", "amount_native": "-84.12",
     "currency": "CAD", "category_id": 4, "category": "Groceries", "note": null}
  ],
  "categories": [{"id": 4, "name": "Groceries"}],
  "plan": {"scenarios": [{"id": "ab12cd34ef", "name": "Retirement", "events": []}]}
}
```

Bounds (a prompt is not a data export): accounts ≤ 100, holdings ≤ 100
(`compute_positions`, cache-only), recent transactions = latest 50 committed
(`txn_filters.committed_tx_conditions` — staging rows never reach the model,
docs/03), categories ≤ 100, scenarios ≤ 25 (`MAX_SCENARIOS_PER_USER`, the
write-side cap).
`accounts_cash` supplies `cash_balance_cad` (`None` → `null`, "no honest
figure", never a fabricated 0). All `Decimal`s are serialized as strings.

**LLM response schema** (`response_format: json_schema`, strict — the shared
send point embeds it; the wire schema name stays the shared literal
`categorization`, a cosmetic wart of the single-send seam kept out of scope):

```json
{
  "type": "object",
  "properties": {
    "answer":            {"type": "string"},
    "proposal_kind":     {"type": "string", "enum": ["none", "account.update",
                                                     "transaction.update",
                                                     "forecast_scenario.update"]},
    "proposal_target":   {"type": "string"},
    "proposal_summary":  {"type": "string"},
    "proposal_fields":   {"type": "object"}
  },
  "required": ["answer", "proposal_kind", "proposal_target",
               "proposal_summary", "proposal_fields"],
  "additionalProperties": false
}
```

`proposal_kind: "none"` ⇒ **answer only**, `proposal: null`, no row written;
any other unparseable shape is the §5 refusal.

**Success** → `200 {answer, proposal}` with `proposal`:

```json
{"id": 7, "kind": "transaction.update", "target": "transaction:8841",
 "target_label": "Loblaws · 2026-11-08 · -84.12 CAD", "summary": "Categorize this as Groceries",
 "before": {"category_id": null}, "after": {"category_id": 4},
 "status": "pending", "question": "...", "answer": "...",
 "created_at": "2026-11-10T14:03:11Z"}
```

Rate limit: `human_paced_limit("12/minute")` (`RATE_LIMIT_ENABLED=false` in
tests, docs/03 limiter rules).

### 3b. `POST /api/v1/assistant/proposals` — the BYO-AI connector entry (M189, docs/59)

The connector (ChatGPT / Claude / Codex plugged in through the MCP
`read_propose` scope, docs/40 §2) proposes changes **without a model in the
loop** — the model lives on the user's side, so this route is pure transport
+ validation:

```
POST /api/v1/assistant/proposals        (Bearer: the caller's own JWT,
                                         or the per-user JWT mode 3 issues
                                         for an MCP token)
201 ProposalPublic                      status: pending
```

- **Body** (`extra="forbid"`): `{proposal_kind: str, proposal_target:
  "<prefix>:<id>", proposal_summary: str, proposal_fields: object,
  question: str (1..2000), answer: str (1..2000)}`. `question`/`answer` are the
  card's copy — what the user's agent asked and answered, shown verbatim in
  History. Both are bounded: `answer` was an open `str (≥1)` until security
  F2 (t_108c24f3) — an unbounded field on a row that is never deleted.
- **Validation** reuses §2's registry through `services.assistant.propose()`
  with no LLM call anywhere on the path (pinned by the transport sentinel in
  `tests/test_byo_ai_connector.py`): a kind the registry does not know →
  `422 assistant_proposal_invalid` (same family as a model hallucination);
  shape problems → `422 validation_error`; the target row must exist →
  `404 not_found`.
- **Only the card is written.** `propose()` persists one `pending`
  `assistant_proposals` row — no domain row moves, no audit row, `before`
  mirrors the target (byte-equal after `apply`/`undo`). Confirmation is §4,
  unchanged: the external side has **no** apply/dismiss/undo tool on any
  token scope, so the gate cannot be walked around from outside.
- **Guards:** ordinary auth (`401 invalid_token`), the demo write guard
  (`403 demo_account_restricted`), and the MCP tier refusal codes
  `mcp_read_only` / `mcp_read_propose` before the proxy (docs/40 §2).
- **Rate limit:** `human_paced_limit("12/minute")` on the route itself.
  docs/59 §8 used to say this route "inherits /assistant/ask's middleware
  posture" — slowapi decorates **per route** and `app/main.py` has no
  rate-limit middleware, so there was nothing to inherit (measured: 30 rapid
  proposes → 30×201). It carries its own ceiling now (security F3).

### 4. Apply / Dismiss / Undo / History

| Method/Path | Behaviour | Errors |
| --- | --- | --- |
| `POST /assistant/proposals/{id}/apply` | Re-reads the target, refuses stale, writes the new values, inserts one `assistant_audit` row (`action=apply`, complete before/after), sets `status=applied`. A `transaction.update` also writes the same `transaction_events` receipt `PATCH /transactions/{id}` writes (`record_edit_event`, `via: "assistant"`), so the History drawer cannot tell an applied card from a click | `404 assistant_proposal_not_found`, `409 assistant_proposal_not_pending`, `409 assistant_proposal_stale`, `409 transaction_is_split`, `422 encryption_required`, `404 not_found` (target gone from context's view) |
| `POST /assistant/proposals/{id}/dismiss` | `status=dismissed` — writes nothing else (no mutation, no audit row) | `404 assistant_proposal_not_found`, `409 assistant_proposal_not_pending` |
| `POST /assistant/proposals/{id}/undo` | Requires `status=applied`; re-reads the target and refuses when it no longer equals the applied `after` (someone edited it since), restores `before` field-by-field, inserts `action=undo` (before = the applied state, after = the restored state), writes the mirrored `transaction_events` receipt for a `transaction.update` (the restore IS a categorization change — `via: "assistant"`, delta computed against the applied state), sets `status=undone` | `404 assistant_proposal_not_found`, `409 assistant_proposal_not_undoable`, `409 assistant_undo_conflict`, plus apply's target guards |
| `GET /assistant/history?limit=50` | `status IN (applied, undone)` newest-first, each with its `apply_audit_id` / `undo_audit_id` | `401` only |

Every mutation response is the full history item:

```json
{"id": 7, "question": "...", "answer": "...", "kind": "transaction.update",
 "target": "transaction:8841", "target_label": "...", "summary": "...",
 "before": {"category_id": null}, "after": {"category_id": 4},
 "status": "undone", "created_at": "...", "applied_at": "...", "undone_at": "...",
 "apply_audit_id": 3, "undo_audit_id": 4}
```

Undo is **not** re-appliable: a proposal is `pending → applied|dismissed`, and
`applied → undone` exactly once (409 otherwise). Re-asking is the honest way
to propose the change again.

Apply and undo run inside one transaction with the audit insert (a mutation
that cannot be logged does not happen), and `db.commit()` only after the
receipt + audit are staged.

**The proposal row is locked before its status is read** — all three
transitions load it `SELECT … FOR UPDATE` (`load_proposal(for_update=True)`),
and apply/undo lock the *target* row inside the stale check. Two concurrent
decisions on one card therefore serialize instead of both reading `pending`:
the loser blocks, re-reads the winner's committed status, and answers `409
assistant_proposal_not_pending` (or `assistant_proposal_not_undoable`), so a
card still gets exactly one `assistant_audit` row per mutation, a racing
`dismiss` and `apply` always resolve to one winner with state matching it,
and a hand edit can no longer land between the stale check and the write
(it either committed first — the check refuses `409 …_stale` — or waits and
applies on top of this transaction).

**All three transitions run off the event loop** (docs/01 § "Sessions and
the event loop"): each route hops its whole body — the `FOR UPDATE` wait,
the writes, the audit read — through `api/_offloop.blocking` into a worker
thread, so a wait on one card can never freeze the single-worker loop
(health included). Before this (security F1, t_108c24f3) the routes were
`async def`, the lock wait ran **on** the loop, and a held row deadlocked
the whole process. The wait itself is bounded per transaction: `SET LOCAL
lock_timeout = LOCK_WAIT_LIMIT` (5 s), and an expiry — or any database
failure — answers one envelope, `503 assistant_busy`, never a raw driver
error. apply/dismiss/undo also share a single
`shared_limit("12/minute", "assistant_transition")` bucket per client
(security F3; the shared scope was made true in t_828c243a — the three
routes had sat in three per-route 12/minute buckets, 36 writes a minute in
all, while these docs claimed one); the path parameter never mints a bucket
per card.

### 5. Refusal path (honest failure, no silent write)

Anything the server cannot fully validate raises the error envelope and writes
**nothing** (no proposal row, no audit row, no domain write):

- `422 assistant_proposal_invalid` — unparseable/unsupported `proposal_kind`,
  target prefix mismatch, target not owned or not found, unknown field name,
  field value out of bounds (bad decimal, over-long string, unowned
  `category_id`, invalid `events`), a no-op proposal (`after == before`), or a
  kind-specific guard (E2EE, split row, staging). `detail` names the exact
  field/reason; `answer` is discarded with it — the structured output of that
  run is untrusted end-to-end, so the user re-asks.
- `503 assistant_unavailable` — `build_provider` degraded to `provider=None`
  (no model configured); nothing was attempted, so it is not an upstream error.
- `503 assistant_busy` — a transition's proposal-row lock wait hit its
  per-transaction bound (`LOCK_WAIT_LIMIT`, 5 s) or the database failed
  mid-transition: another decision on this card is in flight, so the client
  retries. One envelope for the whole class — never a raw driver error.
- `502 llm_upstream_error` — the shared send seam's generic envelope for any
  transport/non-2xx/parse failure (upstream status + sanitized URL logged
  server-side only, docs/04).
- `422 invalid_llm_base_url` / `403 privacy_mode_blocks_external_api` — the
  egress policy, raised **before any socket opens** (docs/11 step 5).

The frontend shows the refusal verbatim (envelope `detail`) in the answer
area with "nothing was applied" copy and no card.

### 6. Frontend (`/assistant`)

- `frontend/src/api/assistant.ts` — `askAssistant`, `applyProposal`,
  `dismissProposal`, `undoProposal`, `listHistory` (+ exported types). Uses the
  shared `api()` client: no key, no direct upstream URL, branch on
  `hasErrorCode(err, code)`.
- `frontend/src/pages/Assistant.tsx` — question box (Enter to ask), the answer,
  the **ProposedChangeCard** and the **History list**; all server state through
  react-query (`["assistant","history"]`, `["assistant","ask"]`).
- `frontend/src/components/assistant/ProposedChangeCard.tsx` — target label +
  summary, a *Now* vs *With change* table built by pairing `before`/`after`
  keys (field labels: `name`, `starting_balance`, `llm_autopilot`,
  `category_id` (category name resolved from `categories`), `note`, `events`
  ("N events → M events")), then **Apply** (primary) and **Dismiss**.
- History rows show `Applied`/`Undone` status, applied time, and an **Undo**
  button while `status == applied`; after undo the row renders the restored
  state and no Undo button.
- Route `{path: "assistant", ...}` in `App.tsx` (code-split like its
  neighbours) and one nav row in `components/shell/navGroups.ts`
  (`PRIMARY_GROUP`, label "Assistant", icon `Sparkles`).
- Demo tenant: Apply/Dismiss/Undo/Ask surface the envelope `detail`
  (`demo_account_restricted`) — no special client-side branch (the ledger's
  generic error path already renders it).

### 7. Egress, privacy and rate accounting (the three docs this change amends)

1. **docs/04 §"What qualifies as a send"** gains a fourth row:
   `POST /assistant/ask` → context JSON (§3 above) + the question + the system
   prompt; UI gesture **Assistant → Ask**. It reaches the network only through
   `app/llm/base.py::post_chat_completions`.
2. **docs/11 "What leaves the machine"** gains the same payload description.
3. **docs/07 §Provider-wait ledger** gains
   `app/services/assistant.py::ask` → `release` (the release runs inside that
   function, before `provider.chat`), with the executable form in
   `tests/test_provider_wait_ledger.py::LEDGER`.

No other egress: context building is cache-only (`compute_positions`,
`accounts_cash`, plain row reads).

## Touch points

| File | Change |
| --- | --- |
| `backend/app/models/assistant.py` | new — `AssistantProposal`, `AssistantAudit` |
| `backend/app/models/__init__.py` | import + `__all__` (registry derives from here) |
| `backend/app/schemas/assistant.py` | new — request/response shapes (`answer` bounded 1..2000, F2) |
| `backend/app/services/assistant.py` | new — context, prompt/schema, validation, apply/dismiss/undo/history (transitions take the proposal-row lock; apply/undo also lock the target row; the lock wait is bounded per transaction → `503 assistant_busy`, F1) |
| `backend/app/api/assistant.py` | new — router, per-route rate limits (ask / propose / transitions), the off-loop transition hop, demo guard (inherited) |
| `backend/app/main.py` | mount `assistant.router` |
| `backend/alembic/versions/0059_assistant.py` | new — both tables + `secure_registered_table` × 2 |
| `backend/app/db/rls/policies.py` | `SECURED_TABLES` += both tables |
| `backend/app/api/users.py` | `_USER_OWNED_MODELS` += audit, proposal (audit first) |
| `backend/app/services/tx_events.py` | `render_summary`: `via == "assistant"` wording; `_FIELD_LABELS["note"]` |
| `backend/app/llm/base.py` | unchanged (single send point reused as-is) |
| `backend/tests/test_assistant.py` | new — the contract's pins |
| `backend/tests/test_assistant_concurrency.py` | new — the S-2 row-lock pins: transitions raced on two connections (+ the F1 lock-wait bound pins) |
| `backend/tests/test_assistant_transition_offloop.py` | new — F1: ≥4 concurrent transitions over a live uvicorn socket — one `200`, the rest `409`, `/docs` and `/health` stay responsive throughout |
| `backend/tests/test_assistant_rate_limits.py` | new — F3: propose and the transitions answer `429 rate_limited` after their human-paced burst |
| `backend/tests/test_models.py` | `DOMAIN_MODELS` count 53 → 55 |
| `backend/tests/test_provider_wait_ledger.py` | `LEDGER` entry for `ask` |
| `frontend/src/api/assistant.ts`, `frontend/src/pages/Assistant.tsx`, `frontend/src/components/assistant/ProposedChangeCard.tsx` | new |
| `frontend/src/App.tsx`, `frontend/src/components/shell/navGroups.ts` | route + nav row |
| `frontend/tests/assistant.test.tsx` | new |
| `docs/00`, `01`, `02`, `04`, `07`, `08`, `09`, `10`, `11` | reconciled in the same change (AGENTS §9) |

## Verification

Backend (`uv run pytest`, real Postgres, per-pid suite DB):

1. **Ask happy path** — with `provider.chat` stubbed to a fixed JSON: 200 with
   `answer` + `proposal.before/after`, exactly one `assistant_proposals` row,
   zero `assistant_audit` rows.
2. **Ask, no change** — `proposal_kind: "none"` → `proposal is null`, zero rows.
3. **Refusal** — stub returns an unknown field / bad decimal / foreign
   `category_id` / `proposal_kind: "banana"`: `422
   assistant_proposal_invalid`, `detail` names the field, **zero** proposal and
   audit rows written (the "no silent write" pin).
4. **Apply → history → undo round trip (the headline AC)** — snapshot the
   target row before, apply, assert the new values + exactly one `apply` audit
   row with `before`/`after` matching the snapshot, undo, assert the row is
   **byte-identical to the pre-apply snapshot** (every column, compared field by
   field) + exactly one `undo` audit row, and `status == "undone"`.
   Repeated per kind (account, transaction, forecast scenario).
5. **Audit completeness** — every mutation (apply, undo) has one audit row with
   both payloads; dismiss writes none.
6. **Staleness / conflict** — edit the target between ask and apply → `409
   assistant_proposal_stale`, nothing written; edit between apply and undo →
   `409 assistant_undo_conflict`, nothing written.
7. **Tenancy** — another tenant's proposal id → `404
   assistant_proposal_not_found`; context never contains a foreign row.
8. **Gateway policy** — with `privacy_mode=true` and a non-loopback
   `llm_base_url`, `POST /assistant/ask` answers `403
   privacy_mode_blocks_external_api` (or `422 invalid_llm_base_url`) **before**
   any socket: assert by asserting the stubbed transport was never called.
9. **Guards** — split-parent category change refused; pending-batch
   transaction refused; E2EE tenant refused; demo tenant `403
   demo_account_restricted` on every POST, 200 on `GET /history`.
10. **Provider-wait ledger** + **RLS coverage** + **model/export registries**
    green (`test_provider_wait_ledger`, `test_rls_coverage`, `test_models`,
    `test_export_registry`).
11. **Concurrency — the proposal-row lock (S-2)** — two connections race one
    card (`tests/test_assistant_concurrency.py`): the second `apply` blocks on
    `SELECT … FOR UPDATE`, then answers `409 assistant_proposal_not_pending`
    with exactly ONE `apply` audit row; apply vs dismiss resolves to one
    winner in either order with the stored status matching the winner; a
    second undo answers `409 assistant_proposal_not_undoable` with one `undo`
    audit row. Removing the lock turns all four red (each racer completes its
    transition instead of blocking).
12. **Off-loop transitions + bounded wait (F1)** — a live uvicorn socket runs
    `tests/test_assistant_transition_offloop.py`: four concurrent `apply`s
    answer exactly one `200` and three `409 …_not_pending`, `/docs` stays
    inside its loop budget while they are in flight, and `/health` answers
    after the burst. The same file's sibling pins in
    `tests/test_assistant_concurrency.py` assert the `SET LOCAL` bound is in
    effect inside the transaction (and does not leak past it) and that an
    expiry answers `503 assistant_busy` rather than hanging. On the unfixed
    tree the live probe shows slow/starved `/docs`, hung clients and an
    unreachable `/health` — the security repro.
13. **Answer bound (F2)** — `tests/test_byo_ai_connector.py` pins both edges:
    a 2000-char `answer` is accepted and stored verbatim, 2001 is `422
    validation_error` with zero rows written.
14. **Human-paced assistant writes (F3)** — with the limiter enabled,
    `tests/test_assistant_rate_limits.py` drives 12 rapid proposes (each
    `201`) then a `429 rate_limited`, and the same burst on apply
    (`200` + `409…` then `429`), proving the decorators exist AND enforce.

Frontend (`NODE_ENV=test npx vitest run`, `tsc --noEmit`, `eslint --max-warnings 0`):

- card renders *Now* vs *With change* deltas from `before`/`after`;
- Apply POSTs `/assistant/proposals/{id}/apply` and invalidates the history
  query; Dismiss POSTs dismiss;
- history row with `status=applied` shows Undo; after undo the row shows
  `Undone` and no Undo button;
- a refusal envelope (`code=assistant_proposal_invalid`) renders the honest
  "nothing was applied" copy and **no** card.

Full sequence per AGENTS §4: doc reconciliation → ruff → pytest → `tsc` +
eslint + vitest → `npm run build` → `docker compose up -d --build` → smoke the
`/assistant` page through `http://localhost:8080` → secret scan → commit.
