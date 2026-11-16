/**
 * M123 — "Create from search" (docs/09 §Create from search).
 *
 * ONE criteria shape — the four URL-addressable `/transactions` params —
 * encoded three ways:
 *
 *   1. the live filter state on the Transactions page (`applied` + `searchQ`),
 *   2. `?q=…&category=…&account=…&tag=…` — the restore/bookmark URL, which is
 *      exactly what a `kind="transactions"` saved view stores as `state`,
 *   3. a `?prefill=transactions&…` deep link into a target editor.
 *
 * Because (2) and (3) are the same encoding, a saved view IS a bookmark and
 * every target reads one parser instead of three. Values are validated on the
 * way in with the SAME rules the server applies to `state`, so a hand-written
 * link degrades to "that param was ignored" rather than a NaN filter.
 */

import { normalizePayeeKey } from "./payeeKey";

/** The four URL-addressable search params, in URL order. */
export const CRITERIA_KEYS = ["q", "category", "account", "tag"] as const;

export type CriteriaKey = (typeof CRITERIA_KEYS)[number];

/**
 * A slice of the Transactions search. Absent key = "not filtered"; the
 * encoding never carries an empty string, so `state` and `?…` round-trip
 * without a sentinel.
 */
export interface SearchCriteria {
  /** The fuzzy search term — `GET /transactions?q=…`, NOT the payee filter. */
  q?: string;
  /** A category id, or the literal `"uncategorized"`. */
  category?: string;
  /** An account id. */
  account?: string;
  /** Comma-separated tag ids — "matches ALL of these". */
  tag?: string;
}

/** Marker param every target editor reads on mount (once) before its criteria. */
export const PREFILL_PARAM = "prefill";
export const PREFILL_VALUE = "transactions";

const ID_RE = /^\d+$/;
const TAG_LIST_RE = /^\d+(,\d+)*$/;
const CATEGORY_RE = /^(?:\d+|uncategorized)$/;

function isValid(key: CriteriaKey, value: string): boolean {
  if (value.trim() === "") return false;
  if (key === "q") return true;
  if (key === "category") return CATEGORY_RE.test(value);
  if (key === "account") return ID_RE.test(value);
  return TAG_LIST_RE.test(value);
}

/**
 * Read the criteria a URL carries. An absent OR malformed param is simply not
 * part of the criteria — callers distinguish the two by testing membership,
 * never by trusting the raw string.
 */
export function criteriaFromParams(params: URLSearchParams): SearchCriteria {
  const criteria: SearchCriteria = {};
  for (const key of CRITERIA_KEYS) {
    const value = params.get(key);
    if (value !== null && isValid(key, value)) {
      criteria[key] = key === "q" ? value.trim() : value;
    }
  }
  return criteria;
}

/**
 * Write criteria back onto a URL.
 *
 * `base` (usually the current search) is copied first so params this module
 * does not own survive a rewrite; with no base the result is exactly the
 * criteria, which is what a saved view's restore navigation wants.
 */
export function criteriaToParams(
  criteria: SearchCriteria,
  base?: URLSearchParams,
): URLSearchParams {
  const params = base ? new URLSearchParams(base) : new URLSearchParams();
  for (const key of CRITERIA_KEYS) {
    const value = criteria[key];
    if (value !== undefined) params.set(key, value);
    else params.delete(key);
  }
  return params;
}

/** True when at least one filter is set — the menu has something to carry. */
export function hasCriteria(criteria: SearchCriteria): boolean {
  return CRITERIA_KEYS.some((key) => criteria[key] !== undefined);
}

/** Plain ids, ready for a label lookup — used by `criteriaLabel`. */
interface CriteriaLabelSources {
  categories: { id: number; name: string }[];
  accounts: { id: number; name: string }[];
  tags: { id: number; name: string }[];
}

function idLabel(id: string, items: { id: number; name: string }[]): string | undefined {
  if (!ID_RE.test(id)) return undefined;
  const found = items.find((item) => String(item.id) === id);
  return found?.name;
}

/** One human line for what the search is filtered to, for menus and titles. */
export function criteriaLabel(criteria: SearchCriteria, sources: CriteriaLabelSources): string {
  const parts: string[] = [];
  if (criteria.q !== undefined) parts.push(`“${criteria.q}”`);
  if (criteria.category !== undefined) {
    parts.push(
      criteria.category === "uncategorized"
        ? "Uncategorized"
        : (idLabel(criteria.category, sources.categories) ?? `category ${criteria.category}`),
    );
  }
  if (criteria.account !== undefined) {
    parts.push(idLabel(criteria.account, sources.accounts) ?? `account ${criteria.account}`);
  }
  if (criteria.tag !== undefined) {
    const names = criteria.tag
      .split(",")
      .map((id) => idLabel(id, sources.tags) ?? id)
      .join(", ");
    parts.push(`tags ${names}`);
  }
  return parts.join(" · ");
}

