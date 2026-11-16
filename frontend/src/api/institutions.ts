// institutions — CRUD + validated CSV import profiles (docs/08, docs/03).
import { api } from "./client";

/**
 * `institutions.importer_profile` (docs/03). Strictly validated by the
 * backend (pydantic extra=forbid) — violations surface as a 422 envelope.
 *
 * Two modes, chosen by which column keys are present:
 * - signed: `columns` maps date / amount_native / raw_description; amounts
 *   carry their sign in-file, optionally flipped via `credits_positive`.
 * - split: `columns` maps date / raw_description / debit / credit — money
 *   out lives in the debit column, money in the credit column. Signs per
 *   column are configurable via `signs` (defaults {-1, +1}). No
 *   `amount_native`; `credits_positive` is ignored.
 *
 * Optional in BOTH modes: `columns.posting_date` (secondary date column,
 * parsed with `date_format`) and `columns.currency` (per-row ISO currency
 * token; invalid/blank falls back to account currency).
 */
export interface ProfileSigns {
  debit: -1 | 1;
  credit: -1 | 1;
}

export interface ImporterProfile {
  columns: Record<string, string>;
  date_format?: string;
  decimal_comma?: boolean;
  /** Signed mode only. Ignored when the profile uses debit/credit columns. */
  credits_positive?: boolean;
  /**
   * Split mode only; silently ignored in signed mode. Multiplies each
   * column's value before it becomes the row amount. Defaults to
   * {debit: -1, credit: 1}. Omit unless explicitly configured.
   */
  signs?: ProfileSigns;
  /**
   * When true, `columns` values are 1-based column indices (strings) and the
   * parser consumes no header row. When false (default), values are header names.
   */
  headerless?: boolean;
}

export interface Institution {
  id: number;
  name: string;
  importer_profile: ImporterProfile | null;
}

export interface InstitutionPage {
  items: Institution[];
  total: number;
  limit: number;
  offset: number;
}

export interface InstitutionPayload {
  name: string;
  importer_profile?: ImporterProfile | null;
}

export async function listInstitutions(limit = 50, offset = 0): Promise<InstitutionPage> {
  return api<InstitutionPage>(`/institutions?limit=${limit}&offset=${offset}`);
}

export function createInstitution(payload: InstitutionPayload): Promise<Institution> {
  return api<Institution>("/institutions", { method: "POST", body: payload });
}

export function updateInstitution(
  institutionId: number,
  patch: Partial<InstitutionPayload>,
): Promise<Institution> {
  return api<Institution>(`/institutions/${institutionId}`, { method: "PATCH", body: patch });
}

export function deleteInstitution(institutionId: number): Promise<void> {
  return api<void>(`/institutions/${institutionId}`, { method: "DELETE" }).then(
    () => undefined,
  );
}
