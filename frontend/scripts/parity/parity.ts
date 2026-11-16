/**
 * Parity diff harness for the M48 E2EE parity ports (docs/33 Phase 3, S1-S5).
 *
 *   node frontend/scripts/parity/parity.ts --self-test           # diff machinery self-test
 *   node frontend/scripts/parity/parity.ts --verify --slice s3   # fixture structure only
 *   node frontend/scripts/parity/parity.ts --slice projection    # POST /projections/run (live; M157)
 *
 * Exit codes (types.ts::EXIT): 0 pass · 1 diff · 2 engine not wired ·
 * 3 self-test failure · 4 usage · 5 fixture invalid.
 *
 * M160 (P2-5, docs/33 §Server-first rule 5) retired the six CLIENT-COMPARISON
 * slices — s2a, matrix, s3, s4, s5, household — together with the mirrors
 * their engines ported (`lib/reportEngine.ts`, `lib/s3Engine.ts`,
 * `lib/totalReturnEngine.ts`): with no client half left to compare, an
 * offline engine adapter would have nothing to run. Their fixtures
 * (`tests/fixtures/*-vectors.json`) were NOT deleted — M158 kept them as
 * SERVER-ROUTE REGRESSION PINS, and they are now replayed against the mounted
 * routes by the backend suite, on the `test_search_goldens.py` pattern:
 *
 *   backend/tests/test_m160_replay_s2a_matrix.py   (s2a 29 + matrix 6)
 *   backend/tests/test_m160_replay_s3.py           (16)
 *   backend/tests/test_m160_replay_s4.py           (14)
 *   backend/tests/test_m160_replay_household.py    (12)
 *   backend/tests/test_search_goldens.py           (s5 10 — M137 already)
 *
 * `--verify --slice <retired>` still validates a kept fixture's structure.
 * Only `projection` remains runnable here: its engine IS the server over
 * HTTP, so it needs a running stack (`--base-url`, or `PARITY_BASE_URL`).
 * `wired: false` in engines/<slice>.ts still exits 2 on purpose: "no engine
 * yet" must never read as a pass.
 */

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { deepDiff, type Diff } from "./deepDiff.ts";
import {
  EXIT,
  type Engine,
  type EngineCall,
  type EngineModule,
  type Fixture,
  type Json,
  type ParityCase,
} from "./types.ts";

const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url));

/** Every slice the harness knows. `projection` is the live one (see header). */
const SLICES = ["s2a", "matrix", "s3", "s4", "s5", "projection", "household"] as const;
type Slice = (typeof SLICES)[number];

/** M160: client-comparison slices whose engines retired — run one and the
 *  harness refuses loudly (exit 4) instead of pretending to compare. */
const RETIRED_SLICES = new Set<string>(["s2a", "matrix", "s3", "s4", "s5", "household"]);

/** Fixture files whose name does not follow the `<slice>-vectors.json` rule. */
const FIXTURE_FILE: Partial<Record<Slice, string>> = { projection: "projection-parity.json" };

/** The deployed API root the live slice talks to (overridable, see --base-url). */
const DEFAULT_BASE_URL = "https://fintrac.princessandmark.com/api/v1";

function fixtureUrl(slice: Slice): string {
  const name = FIXTURE_FILE[slice] ?? `${slice}-vectors.json`;
  return fileURLToPath(new URL(`../../tests/fixtures/${name}`, import.meta.url));
}

function rel(p: string): string {
  return path.relative(REPO_ROOT, p) || p;
}

// --------------------------------------------------------------------------
// fixture loading + structural validation
// --------------------------------------------------------------------------

