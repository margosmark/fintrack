# 04 — Categorization via LLM

Three-layer pipeline applied to any transaction with a null `category_id`:

1. **Rules engine** — rows from `rules` ordered by `priority`, first match wins, **applies and receipts (M120, docs/02 §transaction_events)**: each row a rule touches gets its own `categorized` receipt with `via: "rule"` and `actor: null`, plus a `tagged` rider (same payload shape as `PUT /transactions/{id}/tags`) when the rule's tags moved the junction. A rule with `stop_on_match=false` lets processing continue to later rules (later categories overwrite, tags accumulate). Explicit dry-runs and targeted backfills live outside the pipeline: the dry-run is client-side since M48-S3 (`s3Engine.previewRule`, the `POST /rules/preview` route is removed) + `POST /rules/{id}/apply` (full spec docs/31). **M121 (docs/25 §M121 D3):** the two PAYEE matchers (`exact_payee`, `contains`) run through the ONE shared `services/match_terms.py::match_terms` with a one-element term list under `mode="any"` — semantics unchanged (`exact_payee` = case-sensitive equality, `contains` = case-insensitive substring), so a categorization rule and a bill's term list can never drift on what "matches" means. `regex` deliberately stays its own path: a regex is a pattern language, not a term list, and `invalid_regex` keeps its own 422 shape.
2. **Learned map** — exact `normalized_payee -> category_id` hits from prior user confirmations, stored in `learned_payees` (docs/02). Consulted only for payees no rule matched; a hit auto-applies, bumps `hit_count`, and receipts `categorized` with `via: "learned"`, `actor: null` (M120, docs/02 §transaction_events).
3. **LLM suggestions** — everything still uncategorized. A high-confidence auto-apply on an `llm_autopilot` account receipts `categorized` with `via: "llm"`, `actor: null` (M120, docs/02 §transaction_events); a suggest-only result writes no `categorized` receipt because it moved no field.

## LLM layer

- Batches of ≤ 25 transactions sent per request.
-Prompt includes the user's full category list (excluding "uncategorized") plus up to **10 few-shot examples** drawn from confirmed transactions. Examples are selected via fuzzy `pg_trgm` payee matching against the current batch, falling back to recent categorized transactions when fuzzy matches are insufficient.
- Response is constrained to existing category ids (JSON-schema-constrained output).
- Results land in `llm_suggestions` as a SUGGEST-ONLY queue — **never auto-applied** unless confidence ≥ 0.9 AND the account flag `accounts.llm_autopilot` is set (default `false`; toggled via the standard accounts PATCH, docs/02).
- Auto-applied suggestions are recorded as `llm_suggestions.status='accepted'` and upsert `learned_payees`, identical to manual accepts — and, since M120, they leave the same `categorized` receipt a manual accept does, distinguished only by `via: "llm"` (docs/02 §transaction_events).

Operational contract: `POST /suggestions/run` (optional `{transaction_ids}`, else every uncategorized row that is not a split parent, capped at 500/run (M107, docs/44 §2 — the scan adds `LEDGER_EXCLUDE_SPLIT`: a split parent is never a candidate because its pre-split category is inert while the split exists, and a CHILD may receive one)) dispatches batched LLM calls via BackgroundTasks. Per call: 25 transactions, 60 s timeout, one retry, then the batch is abandoned and logged — failures never block rules/learned-map categorization. Output violating the category-id schema is discarded; those rows stay uncategorized. Provider resolution: user-row settings override env defaults (docs/02 `users`).
- **Connection probe:** `POST /users/me/llm-test` (optional candidate `LlmTestRequest` body: `llm_provider`, `llm_base_url`, `llm_model`, `llm_api_key`) sends one tiny chat completion to the configured or candidate provider and writes nothing. Returns `200 {ok, model, provider}`. Error envelope otherwise: `400 llm_test_invalid_config` (provider could not be constructed), `502 llm_upstream_error` (ANY upstream failure — non-2xx, transport, parse; upstream status + sanitized URL logged server-side only, never echoed — audit H1), plus `422 invalid_llm_base_url` when a candidate `llm_base_url` violates the egress policy and `403 privacy_mode_blocks_external_api` when privacy mode blocks a non-loopback target at send time (policy in `app/llm/egress.py`, contract docs/11 step 5).

