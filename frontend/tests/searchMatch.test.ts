import { describe, expect, it } from "vitest";
import {
  SCORE_THRESHOLD,
  matchRows,
  matchScore,
  normalizeText,
  searchPage,
} from "../src/lib/searchMatch";

/**
 * Golden vectors captured from the LIVE pg_trgm (example.internal Postgres) before the
 * server-side scan was removed (docs/33 S1). Each entry: [text, query,
 * pg greatest(similarity, word_similarity)]. The port must agree on the
 * threshold decision for every pair; raw values may differ slightly because
 * word-window extraction is re-derived rather than bit-copied.
 */
const PG_GOLDEN: ReadonlyArray<readonly [string, string, number]> = [
  ["Metro Groceries", "metro", 1],
  ["metro", "metro", 1],
  ["METRO", "metro", 1],
  ["Metro Groceries", "metrop", 0.71428573],
  ["FreshCo Oakville", "metro", 0],
  ["Metro Groceries Oakville", "metro", 1],
  ["Uber Eats", "ubereats", 0.5833333],
  ["Uber Eats", "uber", 1],
  ["Uber Eats", "zipcar", 0],
  ["Canadian Tire", "canadian", 1],
  ["Shell Hwy 401", "shell station", 0.46153846],
  ["Shell Hwy 401", "shell", 1],
  ["Tim Hortons", "tim hortons", 1],
  ["Tim Hortons", "hospital", 0.22222222],
  ["No Frills", "nofrills", 0.5833333],
  ["No Frills", "no frills", 1],
  ["Costco Wholesale", "cost", 0.8],
  ["Pay: NETFLIX.COM", "netflix", 1],
  ["Tangerine Savings", "tangerine bank", 0.6666667],
];

describe("pg_trgm decision parity", () => {
  it.each(PG_GOLDEN)("match decision for %j ~ %j", (text, query, pgScore) => {
    const mine = matchScore(query, text);
    expect(mine).toBeGreaterThanOrEqual(0);
    expect(mine).toBeLessThanOrEqual(1);
    expect(mine >= SCORE_THRESHOLD).toBe(pgScore >= SCORE_THRESHOLD);
  });

  it("rejects unrelated payees", () => {
    expect(matchScore("metro", "FreshCo Oakville")).toBeLessThan(SCORE_THRESHOLD);
    expect(matchScore("zipcar", "Uber Eats")).toBeLessThan(SCORE_THRESHOLD);
  });
});

describe("normalizeText", () => {
  it("folds case and punctuation", () => {
    expect(normalizeText("Pay: NETFLIX.COM")).toBe("pay netflix com");
    expect(normalizeText("  Métro   Groceries!")).toBe("métro groceries");
  });
});