/** Returns every structural problem found; empty array = valid. */
export function validateFixture(fixture: unknown): string[] {
  const problems: string[] = [];
  if (fixture === null || typeof fixture !== "object" || Array.isArray(fixture)) {
    return ["fixture root is not an object"];
  }
  const f = fixture as Partial<Fixture>;
  const meta = f.meta;
  if (meta === null || typeof meta !== "object") {
    problems.push("meta is missing");
  } else {
    for (const key of ["slice", "base_url", "base_commit", "captured_at_et", "reporting_currency"] as const) {
      if (typeof meta[key] !== "string" || meta[key] === "") problems.push(`meta.${key} is missing`);
    }
    if (!Array.isArray(meta.notes)) problems.push("meta.notes is not an array");
  }
  if (f.datasets === null || typeof f.datasets !== "object" || Array.isArray(f.datasets)) {
    problems.push("datasets is missing");
  }
  if (!Array.isArray(f.cases) || f.cases.length === 0) {
    problems.push("cases is missing or empty");
    return problems;
  }
  const seen = new Set<string>();
  for (const c of f.cases) {
    if (c === null || typeof c !== "object") {
      problems.push("a case is not an object");
      continue;
    }
    const kase = c as ParityCase;
    if (typeof kase.id !== "string" || kase.id === "") {
      problems.push("a case has no id");
      continue;
    }
    if (seen.has(kase.id)) problems.push(`duplicate case id: ${kase.id}`);
    seen.add(kase.id);
    if (typeof kase.dataset !== "string" || !(kase.dataset in (f.datasets ?? {}))) {
      problems.push(`${kase.id}: dataset "${kase.dataset}" is not in datasets`);
    }
    if (kase.request === null || typeof kase.request !== "object" || typeof kase.request.path !== "string") {
      problems.push(`${kase.id}: request is malformed`);
    }
    if (kase.expected === null || typeof kase.expected !== "object" || typeof kase.expected.status !== "number") {
      problems.push(`${kase.id}: expected.status is missing`);
    } else if (!("body" in kase.expected)) {
      problems.push(`${kase.id}: expected.body is missing`);
    }
  }
  return problems;
}

/**
 * `projection-parity.json` (M65, docs/12 §7) predates this harness: it is a
 * bare array of `{name, request, response}` whose `response` was produced by
 * `backend/app/api/projections.py::run_projections`. M135 §4.1 forbids
 * re-capturing a golden to satisfy a parity gate, so the frozen file is
 * ADAPTED into the shape `validateFixture` understands — structurally only:
 * every request body and every response value is copied through untouched.
 */
function adaptProjectionFixture(raw: unknown, baseUrl: string): Fixture {
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new FixtureError("projection fixture is not a non-empty array of {name, request, response}");
  }
  const goldens = raw as { name?: unknown; request?: unknown; response?: unknown }[];
  const fixture: Fixture = {
    meta: {
      slice: "projection",
      base_url: baseUrl,
      tenant: "demo",
      base_commit: "m65-frozen",
      captured_at_utc: "n/a",
      // The run is pure request-body math (docs/12 §7 — "no DB reads"), so no
      // server-side clock enters the response and the capture date is inert.
      captured_at_et: "n/a",
      reporting_currency: "CAD",
      capture_command: "n/a — the M65 goldens predate capture.ts and are FROZEN (M135 §4.1)",
      diff_command: "node frontend/scripts/parity/parity.ts --slice projection",
      notes: [
        "Adapted from frontend/tests/fixtures/projection-parity.json, never re-captured.",
        "The engine under test is the SERVER over HTTP, exercised as the demo tenant.",
      ],
    },
    datasets: { "pure-math": {} },
    cases: goldens.map((golden, index) => ({
      id: typeof golden.name === "string" && golden.name !== "" ? golden.name : `case-${index}`,
      dataset: "pure-math",
      request: {
        method: "POST" as const,
        path: "/projections/run",
        body: golden.request as Json,
      },
      expected: { status: 200, body: golden.response as Json },
    })),
  };
  const problems = validateFixture(fixture);
  if (problems.length > 0) {
    throw new FixtureError(`adapted projection fixture is invalid:\n  - ${problems.join("\n  - ")}`);
  }
  return fixture;
}

function loadSliceFixture(slice: Slice, file: string, baseUrl: string): Fixture {
  if (!existsSync(file)) {
    throw new FixtureError(`fixture not found: ${rel(file)} — run the capture script first`);
  }
  const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
  if (slice === "projection") return adaptProjectionFixture(parsed, baseUrl);
  const problems = validateFixture(parsed);
  if (problems.length > 0) {
    throw new FixtureError(`fixture ${rel(file)} is invalid:\n  - ${problems.join("\n  - ")}`);
  }
  return parsed as Fixture;
}

