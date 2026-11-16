// M139 — the display mask's session contract + the text masker (docs/09
// § Display masking).
//
// The three notification bodies below are COPIED from the shapes
// `backend/app/api/notifications.py` actually emits (docs/35), so the text
// masker is pinned against the real producer, not a guess.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  MASK_SESSION_KEY,
  MASK_TEXT,
  applyDemoMaskDefault,
  isMasked,
  maskAmountsInText,
  maskForYear,
  initMaskForSession,
  setMasked,
  toggleMasked,
} from "../src/lib/mask";
import { fmtSignedPct, fmtSignedPctBookCost } from "../src/lib/impact";

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
});

afterEach(() => {
  sessionStorage.clear();
  initMaskForSession();
});

describe("mask session contract", () => {
  it("starts unmasked and flips instantly", () => {
    expect(isMasked()).toBe(false);
    toggleMasked();
    expect(isMasked()).toBe(true);
    toggleMasked();
    expect(isMasked()).toBe(false);
  });

  it("mirrors the state into sessionStorage (per-tab), never localStorage", () => {
    setMasked(true);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
    expect(localStorage.getItem(MASK_SESSION_KEY)).toBeNull();
    setMasked(false);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
  });

  it("logout/boot kills the key even when it was left on", () => {
    setMasked(true);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
    initMaskForSession();
    expect(isMasked()).toBe(false);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
  });

  it("boots the demo default masked, one click from removal", () => {
    initMaskForSession({ demoDefault: true });
    expect(isMasked()).toBe(true);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
    setMasked(false);
    expect(isMasked()).toBe(false);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
  });

  it("lets the demo default stomp nothing the user already chose", () => {
    // The default is a default, not a lock: a user who un-masked this session
    // keeps their choice when the demo tenant's boot default lands.
    setMasked(false);
    applyDemoMaskDefault();
    expect(isMasked()).toBe(false);
  });

  it("applies the demo default when the user has not chosen", () => {
    initMaskForSession();
    applyDemoMaskDefault();
    expect(isMasked()).toBe(true);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
  });

  it("restores NOTHING from a stale key (restore-on-load OFF)", () => {
    // A tab that crashed with the mask on leaves the key behind. Boot must not
    // resurrect it: the user re-masks deliberately (PM ruling).
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    initMaskForSession();
    expect(isMasked()).toBe(false);
  });

  it("keeps the state on a repeated set (no-op guard)", () => {
    // The setter bails when the value is unchanged, so a chip render cannot
    // spam its subscriber list — pinned here, exercised for real through
    // useMasked() in tests/m139MaskChip.test.tsx.
    setMasked(true);
    setMasked(true);
    expect(isMasked()).toBe(true);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
  });
});

describe("maskForYear — the year carve-out", () => {
  it("masks the current year and anything forward", () => {
    const now = new Date().getFullYear();
    expect(maskForYear(true, now)).toBe(true);
    expect(maskForYear(true, now + 3)).toBe(true);
  });

  it("leaves years-old analytics readable", () => {
    const now = new Date().getFullYear();
    expect(maskForYear(true, now - 1)).toBe(false);
    expect(maskForYear(true, 2019)).toBe(false);
  });

  it("is inert when the mask is off", () => {
    expect(maskForYear(false, new Date().getFullYear())).toBe(false);
  });
});