// --- The three creation targets --------------------------------------------

export type CreateTargetId = "rule" | "bill" | "budget";

export const TARGET_PATH: Record<CreateTargetId, string> = {
  rule: "/rules",
  bill: "/bills",
  budget: "/budgets",
};

/**
 * What each editor can actually express. Rules and bills scope on a payee-ish
 * match term plus a category; a budget is a per-category amount, so only the
 * category lands. `account` and `tag` are NOT part of any of the three — the
 * rule/bill/budget schemas have no such column (docs/04, docs/25, docs/08) —
 * which is why the provenance line below says so instead of implying otherwise.
 */
const CARRIED_KEYS: Record<CreateTargetId, readonly CriteriaKey[]> = {
  rule: ["q", "category"],
  bill: ["q", "category"],
  budget: ["category"],
};

/**
 * The keys THIS criteria can be expressed with.
 *
 * `uncategorized` is a real transaction filter, but no target has a column for
 * it: a rule ASSIGNS a category id and a bill scopes by one (both `int`), and
 * a budget has nothing to budget. Carrying it would mean silently dropping it,
 * which is the exact failure this feature exists to avoid.
 */
export function carriedKeys(target: CreateTargetId, criteria: SearchCriteria): CriteriaKey[] {
  const keys = CARRIED_KEYS[target];
  if (criteria.category !== "uncategorized") return [...keys];
  return keys.filter((key) => key !== "category");
}

const FIELD_LABEL: Record<CriteriaKey, string> = {
  q: "search term",
  category: "category",
  account: "account",
  tag: "tags",
};

function droppedLabel(key: CriteriaKey, criteria: SearchCriteria): string {
  if (key === "category" && criteria.category === "uncategorized") {
    return "category (uncategorized)";
  }
  return FIELD_LABEL[key];
}

/**
 * The provenance line shown above a prefilled editor (docs/09).
 *
 * `null` when the search carries nothing at all. Otherwise it names what
 * landed IN the form and, when filters were set that this target cannot
 * express, says so — a prefill that quietly drops half the search is a lie the
 * user only discovers by re-running the search.
 */
export function provenance(
  criteria: SearchCriteria,
  target: CreateTargetId,
  resolveCategory: (value: string) => string,
): string | null {
  const carried = carriedKeys(target, criteria);
  const landed: string[] = [];
  for (const key of carried) {
    const value = criteria[key];
    if (value === undefined) continue;
    if (key === "q") landed.push(`“${value}”`);
    else if (key === "category") landed.push(resolveCategory(value));
  }
  const dropped = CRITERIA_KEYS.filter(
    (key) => criteria[key] !== undefined && !carried.includes(key),
  );
  if (landed.length === 0 && dropped.length === 0) return null;

  const droppedText = dropped.map((key) => droppedLabel(key, criteria)).join(", ");
  if (landed.length > 0) {
    const suffix = dropped.length > 0 ? ` — ${droppedText} not carried into a ${target}.` : "";
    return `Created from search: ${landed.join(" · ")}${suffix}`;
  }
  const canCarry = carried.map((key) => FIELD_LABEL[key]).join(" and ");
  return `Created from search: ${droppedText} — a ${target} carries only the ${canCarry}.`;
}

/** The deep link into a target editor: same criteria, plus the marker. */
export function targetHref(target: CreateTargetId, criteria: SearchCriteria): string {
  const params = criteriaToParams(criteria);
  params.set(PREFILL_PARAM, PREFILL_VALUE);
  return `${TARGET_PATH[target]}?${params.toString()}`;
}

// --- M174: criteria from a SELECTION, not from the search -------------------

/** The fields of a ledger row this derivation reads. */
export interface SelectionRow {
  normalized_payee?: string | null;
  raw_description?: string | null;
  category_id?: number | null;
}

/**
 * Longest common substring of `a` and `b`, as `[start, length]` indexes into
 * `a`. Both sides are folded to lower case first, which is the comparison the
 * rule matcher itself makes (`services/rules_engine.py`: `contains` is
 * `str.lower` on both sides) — so "the pattern the editors will match" and
 * "the pattern this function computes" are the same relation, not two.
 */
function longestCommonRun(a: string, b: string): [number, number] {
  const left = a.toLowerCase();
  const right = b.toLowerCase();
  let bestStart = 0;
  let bestLength = 0;
  const previous = new Array<number>(right.length + 1).fill(0);
  const current = new Array<number>(right.length + 1).fill(0);
  for (let i = 1; i <= left.length; i += 1) {
    for (let j = 1; j <= right.length; j += 1) {
      current[j] = left[i - 1] === right[j - 1] ? previous[j - 1] + 1 : 0;
      if (current[j] > bestLength) {
        bestLength = current[j];
        bestStart = i - bestLength;
      }
    }
    for (let j = 0; j <= right.length; j += 1) previous[j] = current[j];
  }
  return [bestStart, bestLength];
}

