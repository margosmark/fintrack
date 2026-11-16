import { describe, it, expect } from "vitest";
import { formatCad, formatCadCompact, formatMoney } from "../src/lib/format";

describe("formatCad", () => {
  it("formats a Decimal-as-string CAD amount en-CA", () => {
    expect(formatCad("1234.5")).toBe("$1,234.50");
  });

  it("accepts numeric inputs", () => {
    expect(formatCad(0)).toBe("$0.00");
    expect(formatCad(-42.1)).toBe("-$42.10");
  });

  it("renders an em dash for null, undefined, or non-numeric input", () => {
    expect(formatCad(null)).toBe("—");
    expect(formatCad(undefined)).toBe("—");
    expect(formatCad("not-a-number")).toBe("—");
  });
});

describe("formatCadCompact", () => {
  it("uses compact notation for large magnitudes (>= 10_000)", () => {
    // en-CA compact currency: "$12.3K" (or "CA$12.3K"); symbol + K are stable.
    expect(formatCadCompact(12345)).toMatch(/\$12\.3K/i);
  });

  it("uses standard 2-dp currency for small magnitudes", () => {
    expect(formatCadCompact(345)).toBe("$345.00");
  });
});

describe("formatMoney", () => {
  it("formats native currency en-CA", () => {
    expect(formatMoney("99.9", "USD")).toBe("US$99.90");
  });

  it("falls back to amount + code for an invalid currency code", () => {
    // "ZZ" is not a 3-letter ISO 4217 code, so Intl throws and we fall back.
    expect(formatMoney("5", "ZZ")).toBe("5 ZZ");
  });

  it("renders a dash for null (M145 no-honest-figure), never a fabricated $0.00", () => {
    // Number(null) === 0, so an unguarded formatMoney would print "$0.00" for
    // data that never claimed zero — the exact fabrication M145 forbids.
    expect(formatMoney(null, "CAD", false)).toBe("—");
    expect(formatMoney(undefined, "USD", false)).toBe("—");
  });
});
