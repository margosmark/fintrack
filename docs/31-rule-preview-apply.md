# 31 — Rule Preview & Targeted Apply (M46; server route re-mounted M137 P1-2)

## Context

Rules auto-apply inside the categorization pipeline (docs/04) but ONLY to
uncategorized rows at import time. There is no way to (a) see what a rule
would match before saving it, or (b) backfill a rule over history. Users
want a dry-run plus explicit apply scopes: forward-only, all history, or a
date range.

## Approach

Preview scores rows without writing. It ran **client-side** from M48-S3
(docs/33 Phase 3) until **M137 P1-2** re-mounted `POST /rules/preview`
(docs/33 §Server-first computing, ledger row A10): the verdict is now the
SERVER's answer, computed by the same `services/rules_engine.py::rule_matches`
engine the apply path uses, over the same committed scope, so a preview and
the apply it precedes can never disagree about what "matches" means. **M160
(P2-5)** then retired the client twin — `frontend/src/lib/s3Engine.ts::
previewRule` is gone — and the browser calls
`frontend/src/api/rules.ts::previewRule`, a thin `POST` that never writes.
Apply writes rows, so it stays a server endpoint on the `/rules` router
(docs/08). Since M121 (docs/25 §M121 D3) the predicate's two payee matchers
delegate to the shared `services/match_terms.py::match_terms` (one term,
`mode="any"`), which changes no answer — `exact_payee` is still
case-sensitive equality and `contains` still a case-insensitive substring;
`regex` never routes through the term matcher (it is a pattern language with
its own `invalid_regex` failure shape).

### Preview — dry run, never writes (server-computed since M137 P1-2)

`previewRule` in `frontend/src/api/rules.ts` keeps its M46 signature and
POSTs `/rules/preview` (a thin read — no client-side transaction/category
fetching for the preview since M137: the server loads the committed scope and
the `category_id -> name` map itself).

Request (draft-friendly: works for unsaved rule forms):

```json
{
  "match_type": "contains",
  "pattern": "NETFLIX",
  "from": "2026-01-01",
  "to": "2026-09-03",
  "only_unassigned": true,
  "limit": 25
}
```

- `match_type`/`pattern` validated exactly like `POST /rules`
  (422 `invalid_regex` / `pattern_too_long` / `invalid_amount`), with the
  same `detail` strings — the messages are part of the contract; the captured
  golden set is replayed against the live route
  (`backend/tests/test_m160_replay_s3.py`, the `rules.preview.*` cases).
- Request-shape validation mirrors the `RulePreviewRequest` pydantic model
  field-for-field: unknown keys (`extra="forbid"`), missing/empty
  `match_type`/`pattern`, `limit` outside 1–100, malformed `from`/`to`
  → 422 `{detail: [<pydantic-shaped error objects>], code: "validation_error"}`
  with `loc: ["body", <field>]`.
- Scope: committed transactions of the caller (the `GET /transactions`
  population already applies docs/03 committed-only staging); `date` bounds
  via the docs/02 date-field rule (booking `date`, never `posting_date`),
  inclusive on both ends.
- `only_unassigned=true` (default) restricts to `category_id IS NULL`;
  `false` matches everything in scope.
- Response: `{matched_count, assigned_skipped, items: [{id, date, payee, amount_native,
  category_id, category_name}]}` with `items` capped at `limit` (1–100,
  default 25), newest first (date desc, then id desc). `assigned_skipped`
  counts matches hidden by the unassigned-only filter so an empty preview
  can explain itself. `amount_native` is the 2dp `Numeric(18,2)` column
  echoed as a string (`"-15.99"`); `category_name` is `null` when the row
  is uncategorized.

**Route history — M48-S3 deleted it, M137 P1-2 re-mounted it (security
follow-up F-5).** M48-S3 moved preview into the browser and removed the
route; **M137 P1-2 re-mounted `POST /api/v1/rules/preview`** (docs/33
§Server-first computing: "the verdict is the SERVER's answer"), so the old
"the route is gone / answers 405" claim is FALSE and must never be repeated —
the path has its own route and its own request schema:

