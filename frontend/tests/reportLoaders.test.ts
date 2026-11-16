// Server-first report loaders (docs/33 §Server-first computing; M160-R2,
// t_85311c34).
//
// Before M160-R2 these pins drove the CLIENT computes' input loaders: the
// browser downloaded trades, securities, prices, the ledger and fx and
// re-derived each report, so the contract under test was "paginate every
// input to the full server population". t_85311c34 re-pointed the three
// panels (Dividends / Sankey / Total return) at their routes and deleted the
// `lib/reportClient.ts` shim, so the contract is now the opposite one: ONE
// request per report, to the pinned path, payload passed through VERBATIM —
// no ledger download, no browser re-derivation (docs/33 rule 5).
//
// What these pins protect:
//   1. path + query — a typo here is a permanently blank panel;
//   2. pass-through — the numbers on screen are the server's own strings,
//      never re-parsed or re-rounded in the browser (M65/M105 honesty);
//   3. the `{detail, code}` envelope reaching `ApiError`, which is what
//      `errorHint` renders — the same contract the retired engine's `rethrow`
//      used to guarantee (FIND-4b);
//   4. the local 422 for an inverted range, byte-identical to the server's
//      `_check_range` and thrown BEFORE a round trip (the route re-checks).
// The NUMBERS are pinned where they are computed now:
// `backend/tests/test_dividends.py`, `test_reports_sankey.py`,
// `test_total_return.py`, plus the frozen goldens replayed against the routes
// in `backend/tests/test_m160_replay_s2c.py` (dividends) and
// `backend/tests/test_m160_replay_m77.py` (sankey + total-return).

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import {
  getCashflowSankey,
  getDividendReport,
  getTotalReturn,
} from "../src/api/reports";

/** Minimal Response shape covering exactly what client.ts reads. */
function fakeResponse(opts: { status?: number; body?: unknown }): Response {
  const status = opts.status ?? 200;
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: "OK",
    headers: { get: () => null },
    json: async () => opts.body,
  } as unknown as Response;
}

let fetchMock: Mock;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Every URL the loaders asked for, in order. */
function urls(): string[] {
  return fetchMock.mock.calls.map(([url]) => String(url));
}