/**
 * The payee pattern a create target can carry for a SET of rows (M174).
 *
 * Why not the first row's payee (the naive reading of "create from these"): a
 * statement spells one merchant several ways — the M165 fixture is
 * `GOOGLE *YouTubePremium` vs `Google*Youtubepremium` — and a rule scoped to
 * the first spelling silently misses the sibling, which is exactly the
 * manufactured-duplicate defect M165 fixed one layer down. So:
 *
 *   1. **Identity comes from the shared normalize** (`lib/payeeKey.ts`, the
 *      byte-identical port of the server's `normalize_payee_key`). The rows are
 *      one merchant family only if every row collapses to ONE key; a selection
 *      spanning two merchants has no single pattern and says so instead of
 *      inventing one.
 *   2. **The pattern is the longest common run of the family's payees**, which
 *      by construction is a substring of every selected row — so `contains`
 *      matches all of them, on the rule engine (`str.lower` both sides) and on
 *      the bill matcher (`normalize_payee_key` both sides) alike. A value
 *      rather than a guess: two rows that share only a whitespace-free key
 *      still yield the run that survives the spacing.
 *
 * Never longer than the server's `MAX_TERM_LENGTH` (120) — a prefix of a common
 * run is still a common run, so truncating keeps the guarantee.
 */
export const MAX_SELECTION_PATTERN = 120;

/** The minimum run worth calling a pattern — shorter matches everything. */
const MIN_SELECTION_PATTERN = 3;

function payeeOf(row: SelectionRow): string {
  return (row.normalized_payee ?? "") || (row.raw_description ?? "");
}

export interface SelectionCriteria {
  /** The search criteria the target editors consume — the SAME shape M123 uses. */
  criteria: SearchCriteria;
  /** How many distinct merchants (`normalizePayeeKey`) the rows collapse to. */
  merchants: number;
  /**
   * The pattern carried as `q`, or `null` when the selection cannot express
   * one (two merchants, or a run too short to identify anything). `null` is
   * what makes a target explain itself instead of dead-clicking.
   */
  pattern: string | null;
}

/**
 * Derive create-from-selection criteria from the rows the user ticked.
 *
 * The category rides along only when EVERY selected row agrees on one — the
 * unanimous case is the user saying "these are Groceries", which is what a
 * rule's assignment (or a bill's scope) means; a mixed selection carries no
 * category rather than an arbitrary one.
 */
export function selectionCriteria(rows: readonly SelectionRow[]): SelectionCriteria {
  const payees = rows.map(payeeOf).filter((payee) => payee.trim() !== "");
  const keys = new Set(payees.map((payee) => normalizePayeeKey(payee)).filter((key) => key !== ""));
  const merchants = keys.size;

  let pattern: string | null = null;
  if (merchants === 1 && payees.length > 0) {
    let [start, length] = [0, payees[0].length];
    for (const payee of payees.slice(1)) {
      const [nextStart, nextLength] = longestCommonRun(
        payees[0].slice(start, start + length),
        payee,
      );
      start += nextStart;
      length = nextLength;
      if (length === 0) break;
    }
    const run = payees[0].slice(start, start + length).trim();
    const bounded = run.slice(0, MAX_SELECTION_PATTERN).trim();
    if (bounded.length >= MIN_SELECTION_PATTERN && /[A-Za-z]/.test(bounded)) {
      pattern = bounded;
    }
  }

  const criteria: SearchCriteria = {};
  if (pattern !== null) criteria.q = pattern;

  const categories = new Set(
    rows.map((row) => (row.category_id === undefined || row.category_id === null ? null : row.category_id)),
  );
  const only = [...categories][0];
  if (rows.length > 0 && categories.size === 1 && only !== null && only !== undefined) {
    criteria.category = String(only);
  }

  return { criteria, merchants, pattern };
}

/**
 * Read a target's prefill once on mount: `null` unless this link was built
 * for us, otherwise the criteria to apply. The caller consumes it and stops —
 * a later param change must not clobber what the user already typed.
 */
export function readPrefill(params: URLSearchParams): SearchCriteria | null {
  if (params.get(PREFILL_PARAM) !== PREFILL_VALUE) return null;
  return criteriaFromParams(params);
}

// --- ⌘K ---------------------------------------------------------------------

/**
 * The palette offers "Create from this search" only while the Transactions
 * page (the only surface that OWNS a search) is on screen, so the action can
 * never be a dead click. It hands the open menu back to that page rather than
 * navigating: the live `applied` state may be newer than the URL.
 */
const CREATE_EVENT = "fintrack:create-from-search";

export function requestCreateFromSearch(): void {
  window.dispatchEvent(new CustomEvent(CREATE_EVENT));
}

/** Returns an unsubscribe function. */
export function onCreateFromSearch(handler: () => void): () => void {
  window.addEventListener(CREATE_EVENT, handler);
  return () => window.removeEventListener(CREATE_EVENT, handler);
}
