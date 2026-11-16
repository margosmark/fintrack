// M165 — "already a bill": the ONE payee key and the coverage rule
// (docs/09 §Recurring prompt + §Bills you probably have).
//
// Two questions, both pure, both asked before a card may offer to CREATE a
// bill:
//
//   * WHAT is the same payee? `normalizePayeeKey` — the port of the backend's
//     `services/subscriptions.py::normalize_payee_key`. Mark's three YouTube
//     Premium statement spellings must be ONE key: the detector splitting
//     them is what manufactured the duplicate "Create bill" rows.
//   * WHEN does an existing bill cover a candidate? `coveringBill` — enabled
//     + cadence tolerance + the bill's OWN term rule (any/all over
//     contains/starts_with/exact), so the browser never claims a coverage the
//     server's `/bills/{id}/detail` scan would deny.
import { describe, expect, it } from "vitest";
import type { Bill } from "../src/api/bills";
import { billCoversCadence, coveringBill } from "../src/lib/billCoverage";
import { normalizePayeeKey } from "../src/lib/payeeKey";

/** The three spellings his own ledger carries, all −25.98/month. */
const YOUTUBE_SPELLINGS = [
  "GOOGLE *YouTubePremium",
  "Google *Youtubepremium",
  "Google*Youtubepremium",
];

function bill(overrides: Partial<Bill> = {}): Bill {
  return {
    id: 66,
    user_id: 343,
    name: "Google *Youtubepremium",
    amount: "25.98",
    category_id: null,
    frequency: "monthly",
    next_due_date: "2026-09-28",
    remind_days_before: 3,
    enabled: true,
    match_mode: "contains",
    match_term: "Google *Youtubepremium",
    match_terms: ["Google *Youtubepremium", "Google*Youtubepremium"],
    match_logic: "any",
    amount_tolerance: null,
    ...overrides,
  };
}

const CANDIDATE = { payee: "GOOGLE *YouTubePremium", cadence: "monthly" } as const;

describe("normalizePayeeKey — the ONE grouping key", () => {
  it("folds the three statement spellings of ONE YouTube Premium payee into one key", () => {
    expect([...new Set(YOUTUBE_SPELLINGS.map(normalizePayeeKey))]).toEqual([
      "GOOGLE*YOUTUBEPREMIUM",
    ]);
  });

  it("drops whitespace BEFORE the trailing branch number, and digits alone never name a payee", () => {
    expect(normalizePayeeKey("GROCERY MART #12")).toBe("GROCERYMART");
    expect(normalizePayeeKey("grocerymart 12")).toBe("GROCERYMART");
    expect(normalizePayeeKey("123")).toBe("");
    expect(normalizePayeeKey("#7")).toBe("");
  });

  it("is idempotent and folds nothing but whitespace and digits (no fuzzy match)", () => {
    for (const spelling of YOUTUBE_SPELLINGS) {
      const key = normalizePayeeKey(spelling);
      expect(normalizePayeeKey(key)).toBe(key);
    }
    // Punctuation still separates two payees — the spacing rule is not a
    // licence to guess.
    expect(normalizePayeeKey("A *B")).not.toBe(normalizePayeeKey("AB"));
  });
});

describe("billCoversCadence — the tolerance (docs/09)", () => {
  it("a monthly bill covers a monthly candidate however its gaps fall", () => {
    // The detector fits `monthly` to gaps of 26–32 days, so a bill that
    // records `monthly` covers a charge that really runs on a 28-day cycle.
    expect(billCoversCadence("monthly", "monthly")).toBe(true);
    // A four_weekly fit IS a 28-day cycle — a month by another name.
    expect(billCoversCadence("four_weekly", "monthly")).toBe(true);
  });

  it("a cadence with no bill equivalent is never claimed as covered", () => {
    expect(billCoversCadence("weekly", "monthly")).toBe(false);
    expect(billCoversCadence("biweekly", "monthly")).toBe(false);
    // `one_time` is not a recurring stream.
    expect(billCoversCadence("monthly", "one_time")).toBe(false);
  });
});

describe("coveringBill — never offer to create what exists", () => {
  it("covers every statement spelling when ANY term matches", () => {
    for (const spelling of YOUTUBE_SPELLINGS) {
      expect(coveringBill([bill()], { payee: spelling, cadence: "monthly" })).not.toBeNull();
    }
  });

  it("answers null for a payee no bill tracks — the CTA stays", () => {
    expect(coveringBill([bill()], { payee: "SP REITMANS", cadence: "monthly" })).toBeNull();
    expect(coveringBill([], CANDIDATE)).toBeNull();
  });

  it("ignores a DISABLED bill (a paused reminder does not cover anything)", () => {
    expect(coveringBill([bill({ enabled: false })], CANDIDATE)).toBeNull();
  });

  it("respects ALL-mode: every term must hit, or the bill does not cover", () => {
    const all = bill({
      match_logic: "all",
      match_terms: ["GOOGLE *YOUTUBEPREMIUM", "NETFLIX"],
    });
    expect(coveringBill([all], CANDIDATE)).toBeNull();
    const both = bill({
      match_logic: "all",
      match_terms: ["GOOGLE *YOUTUBEPREMIUM", "YOUTUBEPREMIUM"],
    });
    expect(coveringBill([both], CANDIDATE)).not.toBeNull();
  });

  it("honours exact and starts_with modes the way the server's matcher does", () => {
    expect(
      coveringBill([bill({ match_mode: "exact", match_terms: ["GOOGLE *YOUTUBEPREMIUM"] })], CANDIDATE),
    ).not.toBeNull();
    // `exact` is NOT a substring test: a shortened term does not cover.
    expect(coveringBill([bill({ match_mode: "exact", match_terms: ["YOUTUBEPREMIUM"] })], CANDIDATE)).toBeNull();
    expect(
      coveringBill([bill({ match_mode: "starts_with", match_terms: ["GOOGLE"] })], CANDIDATE),
    ).not.toBeNull();
  });

  it("an empty rule covers nothing (a rule with no terms is no claim)", () => {
    expect(coveringBill([bill({ match_terms: [], match_term: "" })], CANDIDATE)).toBeNull();
  });

  it("falls back to the pre-M121 single match_term when no term list arrived", () => {
    const legacy = bill({ match_terms: [], match_term: "Google*Youtubepremium" });
    expect(coveringBill([legacy], CANDIDATE)).not.toBeNull();
  });

  it("the first covering bill wins, so the line names ONE record", () => {
    const first = bill({ id: 66, name: "YouTube Premium" });
    const second = bill({ id: 67, name: "Google (duplicate)" });
    expect(coveringBill([first, second], CANDIDATE)?.id).toBe(66);
  });
});
