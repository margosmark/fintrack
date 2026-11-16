import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import type { ProjectionRequest, ProjectionResponse } from "../src/api/projections";
import { ProjectionInputError, runProjectionEngine } from "../src/lib/projectionEngine";

/**
 * Server-parity goldens for the client-side projection run (M65, docs/12 §7).
 *
 * Every `response` below was produced by the SERVER implementation —
 * `backend/app/services/projections.py::project_scenario` through
 * `app/api/projections.py::run_projections` (Decimal, 28 significant digits,
 * ROUND_HALF_UP `_cents`) — and captured as JSON. `runProjectionEngine` must
 * reproduce it STRING FOR STRING: the Reports tab now runs this math in the
 * browser, so a "close enough" float port would silently print a different
 * balance than the API, the CSV consumer, and docs/12.
 *
 * Regenerate with the reference harness (scratch, not shipped):
 * `m65_ref.py` builds this JSON from the live backend; `m65_client.mjs` diffs
 * it against the bundled engine (207 cases / 16,565 series rows, 0 mismatches
 * across the curated + randomized sweeps).
 */
interface ParityCase {
  name: string;
  request: ProjectionRequest;
  response: ProjectionResponse;
}

const cases: ParityCase[] = JSON.parse(
  readFileSync("tests/fixtures/projection-parity.json", "utf8"),
) as ParityCase[];

describe("projectionEngine — exact parity with the Decimal reference", () => {
  it("has captured cases to compare against", () => {
    expect(cases.length).toBeGreaterThanOrEqual(7);
  });

  for (const parityCase of cases) {
    it(`matches the server on ${parityCase.name}`, () => {
      expect(runProjectionEngine(parityCase.request)).toEqual(parityCase.response);
    });
  }
});

describe("projectionEngine — input bounds (docs/12 §7, mirrors app/schemas/projections.py)", () => {
  const base: ProjectionRequest = {
    start_value_cad: "1000.00",
    annual_contribution_cad: "1200",
    years: 25,
    inflation_pct: "2",
    scenarios: [{ label: "Balanced", annual_return_pct: "6.5" }],
  };

  /** The client validates the same bounds the endpoint 422s on, so an
   *  invalid payload renders in the panel's ErrorPanel with the same
   *  `code` the envelope carries instead of a network round-trip. */
  const rejects = (over: Partial<ProjectionRequest>, message: RegExp): void => {
    let thrown: unknown;
    try {
      runProjectionEngine({ ...base, ...over } as ProjectionRequest);
    } catch (err) {
      thrown = err;
    }
    expect(thrown).toBeInstanceOf(ProjectionInputError);
    const error = thrown as ProjectionInputError;
    expect(error.code).toBe("validation_error");
    expect(error.message).toMatch(message);
  };

  it("rejects years outside 1–50", () => {
    rejects({ years: 0 }, /Years must be between 1 and 50/);
    rejects({ years: 51 }, /Years must be between 1 and 50/);
  });

  it("rejects a non-integer year count", () => {
    rejects({ years: 2.5 }, /Years must be a whole number/);
  });

  it("rejects inflation outside 0–20", () => {
    rejects({ inflation_pct: "20.1" }, /Inflation must be between 0 and 20/);
  });

  it("rejects returns outside -50–50", () => {
    const over = {
      scenarios: [{ label: "Wild", annual_return_pct: "60" }],
    } as Partial<ProjectionRequest>;
    rejects(over, /Annual return must be between -50 and 50/);
  });

  it("rejects a negative start value or contribution", () => {
    rejects({ start_value_cad: "-0.01" }, /Start value must be 0 or more/);
    rejects({ annual_contribution_cad: "-1" }, /Annual contribution must be 0 or more/);
  });

  it("rejects a non-numeric amount", () => {
    rejects({ start_value_cad: "1,000" }, /Start value must be a number/);
  });

  it("rejects more than 8 scenarios", () => {
    const over = {
      scenarios: Array.from({ length: 9 }, (_, i) => ({
        label: `S${i}`,
        annual_return_pct: "6",
      })),
    } as Partial<ProjectionRequest>;
    rejects(over, /between 1 and 8/);
  });

  it("rejects a label over 40 characters", () => {
    const over = {
      scenarios: [{ label: "x".repeat(41), annual_return_pct: "6" }],
    } as Partial<ProjectionRequest>;
    rejects(over, /1–40 characters/);
  });
});