describe("maskAmountsInText — the notification bodies", () => {
  const unmasked = (body: string): string => {
    initMaskForSession();
    return maskAmountsInText(body);
  };
  const masked = (body: string): string => {
    setMasked(true);
    return maskAmountsInText(body);
  };

  it("passes bodies through untouched while unmasked", () => {
    expect(unmasked("Spent 123.45 of 500.00 CAD (88% of limit; threshold 75%)")).toBe(
      "Spent 123.45 of 500.00 CAD (88% of limit; threshold 75%)",
    );
  });

  it("masks a budget alert's amounts and keeps its percentages", () => {
    expect(masked("Spent 123.45 of 500.00 CAD (88% of limit; threshold 75%)")).toBe(
      `Spent ${MASK_TEXT} of ${MASK_TEXT} CAD (88% of limit; threshold 75%)`,
    );
  });

  it("masks a bill's amount and keeps its due date", () => {
    expect(masked("Due 2026-10-15 — 45.00 CAD")).toBe(
      `Due 2026-10-15 — ${MASK_TEXT} CAD`,
    );
  });

  it("masks both amounts in a tax-loss body", () => {
    expect(
      masked("Unrealized loss 432.10 CAD in TFSA (estimated tax benefit 88.00 CAD)"),
    ).toBe(
      `Unrealized loss ${MASK_TEXT} CAD in TFSA (estimated tax benefit ${MASK_TEXT} CAD)`,
    );
  });

  it("keeps the superficial-loss risk suffix and the account name", () => {
    expect(
      masked("Unrealized loss 432.10 CAD in TFSA (estimated tax benefit 88.00 CAD) — superficial-loss risk"),
    ).toContain("superficial-loss risk");
    expect(masked("Unrealized loss 432.10 CAD in TFSA")).toContain("TFSA");
  });

  it("masks $-prefixed amounts", () => {
    expect(masked("Balance is $1,234.56 today")).toBe(`Balance is ${MASK_TEXT} today`);
    expect(masked("Worth $12")).toBe(`Worth ${MASK_TEXT}`);
  });

  it("keeps bare integers that are counts, not amounts", () => {
    // "3 of 9 rows" is a count; masking it would be theatre.
    expect(masked("Matched 3 of 9 rows")).toBe("Matched 3 of 9 rows");
  });

  it("masks negative amounts, keeping the currency word", () => {
    expect(masked("Net -42.10 CAD this month")).toBe(`Net ${MASK_TEXT} CAD this month`);
    expect(masked("Net -42.10 this month")).toBe(`Net ${MASK_TEXT} this month`);
  });
});

// M139 — the operator's percentage ruling (docs/09 § Display masking, D2).
//
// Percentages are PROPORTIONAL, not absolute: they say nothing about scale, so
// a shoulder-surfer learns nothing new from one. Amounts mask; percentages do
// not. The single carve-out is a percentage DERIVED FROM BOOK COST — showing
// "%+4.63" beside a masked market value hands back the very scale the mask
// removed — so those call sites opt in explicitly.
describe("percentages stay visible; amounts mask (operator ruling)", () => {
  it("fmtSignedPct is visible by default, even in a masked session", () => {
    initMaskForSession({ demoDefault: true });
    expect(isMasked()).toBe(true);
    // The ruled-visible set: budget %, donut slice %, spending-change %,
    // growth/period delta %, TWR/MWR %.
    expect(fmtSignedPct(12.3456)).toBe("+12.35%");
    expect(fmtSignedPct(-3.5)).toBe("-3.50%");
    expect(fmtSignedPct(0)).toBe("0.00%");
  });

  it("keeps null percentages at the honest em dash rather than 0.00%", () => {
    initMaskForSession({ demoDefault: true });
    expect(fmtSignedPct(null)).toBe("—");
    expect(fmtSignedPct(undefined)).toBe("—");
    expect(fmtSignedPct(Number.NaN)).toBe("—");
  });

  it("the book-cost carve-out FOLLOWS the session mask", () => {
    // Investments' position-level "Your gain %", and SecurityDetail's
    // book-vs-market gain % — masked while the mask is on...
    initMaskForSession({ demoDefault: true });
    expect(fmtSignedPctBookCost(4.63)).toBe(MASK_TEXT);
    // ...including a legitimate 0.00%, which would otherwise leak nothing.
    expect(fmtSignedPctBookCost(0)).toBe(MASK_TEXT);
  });

  it("the carve-out REVEALS again when the mask is off", () => {
    // It must track the session, not be a permanent bullet — otherwise the
    // Investments % column is just blank data for an unmasked user.
    initMaskForSession();
    expect(isMasked()).toBe(false);
    expect(fmtSignedPctBookCost(4.63)).toBe("+4.63%");
    expect(fmtSignedPctBookCost(-3.5)).toBe("-3.50%");
  });

  it("the carve-out keeps the honest em dash on null/NaN", () => {
    initMaskForSession({ demoDefault: true });
    expect(fmtSignedPctBookCost(null)).toBe("—");
    expect(fmtSignedPctBookCost(Number.NaN)).toBe("—");
  });
});