**Error-code registry:** every error `code` (including the `llm_test_*` family) is an **inline literal** at its `api_error(...)` call site — there is NO central constants/registry file in `app/core/errors.py`. `api_error(status, detail, code)` is the only envelope helper.

## Consent & disclosure model (pinned by M54, 2026-09-28)

**There is no per-send consent dialog, and no request schema carries a
`consent` field.** That is a deliberate, pinned decision, not a gap: the
visible `sending N rows to <provider>` dialog an earlier revision of `docs/33`
promised was never implemented and was struck from that ledger in the same
change that added this section (rationale recorded there — `[STRUCK
2026-09-28, M54]`). This section is the authoritative statement of what
qualifies as a send, where consent is actually obtained, and what is enforced
server-side. A future change that wants a consent gate must edit this section
and `docs/33` first (AGENTS §7/§9).

**What qualifies as a "send".** Exactly four authenticated endpoints can put
data on the wire for the LLM layer, and every one of them reaches the network
through the single outbound call `app/llm/base.py::post_chat_completions`
(→ `assert_send_allowed`, `base.py:73`) — there is no other LLM socket in the
app. Nothing fires on a schedule, on login, or on import commit.

| Trigger | Endpoint | What leaves the machine | UI gesture |
| --- | --- | --- | --- |
| Categorization run | `POST /suggestions/run` (`app/api/suggestions.py:41-57`, `SuggestionRunRequest` = `transaction_ids`/`from`/`to` only) | full category-name list, ≤10 few-shot `normalized_payee → category` examples, then ≤25 `transaction_id: normalized_payee` pairs per batch (`services/categorization.py:34,37,226-230,350`) | Suggestions panel → **Run** (`SuggestionQueue.tsx`) |
| CSV column mapping | `POST /imports/suggest-profile` (`app/api/imports.py:242-281`) | CSV headers + up to 5 sample data rows verbatim (`services/column_mapping.py:56-59,321,386`) | Imports → **Configure mappings**; also fires as the `missing_importer_profile` fallback — matched on the envelope `code`, `Imports.tsx:566` predicate used at `:670` (preview) and `:745` (upload) — immediately after **Preview import** / **Confirm import** — a side effect of that click, not a separate gesture |
| Plain-English assistant | `POST /assistant/ask` (`app/api/assistant.py`, `AskRequest` = `question` only, docs/57) | the question (1–2000 chars) + the system prompt + the bounded context JSON: ≤100 accounts, ≤100 holdings, ≤50 recent committed transactions (id/date/payee/amount/currency/category/note), ≤100 categories, ≤25 plan scenarios (`services/assistant.py::build_context`) | **Assistant** page → **Ask** (`Assistant.tsx`; docs/57 §6) — the proposal row it may persist is a DB write, not a send |
| Connection probe | `POST /users/me/llm-test` (`app/api/users.py:97-160`) | the fixed ping text `"Respond with ok set to true."` (`users.py:144`), nothing else; writes nothing | Settings → **Test connection** — M185 (Ledger, docs/47 §4 W2): on the **demo tenant** the control renders DISABLED before dispatch with the visible copy *"Test connection is unavailable in the demo. The demo account blocks POST requests to keep the sample data read-only."* and no request is sent; the endpoint is **not** in `DEMO_MUTATION_EXEMPT_PATHS` and still answers `403 demo_account_restricted` if called. Non-demo behaviour and the privacy-mode egress check are unchanged |