describe("report loaders — one thin read per report (docs/33 rule 5)", () => {
  it("getDividendReport() asks /reports/dividends exactly once, no year param", async () => {
    const body = {
      year: 2026,
      available_years: [2026, 2025],
      items: [
        {
          security_id: 7,
          symbol: "RY.TO",
          name: "Royal Bank of Canada",
          dividend_cad: "100.00",
          distribution_cad: "0.00",
          roc_cad: "0.00",
          total_cad: "100.00",
          payment_count: 2,
          last_payment_date: "2026-06-30",
        },
      ],
      by_month: [{ month: "2026-01", total_cad: "0.00" }],
      totals: {
        dividend_cad: "100.00",
        distribution_cad: "0.00",
        roc_cad: "0.00",
        total_cad: "100.00",
      },
      ttm_total_cad: "1822.93",
      unresolved_fx: 0,
    };
    fetchMock.mockResolvedValue(fakeResponse({ body }));

    const report = await getDividendReport();

    // One request, to the route, with no defaulted `year=undefined` junk.
    expect(urls()).toEqual(["/api/v1/reports/dividends"]);
    // Verbatim pass-through: the panel renders the server's strings.
    expect(report).toEqual(body);
    expect(report.ttm_total_cad).toBe("1822.93");
  });

  it("getDividendReport(2025) carries the year on the query string", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: { year: 2025 } }));

    await getDividendReport(2025);

    expect(urls()).toEqual(["/api/v1/reports/dividends?year=2025"]);
  });

  it("getCashflowSankey asks /reports/cashflow-sankey with the panel's range", async () => {
    const body = {
      from: "2026-01-01",
      to: "2026-01-31",
      columns: ["source", "total", "destination"],
      nodes: [
        { id: "src:cat:1", column: "source", label: "Salary", amount_cad: "3000.00", group: "income" },
      ],
      links: [{ source: "src:cat:1", target: "total:in", amount_cad: "3000.00" }],
      totals: {
        income_cad: "3000.00",
        expenses_cad: "0.00",
        invested_cad: "0.00",
        saved_cad: "3000.00",
        drawn_cad: "0.00",
        money_in_cad: "3000.00",
      },
    };
    fetchMock.mockResolvedValue(fakeResponse({ body }));

    const report = await getCashflowSankey("2026-01-01", "2026-01-31");

    expect(urls()).toEqual([
      "/api/v1/reports/cashflow-sankey?from=2026-01-01&to=2026-01-31",
    ]);
    expect(report).toEqual(body);
  });

  it("getTotalReturn keeps the 8-decimal strings and the nulls verbatim", async () => {
    const body = {
      from: "2026-01-01",
      to: "2026-09-01",
      status: "ok",
      start_value_cad: "0.00",
      end_value_cad: "909.00",
      net_flow_cad: "800.00",
      income_cad: "0.00",
      twr_pct: "10.90000000",
      mwr_pct: "12.46915167",
      valuation_days: 90,
      active_days: 89,
      flow_days: 2,
      account_count: 1,
    };
    fetchMock.mockResolvedValue(fakeResponse({ body }));

    const report = await getTotalReturn("2026-01-01", "2026-09-01");

    expect(urls()).toEqual([
      "/api/v1/reports/total-return?from=2026-01-01&to=2026-09-01",
    ]);
    // The wire string is the contract: rendered by slicing, never by parsing.
    expect(report.twr_pct).toBe("10.90000000");
    expect(report.twr_pct?.split(".")[1]).toHaveLength(8);
  });

  it("a null return stays null — the loader never turns it into a zero", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({ body: { status: "no_data", twr_pct: null, mwr_pct: null } }),
    );

    const report = await getTotalReturn("2026-01-01", "2026-09-01");

    expect(report.twr_pct).toBeNull();
    expect(report.mwr_pct).toBeNull();
  });

  it("the three loaders never download the ledger they used to re-derive", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ body: {} }));
    await Promise.all([
      getDividendReport(2026),
      getCashflowSankey("2026-01-01", "2026-01-31"),
      getTotalReturn("2026-01-01", "2026-09-01"),
    ]);

    // The pre-M160-R2 loaders pulled /trades, /transactions, /securities,
    // /prices and /fx into the browser. Rule 5 forbids exactly that.
    const allowed = /^\/api\/v1\/reports\/(dividends|cashflow-sankey|total-return)\b/;
    expect(urls().every((url) => allowed.test(url))).toBe(true);
    expect(urls()).toHaveLength(3);
  });
});

describe("report loaders — the {detail, code} envelope (docs/08)", () => {
  const loaders = [
    ["getDividendReport", () => getDividendReport(2026)],
    ["getCashflowSankey", () => getCashflowSankey("2026-01-01", "2026-01-31")],
    ["getTotalReturn", () => getTotalReturn("2026-01-01", "2026-09-01")],
  ] as const;

  it.each(loaders)("%s surfaces a route failure as ApiError with its code", async (_name, call) => {
    // Exactly the body a 410 under SERVER_REPORTS_DEPRECATED (or any domain
    // failure) sends — `errorHint` branches on `code`, never on `detail`.
    fetchMock.mockResolvedValue(
      fakeResponse({
        status: 500,
        body: { detail: "dividends: trade 1 references unknown security 999", code: "internal_error" },
      }),
    );

    await expect(call()).rejects.toMatchObject({
      name: "ApiError",
      status: 500,
      code: "internal_error",
    });
  });

  const inverted = [
    ["getCashflowSankey", () => getCashflowSankey("2026-01-31", "2026-01-01")],
    ["getTotalReturn", () => getTotalReturn("2026-09-01", "2026-01-01")],
  ] as const;

  it.each(inverted)(
    "%s answers an inverted range with the server's 422 without a round trip",
    (_name, call) => {
      // `from > to` is rejected LOCALLY — synchronously, before a promise
      // even exists — in the same envelope `_check_range` would send (the
      // route re-checks it anyway). Same shape as `getNetWorth`.
      let error: unknown;
      try {
        call();
      } catch (e) {
        error = e;
      }
      expect(error).toMatchObject({
        name: "ApiError",
        status: 422,
        code: "invalid_date_range",
      });
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );
});
