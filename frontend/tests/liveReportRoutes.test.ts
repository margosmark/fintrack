// M160-R2 live probe (t_85311c34) — the three re-pointed report routes, on the
// deployed stack, under the demo account.
//
// The panels now read `GET /reports/dividends`, `/reports/cashflow-sankey` and
// `/reports/total-return` through `api/reports.ts`, so this probe runs the
// three REAL loaders against the live origin and, with the same session, fetches
// the three routes directly — the pair must be byte-identical (a loader that
// ever reshapes a payload, or a route that stops answering the way the panel
// expects, fails here rather than in production) and both sides must carry
// rows, because a diff of two empty bodies is a meaningless green.
//
// It is env-gated and never runs in the normal suite (a unit test must not
// reach a live URL — AGENTS §6):
//
//   LIVE_REPORTS=1 npx vitest run tests/liveReportRoutes.test.ts
//   LIVE_ORIGIN=https://... to aim somewhere other than the deployed stack
//
// The token comes from `POST /auth/demo` and is never logged or written.

import { beforeAll, describe, expect, it } from "vitest";

import { deepDiff, type Diff } from "../scripts/parity/deepDiff";
import { tokens } from "../src/api/client";
import { getCashflowSankey, getDividendReport, getTotalReturn } from "../src/api/reports";

const LIVE = process.env.LIVE_REPORTS === "1";
const ORIGIN = process.env.LIVE_ORIGIN ?? "https://fintrac.princessandmark.com";
const API = `${ORIGIN}/api/v1`;

/** The three ranges the panels themselves send (Investments pins BENCHMARK_*;
 *  Reports' default preset is YTD = Jan 1 -> today). */
const now = new Date();
const YEAR = now.getFullYear();
const pad = (n: number) => String(n).padStart(2, "0");
const TODAY = `${YEAR}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
const YTD_FROM = `${YEAR}-01-01`;
const TOTAL_RETURN_FROM = "2026-01-01";
const TOTAL_RETURN_TO = "2026-09-01";

/** Summarise a diff for the failure message without dumping whole reports. */
function show(diffs: Diff[]): string {
  return diffs
    .map((d) => `  ${d.path} [${d.kind}] expected=${d.expected} actual=${d.actual}`)
    .join("\n");
}

describe.runIf(LIVE)("M160-R2 live probe — the three re-pointed report routes", () => {
  /** Absolute-URL fetch, used for the server side and for logging in. */
  let raw: typeof fetch;
  let token = "";

  beforeAll(async () => {
    raw = globalThis.fetch.bind(globalThis);

    // The client builds every URL as the browser-relative `/api/v1/...`, which
    // Node's fetch refuses to parse. Resolve it against the live origin — the
    // same thing a browser does — so the loaders under test run UNMODIFIED.
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
      raw(
        typeof input === "string" && input.startsWith("/") ? `${ORIGIN}${input}` : input,
        init,
      )) as typeof fetch;

    const res = await raw(`${API}/auth/demo`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: "{}",
    });
    expect(res.status, "POST /auth/demo").toBe(200);
    const body = (await res.json()) as { access_token?: string };
    token = body.access_token ?? "";
    expect(token.length, "POST /auth/demo access_token").toBeGreaterThan(0);
    tokens.storeAccess(token);
  }, 60_000);

  /** One server route, authenticated with the same demo session. */
  async function server(path: string): Promise<unknown> {
    const res = await raw(`${API}${path}`, {
      headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
    });
    expect(res.status, `GET ${path}`).toBe(200);
    return res.json();
  }

  /** A diff of two EMPTY reports is a hollow green: pin the shape first, so a
   *  tenant with no rows can never make this suite look like proof. */
  function nonEmpty(body: unknown, what: string, probe: (b: Record<string, unknown>) => number) {
    expect(body, `${what}: not an object`).toBeTypeOf("object");
    const n = probe(body as Record<string, unknown>);
    console.log(`      ${what}: ${n} non-empty fields/rows`);
    expect(n, `${what}: ${n} rows — nothing to prove the route serves`).toBeGreaterThan(0);
    return body;
  }

  /** Negative control: a diff of two DIFFERENT bodies must be non-empty, or
   *  the three greens below would prove nothing (the harness ships the same
   *  self-test as `parity.ts --self-test`). */
  it("the diff machinery actually catches a mismatch", () => {
    expect(deepDiff({ a: "1.00", b: [1, 2] }, { a: "1.01", b: [1, 2] })).toHaveLength(1);
    expect(deepDiff({ a: "1.00" }, { a: 1 })).toHaveLength(1);
    expect(deepDiff({ a: "1.00" }, { a: "1.00", b: null })).toHaveLength(1);
    expect(deepDiff({ a: "1.00" }, { a: "1.00" })).toHaveLength(0);
  });

  it("GET /reports/dividends == getDividendReport() (default year)", async () => {
    const [srv, viaLoader] = await Promise.all([
      server("/reports/dividends"),
      getDividendReport(),
    ]);
    const rows = (b: Record<string, unknown>) =>
      (b.items as unknown[]).length + (b.available_years as unknown[]).length;
    nonEmpty(srv, "dividends(route)", rows);
    nonEmpty(viaLoader, "dividends(loader)", rows);
    const diffs = deepDiff(srv, viaLoader);
    expect(show(diffs), `dividends: ${diffs.length} diff(s)`).toBe("");
  }, 120_000);

  it(`GET /reports/cashflow-sankey == getCashflowSankey() (${YTD_FROM}..${TODAY})`, async () => {
    const [srv, viaLoader] = await Promise.all([
      server(`/reports/cashflow-sankey?from=${YTD_FROM}&to=${TODAY}`),
      getCashflowSankey(YTD_FROM, TODAY),
    ]);
    const rows = (b: Record<string, unknown>) =>
      (b.nodes as unknown[]).length + (b.links as unknown[]).length;
    nonEmpty(srv, "cashflow-sankey(route)", rows);
    nonEmpty(viaLoader, "cashflow-sankey(loader)", rows);
    const diffs = deepDiff(srv, viaLoader);
    expect(show(diffs), `cashflow-sankey: ${diffs.length} diff(s)`).toBe("");
  }, 120_000);

  it(
    `GET /reports/total-return == getTotalReturn() (${TOTAL_RETURN_FROM}..${TOTAL_RETURN_TO})`,
    async () => {
      const [srv, viaLoader] = await Promise.all([
        server(`/reports/total-return?from=${TOTAL_RETURN_FROM}&to=${TOTAL_RETURN_TO}`),
        getTotalReturn(TOTAL_RETURN_FROM, TOTAL_RETURN_TO),
      ]);
      const rows = (b: Record<string, unknown>) => {
        // Count the non-null numeric/percent fields actually reported, so an
        // all-empty `status: no_data` body cannot pass as "the route works".
        return Object.keys(b).filter((k) => {
          const v = b[k];
          return typeof v === "string" && v !== "" && v !== "0.00" && v !== "0.00000000";
        }).length;
      };
      nonEmpty(srv, "total-return(route)", rows);
      nonEmpty(viaLoader, "total-return(loader)", rows);
      const diffs = deepDiff(srv, viaLoader);
      expect(show(diffs), `total-return: ${diffs.length} diff(s)`).toBe("");
    },
    120_000,
  );
});
