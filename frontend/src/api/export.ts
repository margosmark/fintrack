import { api, apiBinary } from "./client";

/**
 * Export endpoints (docs/11): the machine snapshot, the M75 one-click ZIP and
 * OFX exports, and the M75 scheduled-export schedule.
 *
 * `GET /export/full` returns a single JSON object keyed by table name — the
 * source for the browser download AND for the encrypted local backup.
 * `GET /export/zip` returns one ZIP of one CSV per table plus a JSON manifest
 * (committed rows only — it mirrors what the app shows). `GET /export/ofx`
 * returns one account's committed transactions as an OFX 1.02 statement.
 *
 * `vault` is this session's vault state: `locked` (the server's fail-closed
 * default) makes the export write `E2EE:<ciphertext>` where a cipher-backed
 * field's plaintext would be, `unlocked` ships plaintext (docs/11 §
 * "Export & the vault"). Every path is a read, so all of them work under
 * privacy mode and on the demo tenant.
 */

const EXPORT_PATH = "/export/full";
const ZIP_PATH = "/export/zip";
const OFX_PATH = "/export/ofx";

export const EXPORT_SCHEDULE_PATH = "/export/schedule";

export type VaultState = "locked" | "unlocked";
export type ExportFrequency = "off" | "daily" | "weekly" | "monthly";

/** The five `users` columns behind `GET/PUT /export/schedule` (docs/11). */
export interface ExportSchedule {
  frequency: ExportFrequency;
  /** 0–23, local time of the machine running the tab. */
  hour: number;
  /** 0–6, 0 = Monday (weekly only). */
  weekday: number;
  /** 1–28 (monthly only). */
  day_of_month: number;
  /** 1–20 retention. */
  keep_last_n: number;
}

/** `fin-tracker-export-<iso-date>.json` (docs/11 step 3). */
export function exportFilename(at: Date = new Date()): string {
  return `fin-tracker-export-${at.toISOString().slice(0, 10)}.json`;
}

/** `fin-tracker-export-<iso-date>.zip` — mirrors the server's attachment name. */
export function zipFilename(at: Date = new Date()): string {
  return `fin-tracker-export-${at.toISOString().slice(0, 10)}.zip`;
}

/** Filename from a `Content-Disposition`, or `fallback` when absent. */
function filenameFromDisposition(header: string | null, fallback: string): string {
  const match = /filename="([^"]+)"/.exec(header ?? "");
  return match?.[1] ?? fallback;
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

/**
 * Fetches the export snapshot for the encrypted browser backup (docs/11 step 4).
 * Independent of LLM / market-data services, so it works under privacy mode
 * (docs/11 step 7). `apiBinary` attaches the bearer token, runs the shared
 * 401 → refresh → replay dance, and throws `ApiError` on a non-2xx response.
 */
export async function getFullExport(): Promise<unknown> {
  return (await apiBinary(EXPORT_PATH)).json();
}

/**
 * Downloads the full export as a JSON blob named
 * `fin-tracker-export-<iso-date>.json`; resolves with the completion timestamp
 * (ISO string) for the "last export" display.
 */
export async function downloadExport(): Promise<string> {
  const response = await apiBinary(EXPORT_PATH);
  const blob = await response.blob();
  const url = URL.createObjectURL(
    new Blob([blob], { type: "application/json;charset=utf-8" }),
  );
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = exportFilename();
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
  return new Date().toISOString();
}

/**
 * The ZIP itself, without downloading it — the scheduled-export runner stores
 * it in local storage (IndexedDB) instead of dropping a file on disk.
 */
export async function fetchZip(vault: VaultState): Promise<Blob> {
  const response = await apiBinary(`${ZIP_PATH}?vault=${vault}`);
  return response.blob();
}

/** One-click full export: downloads the ZIP; resolves with the ISO timestamp. */
export async function downloadZipExport(vault: VaultState): Promise<string> {
  const response = await apiBinary(`${ZIP_PATH}?vault=${vault}`);
  const blob = await response.blob();
  saveBlob(
    blob,
    filenameFromDisposition(
      response.headers.get("content-disposition"),
      zipFilename(),
    ),
  );
  return new Date().toISOString();
}

/** One account's committed transactions as OFX 1.02 (docs/11). */
export async function fetchOfx(accountId: number, vault: VaultState): Promise<Blob> {
  const response = await apiBinary(`${OFX_PATH}?account_id=${accountId}&vault=${vault}`);
  return response.blob();
}

/** Downloads the OFX statement; resolves with the filename that was used. */
export async function downloadOfxExport(
  accountId: number,
  vault: VaultState,
): Promise<string> {
  const response = await apiBinary(`${OFX_PATH}?account_id=${accountId}&vault=${vault}`);
  const blob = await response.blob();
  const filename = filenameFromDisposition(
    response.headers.get("content-disposition"),
    `fin-tracker-account-${accountId}.ofx`,
  );
  saveBlob(blob, filename);
  return filename;
}

export function getExportSchedule(): Promise<ExportSchedule> {
  return api<ExportSchedule>(EXPORT_SCHEDULE_PATH);
}

export function putExportSchedule(schedule: ExportSchedule): Promise<ExportSchedule> {
  return api<ExportSchedule>(EXPORT_SCHEDULE_PATH, { method: "PUT", body: schedule });
}