class FixtureError extends Error {}

// --------------------------------------------------------------------------
// the run
// --------------------------------------------------------------------------

export interface CaseResult {
  id: string;
  method: string;
  requestPath: string;
  diffs: Diff[];
  error?: string;
}

async function runCase(engine: Engine, fixture: Fixture, kase: ParityCase): Promise<CaseResult> {
  const base: CaseResult = {
    id: kase.id,
    method: kase.request.method,
    requestPath: kase.request.path,
    diffs: [],
  };
  const dataset = fixture.datasets[kase.dataset];
  if (dataset === undefined) {
    return { ...base, diffs: [{ path: "$", kind: "missing", expected: "dataset", actual: kase.dataset }] };
  }

  const call: EngineCall = {
    meta: fixture.meta,
    datasetName: kase.dataset,
    dataset,
    request: kase.request,
  };

  let actual: { status?: unknown; body?: unknown };
  try {
    actual = await engine(call);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return {
      ...base,
      error: message,
      diffs: [{ path: "$", kind: "value", expected: "<engine returned a result>", actual: `threw: ${message}` }],
    };
  }
  if (actual === null || typeof actual !== "object") {
    return {
      ...base,
      error: "engine returned a non-object",
      diffs: [{ path: "$", kind: "type", expected: "{status, body}", actual: typeof actual }],
    };
  }

  const diffs = deepDiff(kase.expected.status, actual.status, "$.status");
  diffs.push(...deepDiff(kase.expected.body, actual.body, "$"));
  return { ...base, diffs };
}

export async function runParity(fixture: Fixture, engine: Engine): Promise<CaseResult[]> {
  const results: CaseResult[] = [];
  for (const kase of fixture.cases) {
    results.push(await runCase(engine, fixture, kase));
  }
  return results;
}

export function exitCodeFor(results: CaseResult[]): number {
  return results.every((r) => r.diffs.length === 0) ? EXIT.PASS : EXIT.DIFF;
}

function printResults(results: CaseResult[], perCaseLimit = 10): void {
  let failed = 0;
  for (const r of results) {
    const ok = r.diffs.length === 0;
    if (!ok) failed += 1;
    console.log(`  ${ok ? "PASS" : "FAIL"} ${r.id} (${r.method} ${r.requestPath})`);
    if (ok) continue;
    for (const d of r.diffs.slice(0, perCaseLimit)) {
      console.log(`        ${d.path} [${d.kind}] expected ${d.expected} | actual ${d.actual}`);
    }
    if (r.diffs.length > perCaseLimit) {
      console.log(`        ... ${r.diffs.length - perCaseLimit} more diff(s)`);
    }
  }
  console.log(`${results.length} case(s): ${results.length - failed} passed, ${failed} failed`);
}

async function loadEngine(slice: string, override: string): Promise<EngineModule> {
  const url =
    override !== ""
      ? pathToFileURL(path.resolve(override)).href
      : new URL(`./engines/${slice}.ts`, import.meta.url).href;
  const mod = (await import(url)) as Partial<EngineModule>;
  if (typeof mod.runEngine !== "function") {
    throw new Error(`${override !== "" ? override : `engines/${slice}.ts`} does not export runEngine`);
  }
  return { wired: mod.wired === true, runEngine: mod.runEngine };
}

// --------------------------------------------------------------------------
// self-test: prove the diff machinery both passes and FAILS correctly
// --------------------------------------------------------------------------

function syntheticFixture(): Fixture {
  return {
    meta: {
      slice: "self-test",
      base_url: "http://self.test/api/v1",
      tenant: "self-test",
      base_commit: "self-test",
      captured_at_utc: "2026-01-01T00:00:00.000Z",
      captured_at_et: "2026-01-01",
      reporting_currency: "CAD",
      capture_command: "n/a",
      diff_command: "n/a",
      notes: [],
    },
    datasets: {
      d: {
        rows: [
          { id: 1, amount: "10.00" },
          { id: 2, amount: "-1.05" },
        ],
      },
    },
    cases: [
      {
        id: "self.ok",
        dataset: "d",
        request: { method: "GET", path: "/self" },
        expected: { status: 200, body: { count: 2, sum: "8.95", items: [{ id: 1 }, { id: 2 }] } },
      },
    ],
  };
}