- live tenant: **422** `{detail: [<pydantic errors>], code: "validation_error"}`
  on a bad body (missing/empty `match_type`/`pattern`, bad `limit`, …), 200
  with `{matched_count, assigned_skipped, items}` on a good one;
- demo tenant: **403 `demo_account_restricted`** — the method-scoped write
  guard in `backend/app/api/deps.py` refuses every mutating method for
  `demo@fintracker.local` and `/rules/preview` is not in its exemption set,
  even though the handler itself never writes.

Pins: `backend/tests/test_rules_preview_apply.py::test_preview_is_mounted_and_needs_auth`
("401, never 405/404") and the `test_preview_*` cases in the same file.
`POST /rules` and `POST /rules/{rule_id}/apply` are unchanged.

### POST /rules/{rule_id}/apply — targeted backfill, writes

Request:

```json
{
  "mode": "all | range | forward",
  "from": "2026-01-01",
  "to": "2026-09-03",
  "overwrite": false
}
```

- Rule must exist and belong to the caller (404 `not_found` otherwise).
- `mode=all`: whole history. `mode=range`: `from`/`to` required
  (422 `invalid_date_range` when missing/inverted). `mode=forward`:
  performs zero writes — the rule stays enabled and the import pipeline
  covers future rows; response carries `note` saying so.
- `overwrite=false` (default): only `category_id IS NULL` rows are
  updated. `true`: matching rows are re-categorized regardless.
- The rule's own predicate decides matches (NOT first-match-wins across
  rules — this is an explicit user action on one rule). Category and the
  rule's `tag_action`/`tag_ids` apply exactly like the pipeline.
- Response: `{rule_id, mode, matched_count, applied_count}`.
- Commits once; error envelope on domain failures (AGENTS.md §2).

### Frontend (`/rules`, docs/09)

Per-rule `Preview` affordance opens a dry-run panel: scope selector
(All history / Date range with from–to inputs / Forward only),
Only-unassigned toggle, `Run preview` → count + sample table (an empty
preview names the assigned rows the filter hid). `Apply` (with explicit
mode + overwrite checkbox) runs the backfill and shows `{matched,
applied}`. Forward-apply shows the pipeline note. The rule form's Tags
section has inline `+ New…` creation (same pattern as the transaction
editor) — no prompt, and the new tag is preselected in the draft.

## Touch points

- `backend/app/api/rules.py`: two endpoints + request schemas.
- `backend/app/services/rules_engine.py`: single-rule applier reused by
  the endpoint (pipeline's `apply_rules` untouched).
- `backend/app/schemas/rules.py`: `RulePreviewRequest/Response`,
  `RuleApplyRequest/Response`.
- `backend/tests/test_rules_preview_apply.py`: counts, sample shape,
  no-write preview, all/range/forward modes, overwrite default,
  tenancy, invalid pattern/range envelopes.
- `frontend/src/api/rules.ts`, `frontend/src/pages/Rules.tsx`,
  `frontend/tests/rules.test.ts`.
- `docs/08-api-spec.md` (endpoint rows), `docs/04` (one-line pointer),
  `docs/10-roadmap.md` via `docs/24` M46 entry.

## Verification

- `uv run pytest tests/test_rules_preview_apply.py -q` green on real
  Postgres with seeded payees across months.
- `npx vitest run tests/rules.test.ts` green (red-without/green-with
  on the preview wiring).
- Live as a TENANT: create rule `contains DEMO-PAYEE`, preview shows the
  seeded count + sample, apply `range` updates exactly those rows,
  `/rules` page renders count/sample/apply result. **As demo none of that is
  reachable**: every one of them is a `POST`, and the demo tenant's
  method-scoped guard answers **403 `demo_account_restricted`** for all of
  them (measured: `POST /rules/preview` → live `422` on an empty body /
  demo `403`) — a demo session cannot create, preview or apply a rule, and
  the UI reports those refusals as demo refusals, never as errors
  (docs/47 §4).