The MCP tools `suggestions_run` (`backend/mcp_server/server.py:1334-1348`),
`imports_suggest_profile` (`server.py:1207-1219`) and `users_test_llm`
(`server.py:189-206`) are legitimate *scripted* callers of all three routes:
they POST the same bodies with no UI in the loop and no consent field — by
design, see below.

**Consent points (the model the code enforces):**

1. **Provider opt-in, once, in Settings.** The destination is only ever the
   user-configured `users.llm_base_url` (else env `LLM_BASE_URL` —
   `llm/base.py:120-137`). Under privacy mode a non-loopback provider cannot be
   selected (`409 privacy_mode_requires_local_llm`, `app/api/users.py:190`) and
   no non-loopback destination survives the send seam (`422
   invalid_llm_base_url` / `403 privacy_mode_blocks_external_api`,
   `app/llm/egress.py`; contract docs/11 step 5). The route releases its request
   session (`services/session_release.py::release_before_provider_wait`) immediately
   before the `chat()` — it writes nothing and the response is built from locals
   already resolved — so a burst of probes cannot pin a pool slot per request
   (docs/07 §Provider-wait ledger; pinned by `backend/tests/test_llm_session_release.py`).
2. **Review before apply.** Suggestions are suggest-only; auto-apply requires
   confidence ≥ 0.9 AND `accounts.llm_autopilot = true` (default `false`,
   docs/02) — a per-account toggle, not a per-send one.
3. **Every send is user-triggered** by the gesture in the table above (the one
   side-effect path — the `missing_importer_profile` fallback — still sits
   inside an explicit **Preview import** / **Confirm import** click).

**Why no consent field is pinned:** such a field would be asserted by the same
authenticated principal the route already trusts, so any client could set it —
a `403` on it would be satisfiable by a script and would prove nothing. The
MCP tools make this concrete: a correct non-interactive caller must either
break or send `consent: true` unconditionally, so the field cannot mean "a
human confirmed this send". A pre-trigger dialog would additionally be
consent-at-trigger, not per-send: `POST /suggestions/run` fans out to ≤25-row
batches through `BackgroundTasks` with one retry
(`services/categorization.py:275-276,348-350`), so no single "N rows" number
describes the sends that follow. The control that does bind is the egress
policy in point 1: server-enforced at the one send point, set once, and
incapable of being toggled from a request body.

**Disclosure.** The complete egress inventory — destination, `Authorization`
key selection (user key, else env `LLM_API_KEY` only for the operator's own
destination — `llm/egress.py:122`), and payload per trigger — is pinned in
docs/11 "What leaves the machine when privacy mode is off"; `docs/33` carries
the `[STRUCK]` ledger entry. Nothing in this section grants a new send path:
adding one is a docs-first change (AGENTS §7).

## Provider abstraction

Providers live in `app/llm/`: `base.py` defines the `LlmProvider` protocol plus the `build_provider` resolver and the shared `post_chat_completions` wire call; `ollama.py` defines `OllamaProvider`; `openai_compatible.py` defines `OpenAiCompatibleProvider`. The categorization pipeline itself lives in `app/services/categorization.py` (NOT `app/services/llm*.py`).

```python
class LlmProvider(Protocol):
    def chat(messages: list[dict], json_schema: dict) -> dict: ...
```

| Adapter | Target | File |
| --- | --- | --- |
| `OllamaProvider` | OpenAI-compatible endpoint `http://localhost:11434/v1`; the base URL is normalized to end in `/v1` if the user omits it (the app appends `/chat/completions`); `http://host:11434` and `http://host:11434/chat/completions` both resolve to `http://host:11434/v1/chat/completions`. | `app/llm/ollama.py` |
| `OpenAiCompatibleProvider` | Configurable base_url / api_key / model → covers OpenAI, OpenRouter, Anthropic-compatible gateways | `app/llm/openai_compatible.py` |