function selfTest(): boolean {
  let failures = 0;
  const check = (name: string, pass: boolean, detail = ""): void => {
    console.log(`  ${pass ? "PASS" : "FAIL"} ${name}${pass || detail === "" ? "" : ` — ${detail}`}`);
    if (!pass) failures += 1;
  };
  const d = (e: unknown, a: unknown): Diff[] => deepDiff(e, a);

  console.log("deepDiff:");
  check("identical objects with different key order -> 0 diffs", d({ a: 1, b: "x" }, { b: "x", a: 1 }).length === 0);
  check("scalar value change -> 1 value diff", d({ a: "1.00" }, { a: "1.01" }).length === 1);
  check("numeric type change -> type diff", d({ a: 100 }, { a: "100" })[0]?.kind === "type");
  check("missing key -> missing diff", d({ a: 1, b: 2 }, { a: 1 })[0]?.kind === "missing");
  check("extra key -> extra diff", d({ a: 1 }, { a: 1, b: 2 })[0]?.kind === "extra");
  check("null vs missing -> diff", d({ a: null }, {}) .length === 1);
  check("array element change -> indexed diff", d([1, 2], [1, 3])[0]?.path === "$[1]");
  check("array length change -> length diff", d([1], [1, 2])[0]?.kind === "length");
  check("array order change -> diff", d([{ id: 1 }, { id: 2 }], [{ id: 2 }, { id: 1 }]).length > 0);
  check("nested object change -> full path", d({ a: { b: [0, { c: 1 }] } }, { a: { b: [0, { c: 2 }] } })[0]?.path === "$.a.b[1].c");
  check("string vs number inside array -> type diff", d(["1"], [1])[0]?.kind === "type");

  const fixture = syntheticFixture();
  const identity: Engine = async () => ({ status: 200, body: fixture.cases[0].expected.body });
  const mutated: Engine = async () => ({ status: 200, body: { count: 3, sum: "8.95", items: [] } });
  const wrongStatus: Engine = async () => ({ status: 422, body: { detail: "x", code: "y" } });
  const throwing: Engine = () => {
    throw new Error("boom");
  };

  console.log("runner:");
  return Promise.resolve()
    .then(() => runParity(fixture, identity))
    .then((identityResults) => {
      check("engine matching the golden -> exit 0", exitCodeFor(identityResults) === EXIT.PASS);
      return runParity(fixture, mutated);
    })
    .then((mutatedResults) => {
      check("engine with a wrong body -> exit 1", exitCodeFor(mutatedResults) === EXIT.DIFF);
      check("wrong body reports the count diff", mutatedResults[0].diffs.some((x) => x.path === "$.count"));
      return runParity(fixture, wrongStatus);
    })
    .then((statusResults) => {
      check("wrong status -> exit 1", exitCodeFor(statusResults) === EXIT.DIFF);
      check("status diff reported at $.status", statusResults[0].diffs.some((x) => x.path === "$.status"));
      return runParity(fixture, throwing);
    })
    .then((throwResults) => {
      check("throwing engine -> exit 1 (never a pass)", exitCodeFor(throwResults) === EXIT.DIFF);
      check("fixture validator catches a broken fixture", validateFixture({ meta: {}, datasets: {}, cases: [] }).length > 0);
      check("fixture validator accepts the synthetic fixture", validateFixture(fixture).length === 0);
      console.log(failures === 0 ? "SELF_TEST_OK" : `SELF_TEST_FAILED (${failures})`);
      return failures === 0;
    })
    .catch((error: unknown) => {
      console.error(`self-test crashed: ${error instanceof Error ? error.message : String(error)}`);
      return false;
    });
}

// --------------------------------------------------------------------------
// CLI
// --------------------------------------------------------------------------