describe("matchRows", () => {
  const rows = [
    { id: 1, payee: "Metro Pharmacy", category_id: 3 },
    { id: 2, payee: "Metro Grocery", category_id: 2 },
    { id: 3, payee: "Metro Gas Bar", category_id: null },
    { id: 4, payee: "FreshCo Oakville", category_id: 2 },
  ];

  it("ranks the closest payee first (old test_similar_ranks_grocery_first)", () => {
    const hits = matchRows(rows, "metro groc");
    expect(hits.length).toBeGreaterThanOrEqual(2);
    expect(hits[0].row.payee).toBe("Metro Grocery");
    const scores = hits.map((h) => h.score);
    expect(scores).toEqual([...scores].sort((a, b) => b - a));
  });

  it("categorizedOnly excludes uncategorized rows (old test_similar_excludes_uncategorized)", () => {
    const hits = matchRows(rows, "metro", { categorizedOnly: true });
    expect(hits.map((h) => h.row.id)).not.toContain(3);
    expect(hits.every((h) => h.row.category_id != null)).toBe(true);
    // Without the flag, uncategorized rows still match (search semantics).
    expect(matchRows(rows, "metro").map((h) => h.row.id)).toContain(3);
  });

  it("excludeId drops the anchor row (old test_similar_exclude_id_and_tenancy)", () => {
    const hits = matchRows(rows, "metro", { excludeId: 2 });
    expect(hits.map((h) => h.row.id)).not.toContain(2);
    expect(hits.length).toBeGreaterThan(0);
  });

  it("caps at limit and drops below-threshold noise", () => {
    expect(matchRows(rows, "metro", { limit: 2 })).toHaveLength(2);
    expect(matchRows(rows, "qqzzxx").map((h) => h.row.id)).toEqual([]);
    expect(matchRows(rows, "")).toEqual([]);
  });

  it("tie-breaks equal scores by id desc (API ordering)", () => {
    const twin = [
      { id: 10, payee: "Same Payee", category_id: 1 },
      { id: 11, payee: "Same Payee", category_id: 1 },
    ];
    expect(matchRows(twin, "same payee").map((h) => h.row.id)).toEqual([11, 10]);
  });

  /**
   * M119: the Similar popover ranks with `categorizedFirst`, so the ONE row
   * whose category can be applied is on top even when every sibling ties at
   * 1.0 — the id-desc tiebreak alone would land it last. The default stays
   * `score desc, id desc` because docs/33 S5 pins that for the search page.
   */
  it("categorizedFirst lifts an equal-scoring categorized row to the top (M119)", () => {
    const family = [
      { id: 7148, payee: "Same Payee", category_id: null },
      { id: 6919, payee: "Same Payee", category_id: 7 },
      { id: 7140, payee: "Same Payee", category_id: null },
    ];
    expect(matchRows(family, "same payee").map((h) => h.row.id)).toEqual([7148, 7140, 6919]);
    expect(
      matchRows(family, "same payee", { categorizedFirst: true }).map((h) => h.row.id),
    ).toEqual([6919, 7148, 7140]);
    // …and the ranking runs BEFORE the limit is applied, so a categorized
    // row that id-desc would have dropped still surfaces.
    expect(
      matchRows(family, "same payee", { categorizedFirst: true, limit: 1 }).map((h) => h.row.id),
    ).toEqual([6919]);
  });
});

/**
 * Live pg_trgm scores for the two pairs that caught S1's heuristic lying
 * (docs/33 S5): the whole-string-padding approximation scored `gro` at 0.273
 * and `payroll` at 0.2, dropping 49 and 10 rows the server ranked. Values are
 * `word_similarity` as computed by postgres `trgm_op.c` on the demo tenant —
 * exact, not approximate, because both sit near the 0.3 threshold.
 */
describe("live pg_trgm scores the old heuristic got wrong (S5)", () => {
  it.each([
    ["GROCERY MART #12", "gro", 0.75],
    ["CARD PAYMENT THANKYOU", "payroll", 0.375],
  ])("%j ~ %j = %j on live postgres", (text, query, live) => {
    expect(matchScore(query, text)).toBeCloseTo(live, 6);
    expect(matchScore(query, text) >= SCORE_THRESHOLD).toBe(true);
  });
});

describe("searchPage (the removed GET /transactions?q= envelope)", () => {
  const rows = [
    { id: 1, payee: "Metro Grocery", category_id: 2 },
    { id: 5, payee: "Metro Pharmacy", category_id: 3 },
    { id: 9, payee: "FreshCo Oakville", category_id: 1 },
    { id: 12, payee: "Metro Gas Bar", category_id: null },
  ];

  it("defaults to the server's pagination (limit 50, offset 0)", () => {
    const page = searchPage(rows, "metro");
    expect(page.limit).toBe(50);
    expect(page.offset).toBe(0);
    // `total` is the HIT count (the old `paginate()` counted the thresholded
    // scope), and FreshCo is below threshold.
    expect(page.total).toBe(3);
    expect(page.items.map((r) => r.id)).toEqual([12, 5, 1]);
  });

  it("windows the ordered hits without changing `total`", () => {
    const page = searchPage(rows, "metro", { limit: 2, offset: 2 });
    expect(page.total).toBe(3);
    expect(page.items.map((r) => r.id)).toEqual([1]);
    expect(searchPage(rows, "metro", { limit: 2, offset: 4 }).items).toEqual([]);
  });

  it("keeps uncategorized rows in the search population", () => {
    expect(searchPage(rows, "metro").items.map((r) => r.id)).toContain(12);
  });

  it("an empty query hits nothing (the endpoint's min_length=1 made it unreachable)", () => {
    expect(searchPage(rows, "")).toMatchObject({ items: [], total: 0 });
  });

  it("a query past the threshold for nothing returns an empty envelope", () => {
    expect(searchPage(rows, "zzzzqqq")).toMatchObject({ items: [], total: 0 });
  });
});
