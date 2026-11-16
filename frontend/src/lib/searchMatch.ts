/**
 * Client-side fuzzy matching for payees (docs/33 Phase 3, S1/S5).
 *
 * The server no longer scores payee/description text (the last content scan,
 * `GET /transactions?q=`, was removed in M48-S5): exact matches travel through
 * the blind-index `payee_token`, everything fuzzy runs here in the browser
 * over rows already resolved to plaintext (or legacy v0 rows).
 *
 * The scorer is a FAITHFUL port of postgres `pg_trgm` (`trgm_op.c`/`trgm.h`),
 * because the browser has to reproduce the decisions the removed SQL made —
 * `greatest(similarity, word_similarity) >= 0.3` — not an approximation of
 * them. S5's goldens (a live capture of the demo tenant) caught the previous
 * heuristic diverging from pg_trgm on real payees: `q=gro` matched 49 rows on
 * the server and 0 in the browser. Every rule below is pinned by live vectors
 * in `tests/searchMatch.test.ts` and by `tests/fixtures/s5-vectors.json`:
 *
 *  - trigrams are built PER WORD (`find_word` = a run of letters/digits; space
 *    and punctuation separate words), lowercased, padded with two spaces on
 *    the left and ONE on the right (`LPADDING`/`RPADDING`), and every
 *    contiguous 3-gram is kept **in order, with duplicates** — that sequence
 *    is what `word_similarity` slides over;
 *  - `similarity(a, b)` = |A∩B| / |A∪B| over the two trigram SETS
 *    (`cnt_sml` → `CALCSML` compiled with `DIVUNION`), and 0 when either side
 *    is empty (postgres returns 0 rather than 0/0);
 *  - `word_similarity(query, text)` maximizes that same Jaccard over
 *    contiguous windows of the text's trigram sequence — ported line-for-line
 *    from `iterate_word_similarity()`, including its monotone lower bound, so
 *    the browser can never score HIGHER than postgres did;
 *  - postgres computes in float4: `Math.fround` keeps the `>= 0.3` comparison
 *    bit-compatible at the threshold.
 *
 * Threshold DECISIONS and the `score desc, id desc` order are the contract;
 * a numeric score may differ only in the last float32/float64 digits, never
 * across 0.3.
 */

/** Same threshold the API's trigram search used (SCORE_THRESHOLD, docs/08). */
export const SCORE_THRESHOLD = 0.3;

