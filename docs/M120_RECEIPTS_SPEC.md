# M120 — Receipts for the four non-receipted mutation surfaces (t_81e4e640)

**Decision made here: reading (b).** The M106 promise — *"edits never
silently vanish; every change is a receipt"* — has four standing
exceptions left on this tree (verified 2026-10-08 at
`origin/main = f6ff18b`, greped live). An Accept click or a bulk rule
backfill that re-categorizes rows without leaving a receipt is exactly
the silence the feature exists to prevent, and closing it is cheap:
the writer (`record_event`), the renderer (`render_summary`), the
drawer, the export registration, the partner projection and the
query-level live tests all already exist and are generic. This spec is
the AGENTS §7 blueprint; the implementation child closes itself with the
same gates every M-wave runs (Actions is waived per Mark's standing
ruling — local gates + live probe are the acceptance standard).

The four surfaces, verified current:

| surface | source of change | fields it moves | whose receipt | kind |
|---|---|---|---|---|
| `POST /suggestions/{id}/accept` (`api/suggestions.py:120`) | a person clicks Accept | `transaction.category_id`; also upserts learned map | the clicking **user** (`actor=current_user.id`) | `categorized` |
| `POST /rules/{id}/apply` (`api/rules.py:160`) | a person runs a backfill (docs/31) | `category_id` (+ tags) on N rows | the calling **user** | `categorized` and/or `tagged` — see §1 |
| `POST /transactions/recurring/accept` (`api/transactions.py:746`) | a person tags a candidate group (M96 prompt) | `is_recurring`, `recurring_cadence`, `recurring_next_date` on N rows | the calling **user** | `edited` |
| pipeline applies (`services/categorization.py` — layer 1 rules `apply_rules`, layer 2 learned map, layer 3 LLM autopilot `_process_one_batch`) | import, sync, or the `Run` button (`POST /suggestions/run`) | `category_id` (and rule tags via layer 1) | **the system** (`actor=None`) | `categorized` / `tagged`, mirror of layer 1's own writing shape |

That is the complete gap set. During the blueprint walk two adjacent
paths were checked and do NOT belong here: merge-back
(`DELETE /transactions/{id}/split`) keeps the parent's category/tags/etc.
byte-identical ("the parent keeps everything it had", the handler
docstring) and already writes its `merge` receipt per D15, so there is no
unreceipted field change in it; and replace-split (`PUT …/split`) likewise
touches only children. Reviews of this spec should re-run that exclusion,
not re-invent it.

## Context

M106 (root `t_f564a3d1`) shipped the append-only `transaction_events`
receipt log with a docs/02 path table holding every receipted mutation.
The follow-up audit (t_a060b5ad) found four surfaces that write none and
reported rather than picked a reading; M106 pinned the boundary in
docs/02 → "Receipt scope (reported, not hidden)" and spawned this card.
The M106 reading (a) — "deliberately out of scope" — is revisited here to
**no** because the surfaces are not exotic back-office paths: three of
them are button-clicks a person makes on data they can already see the
History drawer for, and the fourth is the pipeline's own bulk categorizer,
which is the single biggest volume of `category_id` writes on a fresh
import. A drawer for the same row that says "you created this from import
Batch #148" and then shows nothing about the row being auto-classified
moments later is precisely the half-visibility M106 was built to refuse.