function usage(): void {
  console.error(
    [
      "usage: node frontend/scripts/parity/parity.ts (--self-test | --verify --slice <slice> | --slice <slice> [--fixture <path>] [--engine <path>] [--base-url <url>])",
      "",
      "  --self-test   prove the deep-diff + runner behave (exit 0 / 3)",
      "  --verify      validate the fixture's structure only (exit 0 / 5)",
      `  --slice       run the parity comparison for a slice: ${SLICES.join(" | ")} (exit 0 / 1 / 2)`,
      "  --fixture     override the fixture path (used to diff a tampered copy)",
      "  --engine      override the engine adapter path (plumbing checks; defaults to engines/<slice>.ts)",
      `  --base-url    API root for the live \`projection\` slice (default ${DEFAULT_BASE_URL})`,
    ].join("\n"),
  );
}

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  let slice = "";
  let fixturePath = "";
  let enginePath = "";
  let baseUrl = process.env.PARITY_BASE_URL ?? DEFAULT_BASE_URL;
  let selfTestMode = false;
  let verifyOnly = false;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--slice") {
      slice = argv[i + 1] ?? "";
      i += 1;
    } else if (arg === "--fixture") {
      fixturePath = argv[i + 1] ?? "";
      i += 1;
    } else if (arg === "--engine") {
      enginePath = argv[i + 1] ?? "";
      i += 1;
    } else if (arg === "--base-url") {
      baseUrl = argv[i + 1] ?? "";
      i += 1;
    } else if (arg === "--self-test") {
      selfTestMode = true;
    } else if (arg === "--verify") {
      verifyOnly = true;
    } else if (arg === "--help" || arg === "-h") {
      usage();
      process.exit(EXIT.PASS);
    } else {
      console.error(`unknown argument: ${arg}`);
      usage();
      process.exit(EXIT.USAGE);
    }
  }

  if (selfTestMode) {
    const ok = await selfTest();
    process.exit(ok ? EXIT.PASS : EXIT.SELF_TEST);
  }

  if (!(SLICES as readonly string[]).includes(slice)) {
    usage();
    process.exit(EXIT.USAGE);
  }
  const target = slice as Slice;
  const file = fixturePath !== "" ? path.resolve(fixturePath) : fixtureUrl(target);

  let fixture: Fixture;
  try {
    fixture = loadSliceFixture(target, file, baseUrl);
  } catch (error) {
    console.error(error instanceof FixtureError ? error.message : String(error));
    process.exit(EXIT.FIXTURE);
  }

  if (verifyOnly) {
    console.log(`FIXTURE_OK ${rel(file)} — ${fixture.cases.length} case(s), datasets: ${Object.keys(fixture.datasets).join(", ")}`);
    console.log(`  base_commit=${fixture.meta.base_commit}  captured_at_et=${fixture.meta.captured_at_et}`);
    process.exit(EXIT.PASS);
  }

  if (RETIRED_SLICES.has(target)) {
    console.error(
      [
        `SLICE RETIRED (M160): \`--slice ${target}\` was a client-comparison run, and the`,
        "client mirrors it compared retired with P2-5 (docs/33 §Server-first rule 5).",
        "This is NOT a parity result — nothing was compared.",
        "The fixture is kept as a server-route regression pin; replay it with:",
        "  cd backend && uv run pytest tests/test_m160_replay_*.py -q",
      ].join("\n"),
    );
    process.exit(EXIT.USAGE);
  }

  const engineModule = await loadEngine(target, enginePath);
  if (!engineModule.wired) {
    console.error(
      [
        `ENGINE NOT WIRED: frontend/scripts/parity/engines/${slice}.ts still exports wired=false.`,
        "This is NOT a parity result — nothing was compared.",
        "Fill in runEngine (port the server path, return {status, body}), set wired=true, then re-run.",
      ].join("\n"),
    );
    process.exit(EXIT.NOT_WIRED);
  }

  console.log(
    `PARITY ${slice}  fixture=${rel(file)}  base_commit=${fixture.meta.base_commit}  captured_at_et=${fixture.meta.captured_at_et}`,
  );
  const results = await runParity(fixture, engineModule.runEngine);
  printResults(results);
  process.exit(exitCodeFor(results));
}

main().catch((error: unknown) => {
  console.error(`parity harness failed: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(EXIT.USAGE);
});