/** Lowercase, fold punctuation/symbols to single spaces, trim (both sides). */
export function normalizeText(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

/** pg_trgm's `find_word`: runs of letters/digits; everything else separates. */
function words(text: string): string[] {
  return normalizeText(text).split(" ").filter(Boolean);
}

/**
 * pg_trgm's trigram ARRAY for one string: one padded block per word
 * (`  word ` — `LPADDING` 2, `RPADDING` 1), every contiguous 3-gram,
 * duplicates kept in sequence order. `word_similarity` walks this positionally,
 * so the order and the duplicates both matter.
 */
export function trgmArray(text: string): string[] {
  const out: string[] = [];
  for (const word of words(text)) {
    const buf = `  ${word} `;
    for (let i = 0; i + 3 <= buf.length; i += 1) out.push(buf.slice(i, i + 3));
  }
  return out;
}

function trgmSet(text: string): Set<string> {
  return new Set(trgmArray(text));
}

/** pg_trgm `similarity(text, text)` — Jaccard over the trigram sets, float4. */
export function similarity(text: string, query: string): number {
  const a = trgmSet(text);
  const b = trgmSet(query);
  if (a.size === 0 || b.size === 0) return 0;
  let common = 0;
  for (const trigram of a) if (b.has(trigram)) common += 1;
  const union = a.size + b.size - common;
  return union === 0 ? 0 : Math.fround(common / union);
}

/**
 * pg_trgm `word_similarity(query, text)` — the greatest similarity between
 * the query's trigrams and any contiguous window of the TEXT's trigram
 * sequence, computed the way `iterate_word_similarity()` computes it.
 *
 * The window's `lower` bound only ever moves forward (postgres saves work by
 * never reconsidering an earlier start), so this is deliberately NOT the
 * mathematical maximum over all windows: matching postgres means reproducing
 * its search, not improving on it.
 */
export function wordSimilarity(query: string, text: string): number {
  const queryTrigrams = trgmSet(query);
  if (queryTrigrams.size === 0) return 0;
  const sequence = trgmArray(text);
  if (sequence.length === 0) return 0;

  // One group id per unique trigram across query ∪ text; `found[g]` marks the
  // groups postgres counts toward `ulen1` (the query's unique trigrams).
  const groupId = new Map<string, number>();
  const found: boolean[] = [];
  const group = (trigram: string): number => {
    const existing = groupId.get(trigram);
    if (existing !== undefined) return existing;
    const id = groupId.size;
    groupId.set(trigram, id);
    found.push(queryTrigrams.has(trigram));
    return id;
  };
  for (const trigram of queryTrigrams) group(trigram);
  const sequenceGroup = sequence.map(group);

  // CALCSML with DIVUNION: count / (len1 + len2 - count), computed in float4.
  const calcsml = (count: number, len1: number, len2: number): number =>
    Math.fround(count / (len1 + len2 - count));

  const lastpos: number[] = new Array<number>(groupId.size).fill(-1);
  const ulen1 = queryTrigrams.size;
  let ulen2 = 0;
  let count = 0;
  let lower = -1;
  let smlrMax = 0;

  for (let i = 0; i < sequence.length; i += 1) {
    const g = sequenceGroup[i];
    if (lower >= 0 || found[g]) {
      if (lastpos[g] < 0) {
        ulen2 += 1;
        if (found[g]) count += 1;
      }
      lastpos[g] = i;
    }
    if (!found[g]) continue;

    const upper = i;
    if (lower === -1) {
      lower = i;
      ulen2 = 1;
    }
    let smlrCur = calcsml(count, ulen1, ulen2);
    let tmpCount = count;
    let tmpUlen2 = ulen2;
    const prevLower = lower;
    for (let tmpLower = lower; tmpLower <= upper; tmpLower += 1) {
      const smlrTmp = calcsml(tmpCount, ulen1, tmpUlen2);
      if (smlrTmp > smlrCur) {
        smlrCur = smlrTmp;
        ulen2 = tmpUlen2;
        lower = tmpLower;
        count = tmpCount;
      }
      const leaving = sequenceGroup[tmpLower];
      if (lastpos[leaving] === tmpLower) {
        tmpUlen2 -= 1;
        if (found[leaving]) tmpCount -= 1;
      }
    }
    smlrMax = Math.max(smlrMax, smlrCur);
    for (let pos = prevLower; pos < lower; pos += 1) {
      const leaving = sequenceGroup[pos];
      if (lastpos[leaving] === pos) lastpos[leaving] = -1;
    }
  }
  return Math.fround(smlrMax);
}

/**
 * greatest(similarity(text, query), word_similarity(query, text)) — the score
 * the removed `GET /transactions?q=` ordered and thresholded on, unrounded:
 * postgres compared the raw float4 to 0.3, so rounding here would flip a
 * decision on any score in [0.2995, 0.3).
 */
export function matchScore(query: string, text: string): number {
  return Math.max(similarity(text, query), wordSimilarity(query, text));
}

export interface MatchableRow {
  id: number;
  /** Resolved display payee (decrypted when the vault is unlocked). */
  payee: string;
  category_id?: number | null;
}

/**
 * Payee a row contributes to matching: the resolved display value when
 * available; legacy v0 rows (no cipher) fall back to their plaintext field;
 * cipher rows NEVER match on plaintext — before resolution completes they
 * contribute "" (and a locked vault resolves them to the lock marker), so
 * encrypted rows simply don't hit until the vault is unlocked.
 */
export function searchPayee(
  resolved: string | undefined,
  row: { payee_cipher: string | null; normalized_payee: string },
): string {
  if (resolved !== undefined) return resolved;
  return row.payee_cipher == null ? row.normalized_payee : "";
}

export interface RowMatch<T extends MatchableRow> {
  row: T;
  score: number;
}

export interface MatchOptions {
  limit?: number;
  /** Row to leave out (the editor excludes its own transaction). */
  excludeId?: number;
  /** /transactions/similar contract: only categorized rows qualify as examples. */
  categorizedOnly?: boolean;
  /**
   * Rank categorized rows ahead of EQUAL-SCORING uncategorized ones (M119).
   *
   * Without it the id-desc tiebreak decides which example the user sees
   * first, so the ONE row whose category can actually be applied can land
   * last — 9 identical payees all score 1.0. SimilarExamples passes this;
   * the default stays false because `score desc, id desc` is docs/33 S5's
   * pinned ordering for the search page and the global search box.
   */
  categorizedFirst?: boolean;
}

/**
 * Rank rows against a free-text query: score desc, id desc (the API's old
 * ordering), capped at `limit`. Below-threshold rows never surface.
 */
export function matchRows<T extends MatchableRow>(
  rows: readonly T[],
  query: string,
  opts: MatchOptions = {},
): RowMatch<T>[] {
  const {
    limit = 10,
    excludeId,
    categorizedOnly = false,
    categorizedFirst = false,
  } = opts;
  const q = normalizeText(query);
  if (q === "") return [];
  const hits: RowMatch<T>[] = [];
  for (const row of rows) {
    if (row.id === excludeId) continue;
    if (categorizedOnly && row.category_id == null) continue;
    const score = matchScore(q, row.payee);
    if (score >= SCORE_THRESHOLD) hits.push({ row, score });
  }
  // score desc, [categorized desc,] id desc — the second key only appears
  // when a caller asks for it, so S5's ordering is untouched by default.
  const rank = (row: T): number => (categorizedFirst && row.category_id == null ? 1 : 0);
  hits.sort((a, b) => b.score - a.score || rank(a.row) - rank(b.row) || b.row.id - a.row.id);
  return hits.slice(0, limit);
}

/** `{items, total, limit, offset}` exactly as `GET /transactions` paginated it. */
export interface SearchPage<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * The `GET /transactions?q=` page envelope, computed in the browser (docs/33
 * S5): every row at or above the threshold, ordered score desc then id desc
 * (the API's ordering), windowed to `offset`/`limit`. `total` is the HIT count
 * — the server's `paginate()` counted the thresholded scope too, never the
 * row count — so paging stops where it used to.
 *
 * `limit`/`offset` default the way `deps.get_pagination` does (50/0).
 */
export function searchPage<T extends MatchableRow>(
  rows: readonly T[],
  query: string,
  window: { limit?: number; offset?: number } = {},
): SearchPage<T> {
  const limit = window.limit ?? 50;
  const offset = window.offset ?? 0;
  const hits = matchRows(rows, query, { limit: rows.length });
  return {
    items: hits.slice(offset, offset + limit).map((hit) => hit.row),
    total: hits.length,
    limit,
    offset,
  };
}
