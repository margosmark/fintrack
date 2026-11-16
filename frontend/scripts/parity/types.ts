// Parity harness types — M48 E2EE ports (docs/33 Phase 3, slices S3/S4).
//
// Test infrastructure only: nothing here is imported by the app. A fixture is
// a frozen pair of (inputs, server JSON); an engine under test must reproduce
// `{status, body}` for every case from the inputs alone — the harness never
// hands `expected` to the engine, so echoing the golden is not reachable by
// accident.

/** JSON as the API actually returns it (Decimals are strings, money is a string). */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

/** Provenance + the commands that produced/consume the file (see README.md). */
export interface FixtureMeta {
  /** "s3" | "s4" */
  slice: string;
  /** API root without trailing slash, e.g. https://fintrac.princessandmark.com/api/v1 */
  base_url: string;
  /** Which tenant the goldens came from: "demo", or "demo + throwaway". */
  tenant: string;
  /** `git rev-parse HEAD` at capture time — the goldens are valid FOR this commit. */
  base_commit: string;
  captured_at_utc: string;
  /**
   * `today_et()` (America/Toronto) at capture time, YYYY-MM-DD. The budgets and
   * subscriptions server paths derive their window from it, so an engine must
   * inject THIS date, not the machine's today, to reproduce the golden.
   */
  captured_at_et: string;
  /** `get_settings().reporting_currency` — not exposed by any endpoint (CAD). */
  reporting_currency: string;
  capture_command: string;
  diff_command: string;
  /** Deviations, time-dependence, docs/08 discrepancies found while capturing. */
  notes: string[];
}

export type HttpMethod = "GET" | "POST";

/** The request that produced `expected` — the engine is handed exactly this. */
export interface ParityRequest {
  method: HttpMethod;
  /** Path relative to `meta.base_url`, query string included. */
  path: string;
  body?: Json;
}

export interface ParityExpectation {
  status: number;
  body: Json;
}

export interface ParityCase {
  /** Stable id, unique within the fixture (e.g. "rules.preview.amount-between"). */
  id: string;
  /** Key into `fixture.datasets`. */
  dataset: string;
  request: ParityRequest;
  expected: ParityExpectation;
}

/** One tenant's frozen input collections, keyed by name (transactions, trades, …). */
export type Dataset = Record<string, Json>;

export interface Fixture {
  meta: FixtureMeta;
  datasets: Record<string, Dataset>;
  cases: ParityCase[];
}

/** What an engine is allowed to see: no `expected`, by construction. */
export interface EngineCall {
  meta: FixtureMeta;
  datasetName: string;
  dataset: Dataset;
  request: ParityRequest;
}

export interface EngineResult {
  status: number;
  body: Json;
}

export type Engine = (call: EngineCall) => Promise<EngineResult> | EngineResult;

/** Shape every `engines/<slice>.ts` exports. `wired` stays false until filled in. */
export interface EngineModule {
  wired: boolean;
  runEngine: Engine;
}

/** Exit codes the CLI returns — keep in sync with the README table. */
export const EXIT = {
  /** every case reproduced byte-for-byte */
  PASS: 0,
  /** at least one case differed (or an engine threw / returned a bad status) */
  DIFF: 1,
  /** the slice's engine adapter has not been filled in yet — NOT a pass */
  NOT_WIRED: 2,
  /** `--self-test` found the diff machinery broken */
  SELF_TEST: 3,
  /** bad arguments */
  USAGE: 4,
  /** fixture missing or structurally invalid */
  FIXTURE: 5,
} as const;
