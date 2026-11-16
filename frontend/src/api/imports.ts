import { api, apiForm } from "./client";
// (docs/03). Upload is the one multipart route; client.ts speaks JSON only, so
// the upload posts its own FormData and reuses api()'s refresh path on a 401.

export type ImportBatchStatus = "pending_review" | "committed" | "discarded" | "reverted";

/** Mirrors `ImportBatchPublic`. Counts are batch-summary fields (docs/03). */
export interface ImportBatch {
  id: number;
  account_id: number;
  institution: string;
  file_name: string;
  status: ImportBatchStatus;
  imported: number;
  duplicates_skipped: number;
  unresolved: number;
}

/**
 * Mirrors `NearDuplicateMatchPublic`: the committed/staged/batch twin a
 * near-duplicate row matched, with the evidence the user decides on (docs/03).
 */
export interface NearDuplicateMatch {
  source: "committed" | "staged" | "batch";
  transaction_id: number | null;
  date: string;
  amount_native: string;
  currency: string;
  raw_description: string;
  delta_days: number;
  similarity: number;
}

/** Mirrors `PreviewRow`: one parsed statement row with its dedup verdict. */
export interface PreviewRow {
  id: number;
  date: string;
  amount_native: string;
  currency: string;
  raw_description: string;
  duplicate: boolean;
  cad_amount: string | null;
  /** M71: true when a committed/staged twin is within the near-dup window. */
  near_duplicate?: boolean;
  near_duplicate_match?: NearDuplicateMatch | null;
}

export type RowVerdict = "new" | "duplicate" | "near_duplicate";

/** One column-role mapping the dry run shows before anything is committed. */
export interface FieldRole {
  field: string;
  source: string;
}

export interface FormatMapping {
  format: string;
  kind: "csv_profile" | "structured";
  fields: FieldRole[];
  mode?: string | null;
  date_format?: string | null;
  decimal_comma?: boolean | null;
  credits_positive?: boolean | null;
  signs?: Record<string, number> | null;
  headerless?: boolean | null;
}

/** Mirrors `DryRunRow` — no id, because nothing was written. */
export interface DryRunRow {
  index: number;
  date: string;
  amount_native: string;
  currency: string;
  raw_description: string;
  cad_amount: string | null;
  verdict: RowVerdict;
  near_duplicate_match: NearDuplicateMatch | null;
}

export interface DryRunSummary {
  total: number;
  new: number;
  duplicates: number;
  near_duplicates: number;
  unresolved: number;
}

/** Mirrors `DryRunResponse` — the cancel-safe preview (POST /imports/preview). */
export interface DryRunResponse {
  format: string;
  mapping: FormatMapping;
  rows: DryRunRow[];
  summary: DryRunSummary;
}

export interface UploadResponse extends ImportBatch {
  preview: PreviewRow[];
}

/** LLM-suggested CSV column mappings (POST /imports/suggest-profile). */
export interface SuggestProfileResponse {
  columns: Record<string, string>;
  date_format: string;
  decimal_comma: boolean;
  credits_positive: boolean;
  /** True when the source CSV has no header row; columns then map by 1-based position. */
  headerless?: boolean;
  /**
   * Recognised export format (docs/58 §A) — `null` when the mapping came from
   * the LLM or the heuristic instead of a deterministic preset.
   */
  format?: string | null;
  confidence: number;
  reasoning: string;
}

export async function suggestProfile(file: File): Promise<SuggestProfileResponse> {
  const form = new FormData();
  form.append("file", file);
  return apiForm<SuggestProfileResponse>("/imports/suggest-profile", form);
}

/**
 * Cancel-safe dry run: the full parse + duplicate verdicts + cached-FX
 * conversion with ZERO writes. Confirming is a separate `uploadImport` call;
 * cancelling means simply never calling it (docs/03 preview contract).
 */
export async function previewImport(
  file: File,
  accountId: number,
  profile?: Record<string, unknown>,
): Promise<DryRunResponse> {
  const form = new FormData();
  form.append("file", file);
  form.append("account_id", String(accountId));
  if (profile !== undefined) form.append("profile_json", JSON.stringify(profile));
  return apiForm<DryRunResponse>("/imports/preview", form);
}

export async function uploadImport(file: File, accountId: number): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  form.append("account_id", String(accountId));
  return apiForm<UploadResponse>("/imports/upload", form);
}

/** Upload with a wizard-confirmed profile (no institution profile needed). */
export async function uploadImportWithProfile(
  file: File,
  accountId: number,
  profile: Record<string, unknown>,
): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  form.append("account_id", String(accountId));
  form.append("profile_json", JSON.stringify(profile));
  return apiForm<UploadResponse>("/imports/upload", form);
}

export function getBatch(batchId: number): Promise<ImportBatch> {
  return api<ImportBatch>(`/imports/${batchId}`);
}

export function getBatchRows(batchId: number): Promise<PreviewRow[]> {
  return api<PreviewRow[]>(`/imports/${batchId}/rows`);
}

export function commitBatch(batchId: number, includeIds: number[] = []): Promise<ImportBatch> {
  return api<ImportBatch>(`/imports/${batchId}/commit`, { method: "POST", body: { include_ids: includeIds } });
}

export function discardBatch(batchId: number): Promise<ImportBatch> {
  return api<ImportBatch>(`/imports/${batchId}/discard`, { method: "POST" });
}

export function revertBatch(batchId: number): Promise<ImportBatch> {
  return api<ImportBatch>(`/imports/${batchId}/revert`, { method: "POST" });
}

export interface ImportBatchPage {
  items: ImportBatch[];
  total: number;
  limit: number;
  offset: number;
}

/** GET /imports — paginated batch history, newest first (backend W5 addendum). */
export function listImports(
  paging: { limit?: number; offset?: number; account_id?: number } = {},
): Promise<ImportBatchPage> {
  const params = new URLSearchParams();
  if (paging.limit !== undefined) params.set("limit", String(paging.limit));
  if (paging.offset !== undefined) params.set("offset", String(paging.offset));
  // M95 P3: the account-delete blast radius needs this account's batch count;
  // the server has accepted `account_id` since docs/03 — only the client was
  // not sending it.
  if (paging.account_id !== undefined) params.set("account_id", String(paging.account_id));
  const qs = params.toString();
  return api<ImportBatchPage>(`/imports${qs === "" ? "" : `?${qs}`}`);
}