**The demo SEED still writes no receipts** (docs/02: "the demo SEED
writes no receipts (the handler-layer rule)"). This card does not change
that: the demo tenant cannot write at all (`403 demo_account_restricted`
via the centrally-enforced dependency, not per-router), so demo rows stay
receiptless until a real tenant interacts. That boundary is pinned by
test, not just remarked.

## Approach — pinned decisions (do NOT re-litigate in impl)

### 1. `event_kind` per surface — reuse, never new enum values

The closed set (`created | edited | categorized | tagged | reviewed |
split | merge | deleted`) gets **zero additions**. Each surface maps to
an existing kind, mirroring what the equivalent PATCH-driven write would
have produced:

- **suggestions-accept** → `categorized`, payload
  `{"category_id": <id>, "category_name": "<name at write time>"}` —
  the EXACT payload shape `record_edit_event` already fits for a
  category-only PATCH (docstring: "`categorized` when the category
  moved"), so drawer queries over one user's history see the same kind
  for the same underlying change regardless of which surface caused it.
  The suggestion id lands in payload as `"suggestion_id": <id>` so the
  origin is visible; it plays the same role `"batch_id"` plays on an
  import `created` receipt.
- **pipeline applies** → `categorized` for a category write (rule hit or
  learned-map hit or autopilot), payload
  `{"category_id", "category_name", "via": "rule" | "learned" | "llm"}`
  — the `via` key is the only extra; it names which pipeline layer the
  receipt came from so the drawer line differs ("a rule set this") as
  docs/04 already words the three layers. Layer 1's tag application
  (`_apply_tags` from a rule hit) additionally writes a **`tagged`**
  receipt when the junction actually moved — same one-receipt-per-row
  rule a `PUT /tags` call writes, and the SAME payload shape the tag
  routes already use: `{"added": [<tag names>], "removed": [<tag
  names>], "added_ids": [...], "removed_ids": [...]}` (`added`/`removed`
  are NAMES for the drawer line, `*_ids` exist because the routes write
  them — mirror both sets, do NOT invent a slimmer shape); rule tags are
  ADD/SET (no removal semantics in the engine), so `removed` /
  `removed_ids` can be non-empty only for a `tag_action="set"` replacing
  previously-carried tags, and are `[]` under `"add"`.
- **rules-apply** → identical kinds to pipeline applies, payload
  `{"category_id", "category_name", "via": "rule_backfill"}` + the
  matching `tagged` payload above when its own `tag_ids` moved junction
  rows. It is the same semantic act (rule hit, one row) with a
  different provenance string.
- **recurring-accept** → `edited`, payload
  `{"fields": ["is_recurring", "recurring_cadence",
  "recurring_next_date"], "changes": {…}}` — the generic
  `record_edit_event` shape with NO new helper, since this is a plain
  multi-field PATCH-equivalent on three identity-adjacent columns (the
  user-visible badge state, docs/08). No `categorized` receipt from this
  route even when `category_id` coincidentally changes elsewhere at the
  same time (it never does — the route writes only recurring fields).

**Never** a second receipt row for the same field change on the same row
from one call. (The transfer routes already set the precedent: one
`edited` receipt carries `transfer_pair_id` AND `category_id` in a single
payload rather than emitting two rows.)

### 2. Bulk expanse — every affected row gets its own receipt

`POST /rules/{id}/apply` with `mode=all` and `overwrite=true` can touch
every row a user has. It writes **one receipt per affected transaction,
in the same commit** — the same contract `stage_rows` already holds for
import (`created` receipts) and `POST /review` (one per row, docs/09 §
Bulk selection toolbar: "N rows are N sequential requests, no bulk
schema"). This is deliberately NOT deduplicated into a single group
receipt: a receipt that says "3 rows changed" is only inspectable for
rows it names inside its payload, and receipts that shrink to fit 3
rows' detail into one row lose the per-row `payload_hash` a History
drawer re-checks. If a user complains about row-volume they can be
pointed at the same growth story every append-only log carries (`VACUUM
FULL` on the reviews table, or an archival cutoff in a later card —
parked, not invented here).

`overwrite=false` writes **nothing for rows it skipped** (already
categorized) — same no-op rule the PATCH route already writes.

### 3. `actor` — real id for the four human surfaces, `NULL` for pipeline

- suggestions-accept, rules-apply, recurring-accept →
  `actor = current_user.id`, the same column
  value any PATCH route stores today.
- pipeline applies → `actor = None` (the same open choice
  `record_event`'s docstring already carries, the same `actor=None` a
  sync-pulled row's `created` receipt uses). This is the **key
  product consequence**: the partner projection's
  `actor.in_([owner_id, partner_id])` in `api/partner.py` enzymically
  **excludes** `actor=None` rows — the SQL-level choice installs those
  receipts in the owner's view and nowhere else's, no new guard
  needed.

### 4. `render_summary` — one new branch shape, three new wordings

`services/tx_events.py::render_summary` gains **no new parameter, no new
kind, and no new function** — the existing generic `edited` fallback
path (unknown fields → "you edited …") and the existing `categorized`
branch already cover everything a payload can now carry, with **one
new branch**:

```python
# M120: pipeline + backfill writes name HOW the category arrived so a
# drawer reader can distinguish theirs from its (a suggestion is a
# confession, a rule is a prediction — docs/04's own three layers).
via = payload.get("via")
if kind == TransactionEventKind.categorized:
    name = payload.get("category_name")
    if not name:
        return f"{actor} cleared the category"
    if via == "rule":
        return f"{actor} set category {name} (rule)"
    if via == "learned":
        return f"{actor} set category {name} (learned from your edits)"
    if via == "llm":
        return f"{actor} set category {name} (assistant suggestion, auto-applied)"
    if via == "rule_backfill":
        return f"{actor} set category {name} (rule backfill)"
    if via == "suggestion_accept":
        return f"{actor} set category {name} (accepted suggestion)"
    return f"{actor} set category {name}"   # a plain PATCH — unchanged
```

A plain PATCH's `categorized` payload carries **no** `via` key, so its
existing summary is byte-identical to today — the branch is additive,
and the M106 snapshot (`test_payload_hash_matches_hand_recomputed_
digests`) of `SHA_UNICODE_EDIT` keeps deriving to the same bytes.
Verify no test pins byte-different strings for the four new wordings
(`rule`, `learned`, `llm`, `rule_backfill`; `suggestion_accept`
renders from the same branch keyed by its own `via` value — five new
strings, four of them for the three pipeline shape keys plus the
backfill).

### 5. Partner projection

`actor = None` receipts from pipeline applies are **automatically
dropped** for partners by `partner_transaction_events`'s existing
`TransactionEvent.actor.in_(allowed)` — no new predicate, no new code.
Verified on this tree at `api/partner.py:307` (the route): the filter is
applied at the query layer before projection, so `_USER_OWNED_MODELS`
tenancy is unaffected. The one test needed pins the same shape the
existing `test_partner_projection_boundary` (the `sync`-receipt
section, `actor=None`) already proves: a `via=rule` pipeline receipt
with `actor=None` reaches the owner and is absent from the partner's
page.

### 6. No-op / refusal boundaries (per surface)

- **suggestions-accept**: the existing 409 `suggestion_not_pending` /
  404 `not_found` paths already exit before any write — no receipt. The
  only NEW receipt carries the same commit the category move does, so
  there is no window where the category moved and the receipt did not.
- **pipeline applies**: no-op nothing — a rule hit on an
  already-matching row where `category_id` was ALREADY the rule's target
  still counts as a **write** today (`transaction.category_id =
  rule.category_id` unconditionally in `apply_rules`), so it writes a
  receipt (a rule hit that "confirms" what was already there is still a
  mutation the log narrates — same choice `PUT /tags` makes when the
  wanted set re-applies). The learned-Map path (`remaining =
  [t for t in transactions if t.category_id is None]`) and the LLM
  autopilot path (`Transaction.category_id.is_(None)` in the batch
  scanner) only write rows whose `category_id` WAS null, so they
  cannot produce no-op receipts. To keep layer 1 symmetric with layers
  2/3 it writes one receipt `via=rule` regardless of what the
  previous `category_id` was (with a null previous, the summary reads
  identically to a learned-map hit; the distinguisher for the reader
  is the `via` string, not a diff).
- **rules-apply**: rows `apply_single_rule` skipped under
  `overwrite=false` write nothing (same reading as the pipeline no-op
  above). `match_transactions` returning zero rows writes nothing.
- **recurring-accept**: the route re-writes the same three fields per
  row today regardless of prior state — one receipt per id per call,
  even if the fields were already what the call set them to (a re-tag
  is still a click a person consciously made, different from the
  review route's written-down idempotence contract, which is pinned
  separately and does NOT extend here).
- **pre-handler refusals** (`422` body validation, `429` limiter,
  `403 demo_account_restricted` guard — all dependency or decorator
  layer, before any handler body runs): unchanged and still write
  zero receipts. Existing M106 test `test_pre_handler_refusals_write
  _no_receipt` covers the shared principle for two routes; the impl
  extends the same assertion-file pattern inline (no new test file).

### 7. Split/merge guards do NOT change

`POST /suggestions/{id}/accept` keeps its `_refuse_split_row_write`
guard (M107, docs/08:409 receipt `transaction_is_split`) — its refund
behavior on a split parent is unchanged. Pipeline applies run against
LEDGER rows only (`_uncategorized` and batch queries still scope the
same input set and there is no split row in scope) so they are
structurally unaffected. `recurring-accept` runs `_ensure_live` per id
(the existing pre-check) and the docs/08 mutation matrix
("recurring accept/suppress on a parent → allowed, on a child"
unchanged).

### 8. UX: the drawer, the queue, and their counts just work

No drawer changes, no suggestion-queue changes, no new chic-col
column. HistoryDrawer already renders whatever the endpoint returns;
SuggestionQueue's toasts stay as-is. `GET /transactions/{id}/events`
(`docs/08`) and the export/ZIP table coverage via _visibility filter_
are automatic (the events table is already in `/export/full` and in
the ZIP under the committed-row visibility rule; pipeline receipts
attach to rows, and staged rows' receipts stay hidden until commit —
already true today).

One item is a **test-only** touch: `frontend/tests/HistoryDrawer.test
.tsx` (or its equivalent) may stub the endpoint's response shape and
must tolerate a `categorized` event whose `payload` carries `via`.
Simplest verification: render the drawer against the documented
backend payload fixtures and assert the summary arrives verbatim —
matches how the drawer already consumes `event.summary` without
parsing payloads.

## Docs to reconcile in the SAME commit

- **`docs/02-data-model.md` §transaction_events**: (a) the path table
  gains rows for the four surfaces, (b) the existing note in the
  `payload` examples listing does not need changing (it already shows
  `category_id` + `category_name` as a shape), (c) the
  "Deliberate boundary (reported, not hidden)" section is replaced
  by a new "Receipt scope (v2, M120)" note: every mutation path writes;
  the previous four exceptions are lifted; the table above is
  exhaustive as written again.
- **`docs/04-categorization-llm.md`**: the layer-1 "applies silently"
  phrase is amended to "applies and receipts (M120, docs/02
  §transaction_events)"; a one-line pointer per layer.
- **`docs/08-api-spec.md`**: rows for the three pushed-format routes
  carry an "M120: writes receipts" clause; `GET
  /transactions/{id}/events` row notes the `via` payload extension.
- **`docs/09-ui-ux.md` §History drawer docs** (the "### History
  drawer" section): the sentence examples gain one pipeline example
  ("Oct 5 — the system set category Groceries (rule)"), and the
  pinned sentence list in the contract's drawer section is extended
  the same way docs/09 already pins wording for transfer/merge
  sentences.
- **`docs/10-roadmap.md`**: new milestone entry **M120 — receipts for
  the four non-receipted mutation surfaces**, deps = M106, M96,
  M107, M46; the doc-reconciliation set above.
- **`docs/44-split-transactions.md`**: no change needed — merge-back
  does not gain a receipt (the parent's fields are untouched, verified
  in the surface table above); this row is recorded so the exclusion is
  auditable rather than silent.

## Touch points (implementation card's file list)

- `backend/app/api/suggestions.py` — 1 `record_event` call after the
  category/learned writes, before `db.commit()`.
- `backend/app/api/rules.py` — one receipt per updated row, inside the
  commit-spanning loop; category name resolved once per rule (the rule
  already holds `category_id`; the write needs `category_name` at
  write-time — fetch the Category once per apply call).
- `backend/app/api/transactions.py` — recurring-accept's write loop.
- `backend/app/services/rules_engine.py` — `apply_single_rule` and
  `apply_rules` gain an optional receipts write (no interface change
  visible to the API router: the receipts append into the caller's
  session inside the affected-row loop, the same way `_apply_tags`
  already appends junction rows there).
- `backend/app/services/categorization.py` — layer 2 and layer 3
  receipt writes, plus layer 1 fed receipts from rules_engine.
- `backend/app/services/tx_events.py` — `render_summary`'s new branch
  (the §4 wording table); helper for fetching category names at write
  time.
- `backend/app/models/transaction_events.py` — docstring paragraph
  (no columns, no enum values, no migration).
- `backend/tests/test_m106_review_events.py` — the existing test file
  gains one test function per new surface inline (no new file); each
  new test reuses the shared `_register`/`_create`/`_events`/
  `_kinds` helpers above it. The existing 12 tests' expectations are
  NOT revised; anything that shifts is named in the new test
  functions instead of edited.
- `docs/02/04/08/09/10/44` — the reconciliation set listed above
  (44 is the recorded no-change row).

No frontend-source file changes at all: HistoryDrawer reads only
`event.summary`, which the backend already renders; the frontend's
`TransactionEventKind` union (`api/transactions.ts`) lists only the
eight existing kind strings and the new receipts use only those — the
safe order is backend first, so the union never has to change.

No migration: `event_kind` widening was M107's job and is done;
`payload` is schemaless JSON; the number of enum values added here is
zero.

## Verification (the implementation card's gate contract, mirroring test_m106_review_events.py)

Backend, one new test per surface (co-located in
`test_m106_review_events.py`, extending the 12 to 12+5=17 minimum;
the impl may break into more functions if surfaces share a fixture):

1. **suggestions-accept**: seed a suggestion (direct-table-insert a
   `suggested` row, the way the M107 route tests already seed); accept →
   exactly one new receipt, kind `categorized`, payload carries
   `category_id`/`category_name`/`via=suggestion_accept`/`suggestion_id`,
   `payload_hash` re-derives via the pinned `_sha` helper, `actor ==
   owner_id`, summary `you set category Groceries (accepted
   suggestion)`.
2. **rules-apply**: create a rule (tag_ids included), apply in range
   mode over 3 rows → each row's log gains exactly one `categorized`
   (+ one `tagged` when tags moved) receipt; a second apply against
   `overwrite=false` over the same rows writes NOTHING; the payload
   `via` is `rule_backfill` and the `actor` is the calling user.
3. **recurring-accept**: accept over 2 rows → each row gains exactly
   one `edited` receipt, payload `fields[3]` + `changes` checked
   per-field, `actor` the user, `via` key absent (this surface has no
   new wording, so the byte-identical summary rule for a plain
   `edited` PATCH receipt applies verbatim).
4. **pipeline applies**:
   a. layer 1 (`apply_rules` via `run_pipeline_sync`): a rule hit
      writes `categorized` `via=rule` `actor=NULL` per
      affected row (both when the row was uncategorized and when the
      rule confirms the target value — the symmetric write is the
      §6 reading); a rule tag hit writes `tagged` when the junction
      moved.
   b. layer 2 (learned map): a hit writes `categorized`
      `via=learned` `actor=NULL`.
   c. layer 3 (LLM autopilot): a high-confidence suggestion on
      `llm_autopilot` account writes `categorized` `via=llm`
      `actor=NULL`.
   No-op for layer 2/3 paths (they only scan `category_id IS NULL`).
5. **no-receipt surfaces stay no-receipt**: merge-back and
   replace-split write exactly what they write today (`merge` on the
   parent; `split` on the parent) and nothing new — the exclusion
   recorded in the surface table is pinned so a future refactor
   cannot silently widen receipts onto a path whose fields never
   moved.
6. **partner projection**: pipeline receipts (`actor=NULL`) are
   invisible on `GET /partner/transactions/{id}/events` (count
   drops accordingly vs the owner view), and the STRIPPED payload
   field names still list `via` on other receipts (e.g.
   `changed_fields` includes nothing extra on pipeline receipts
   because pipeline receipts don't reach the partner at all).
7. **pre-handler refusals**: a 429 on imports-check body validation
   route (or the limiter on any mutating route) still writes zero
   receipts on any newly receipted surface — the M91-6 ruling's
   unchanged mechanism.
8. **export coverage**: `/export/full` carries the new receipts on a
   committed row; the ZIP's CSV carries them too; a pipeline receipt
   on a STAGED row is invisible from the ZIP until commit (already
   the general VISIBILITY_FILTERS behavior — one assertion pins it).

`ruff format --check && ruff check`, `pytest -q`
(isolated DB, `SEED_DEMO_ACCOUNT=false`, ~the same runtime as the
current 1367-case suite), `npx tsc --noEmit`, `npx eslint
. --max-warnings 0`, `NODE_ENV=test npx vitest run`,
`npm run build`. Actions is out (standing waiver); live-probe against
the rebuilt stack redeployed for the milestone, with the drawer
probed in-browser for the `via` wording, mirroring the previous
M106 review-probe pattern.

## Acceptance (for the implementation card)

- [ ] Each of the four apply shapes above (suggestions-accept,
      rules-apply, recurring-accept, and the pipeline's three layers
      with their two kinds) writes the pinned receipt
      payload (§1's exact fields, §4's exact summary), verified by
      real isolated-DB pytest, not by reading.
- [ ] No existing test regressed without its own reason noted.
- [ ] docs/02's path table and docs/04's three layer notes no longer
      describe silent applies, and the docs 08/09/10/44 lines
      listed exist with exact wording linked above.
- [ ] Partner projection drops pipeline receipts (actor-NULL filter
      working without a new predicate).
- [ ] Full gates green (the battery above) + live probe
      evidenced on the deployed stack (drawer wording + partner
      scrub check).