**Ollama model resolution:** `OllamaProvider.DEFAULT_MODEL` (`qwen2.5:7b-instruct`, sized for an RTX 4050 6 GB VRAM) is **unused** — the constructor raises `ValueError` on an empty model, so the model is always taken from the user/env settings (via `build_provider`), never auto-defaulted.

Selected per-run in settings (`LLM_PROVIDER`, docs/01). No keys required when Ollama-only.

## Learning loop

- **Accept** a suggestion -> applies the category and updates the learned-map entry (`normalized_payee -> category`; repeats bump `hit_count`). Promoting a pattern to a regex rule is USER-initiated from the rules editor (prefilled from the selected payee, or since M123 from a `/transactions` search via `/rules?prefill=…`, docs/09 §Create from search — which also supplies the ASSIGNMENT category; a rule still has no account/tag scope) — never automatic.
- **Correct** a transaction (its category and/or its payee) → the SAME rule write: `PATCH /transactions/{id}`, and the `bulk/recategorize` / `bulk/payee` routes (one correction expressed as N rows), call `categorization.learn_from_correction`, which upserts `normalized_payee -> category_id` into `learned_payees` and stamps the `edited` receipt's payload with `learned: {normalized_payee, category_id}` — the marker the History drawer renders as the **"learned from you"** badge. Three guards, so a counter cannot be inflated by edits that taught nothing: an edit touching neither `category_id` nor `normalized_payee` (date, note, amount) writes nothing; **clearing** a category writes nothing and never deletes the rule either (`learned_payees.category_id` is NOT NULL, so one cleared row must not wipe the learning behind every future row of that payee); a row with no `normalized_payee` (E2EE rows whose payee lives only in `payee_cipher`) has nothing to key a rule on.
- **Reject** → record negative signal; that transaction's payee is excluded from the future few-shot pool.

The loop closes over HISTORY: `POST /suggestions/run` re-runs layers 1-2 over any scope (`transaction_ids`, or `from_`/`to`), so the rule a correction just taught is applied to every still-uncategorized row carrying that payee — receipted as a pipeline write (`kind=categorized`, `payload.via="learned"`, `actor=null`) and answered as `queued: 0`, with no LLM call. Rows already categorized are never touched: manual data wins, and `overwrite` stays user-initiated from the rules editor (`POST /rules/{id}/apply`).

The loop is what makes suggestion quality improve locally over time without retraining anything.

## Similar past examples

Similar examples are matched **client-side** since docs/33 S1 (the `GET /transactions/similar` endpoint was removed with the server-side content scan): the browser ranks the user's past entries against the anchor payee with `lib/searchMatch` — a pg_trgm-parity scorer (`greatest(similarity, word_similarity)`, same 0.3 threshold, golden-tested against live pg_trgm) — over rows fetched through `GET /transactions` and resolved through the session vault, with the anchor itself excluded and the list capped at 10. Ordering is `score desc → categorized desc → id desc` (`matchRows`'s `categorizedFirst` option), so the row whose category can actually be applied is the first one; the search page and the global search box keep the plain `score desc, id desc` (docs/33 S5).

Uncategorized same-payee rows are listed too — they are the picture the popover is painting — but they render as INERT rows: only a categorized row carries the one-click apply (`PATCH /transactions/{anchor}` with the example's category). **M119**: the popover filtered to categorized rows only, which starved it into printing "No similar past entries" whenever the anchor was the ONLY categorized row of its payee family — `excludeId` removed that row and the filter then dropped every identical-payee sibling, so the message asserted an absence that nine identical payees on the same screen contradicted.

An empty result is reported honestly: when the session vault is LOCKED and the fetched substrate carries cipher rows, the popover says `Unlock your vault to search …` instead of "No similar past entries" — a locked vault resolves cipher payees to the lock marker, so zero hits there means HIDDEN, not absent (docs/33 A2).

It is a deterministic DISPLAY AID for manual categorization (the UI shows how similar past entries were categorized); it is NOT a pipeline layer and never auto-applies anything.

