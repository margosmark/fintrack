// @vitest-environment node
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import { updateProjection } from "../src/api/taxProjection";
import { tokens } from "../src/api/client";

/**
 * TaxProjection partial save (docs/29 F9): PATCH /taxes/projection keeps
 * stored values for omitted params, so the API module must serialize ONLY
 * the fields the caller provides, with money passed through as strings.
 */
describe("updateProjection payload", () => {
  let fetchMock: Mock;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      statusText: "OK",
      headers: { get: () => null },
      json: async () => ({ year: 2026, province: "ON" }),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("window", { location: { assign: vi.fn() } });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends only the provided fields as query params", async () => {
    tokens.storeAccess("t");
    await updateProjection({ year: 2026, province: "BC", deductions: "1200.00" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://test.local");
    expect(url.pathname).toBe("/api/v1/taxes/projection");
    expect(url.searchParams.get("year")).toBe("2026");
    expect(url.searchParams.get("province")).toBe("BC");
    expect(url.searchParams.get("deductions")).toBe("1200.00");
    // Omitted edited-fields keep stored values — absent from the request.
    expect(url.searchParams.has("estimated_income")).toBe(false);
    expect(url.searchParams.has("capital_gains_override")).toBe(false);
    expect(url.searchParams.has("dividend_income_override")).toBe(false);
    expect(fetchMock.mock.calls[0][1]?.method).toBe("PATCH");
  });

  it("clears an override by sending null, and sends every edited field", async () => {
    await updateProjection({
      year: 2027,
      province: "ON",
      estimated_income: "85000",
      deductions: "0",
      capital_gains_override: null,
      dividend_income_override: "310.50",
    });

    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://test.local");
    expect(url.searchParams.get("estimated_income")).toBe("85000");
    expect(url.searchParams.get("deductions")).toBe("0");
    expect(url.searchParams.get("capital_gains_override")).toBe("");
    expect(url.searchParams.get("dividend_income_override")).toBe("310.50");
  });
});
