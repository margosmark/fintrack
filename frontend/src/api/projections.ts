import { api } from "./client";

// projections (M15, docs/12): deterministic future-value scenario runs.
// Amounts are cents-quantized Decimal values serialized as strings.

export interface ProjectionScenarioInput {
  label: string;
  annual_return_pct: string;
}

export interface ProjectionRequest {
  start_value_cad: string;
  annual_contribution_cad: string;
  years: number;
  inflation_pct: string;
  scenarios: ProjectionScenarioInput[];
}

export interface ProjectionYearRow {
  year: number;
  contributed_cad: string;
  value_cad: string;
  value_real_cad: string;
}

export interface ProjectionScenarioResult {
  label: string;
  series: ProjectionYearRow[];
  final_value_cad: string;
  total_contributed_cad: string;
  total_gain_cad: string;
}

export interface ProjectionResponse {
  start_value_cad: string;
  years: number;
  inflation_pct: string;
  scenarios: ProjectionScenarioResult[];
}

export interface ProjectionDefaults {
  start_value_cad: string | null;
}

/**
 * POST /projections/run — runs every scenario through the pure projector.
 *
 * M157: this is the Reports calculator's source of truth for every tenant —
 * the demo one included, since the route joined
 * `DEMO_MUTATION_EXEMPT_PATHS` (pure request-body math, no row is written).
 * The client mirror in `lib/projectionEngine.ts` stays as the offline / E2EE
 * fallback and must keep reproducing this response byte-for-byte
 * (`tests/fixtures/projection-parity.json`).
 */
export async function runProjections(payload: ProjectionRequest): Promise<ProjectionResponse> {
  return api<ProjectionResponse>("/projections/run", { method: "POST", body: payload });
}

/** GET /projections/defaults — current portfolio market value CAD for prefill. */
export async function getProjectionDefaults(): Promise<ProjectionDefaults> {
  return api<ProjectionDefaults>("/projections/defaults");
}
