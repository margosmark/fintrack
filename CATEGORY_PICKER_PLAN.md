# Plan: Fuzzy Few-Shot Examples + Exclude Uncategorized from LLM Suggestions

## Context

The LLM categorization prompt currently uses the 10 most recent categorized transactions as few-shot examples. These are often irrelevant to the batch being categorized (e.g., recent "Hydro One" examples when categorizing "Uber" transactions). Additionally, the "uncategorized" category appears in the LLM's category list, which can lead the LLM to suggest it as a valid choice — a useless suggestion.

**Goal**: Improve suggestion quality by (1) using fuzzy payee search to select relevant few-shot examples, and (2) excluding "uncategorized" from the LLM's category list.

## Approach

### Step 1: Fuzzy few-shot examples

**File**: `backend/app/services/categorization.py`

1. **Change `_few_shot_examples` signature** to accept the batch of uncategorized transactions:
   ```python
   def _few_shot_examples(
       session: SASession, user_id: int, batch: Sequence[Transaction]
   ) -> list[Transaction]:
   ```

2. **Replace the simple `ORDER BY id DESC LIMIT 10` query** with a fuzzy search approach:
   - Collect unique `normalized_payee` values from the batch
   - For each payee, query past categorized transactions using pg_trgm scoring (reuse `_payee_score` from `app/api/_search_helpers.py`)
   - Filter: `category_id IS NOT NULL`, `score >= SCORE_THRESHOLD` (0.3)
   - Exclude rejected payees (existing logic)
   - Rank by score descending, then id descending
   - Limit to 10 total (deduplicated by payee — one example per distinct payee pattern)

3. **Implementation detail**: Use a single query with `func.greatest(func.similarity(...), func.word_similarity(...))` across the batch payees. Since pg_trgm works on a single query value, we need to union results per payee. Simpler approach: for each unique payee in the batch (capped at 5 to limit queries), find the best single match. Collect all candidates, deduplicate by transaction id, sort by score, take top 10.

4. **Update the call site** in `_process_one_batch` (line ~248):
   ```python
   few_shots = _few_shot_examples(session, user.id, transactions)
   ```

5. **Fallback**: If fuzzy search returns fewer than 10 examples, backfill with recent categorized transactions (existing logic) to ensure the LLM always has examples.

### Step 2: Exclude "uncategorized" from LLM category list

**File**: `backend/app/services/categorization.py`

1. In `process_llm_batches`, after loading categories, filter out the "uncategorized" category:
   ```python
   categories = [c for c in categories if c.name != "uncategorized"]
   ```
   This excludes it from both the LLM prompt and the `allowed_category_ids` set, so the LLM cannot suggest it.

2. **Why filter by name, not `is_system`**: Other system categories (e.g., "food", "transfers") are valid suggestions. Only "uncategorized" is meaningless as a suggestion.

### Step 3: Update docs

**File**: `docs/04-categorization-llm.md`

1. Update the "few-shot examples" bullet to describe fuzzy payee matching instead of "most recent 10"
2. Add a note that "uncategorized" is excluded from the LLM's category list
3. Mention the fallback to recent examples when fuzzy matches are insufficient

## Critical files & anchors

- `backend/app/services/categorization.py:143` — `_few_shot_examples` (rewrite core query)
- `backend/app/services/categorization.py:220` — `process_llm_batches` (filter uncategorized)
- `backend/app/services/categorization.py:248` — `_process_one_batch` (update call site)
- `backend/app/api/_search_helpers.py:14` — `_payee_score` (reuse trigram scoring)
- `docs/04-categorization-llm.md` — spec update

## Verification

1. **Backend lint**: `cd backend && uv run ruff format --check && uv run ruff check`
2. **Backend tests**: `cd backend && uv run pytest tests/test_cat_pipeline.py tests/test_cat_common.py -v` — existing pipeline tests must pass (they use a fake_provider that doesn't inspect the prompt content, so they'll pass regardless; the key is no regressions)
3. **Manual verification**: Create a few categorized transactions with distinct payees, run the suggestion pipeline, and inspect the LLM prompt in logs to confirm:
   - Fuzzy examples are relevant to the batch payees
   - "Uncategorized" does not appear in the category list
4. **Docker rebuild + smoke**: `docker compose up -d --build api`
