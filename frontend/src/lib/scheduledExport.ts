/**
 * Scheduled export — the run step (M75, docs/11 § "Due semantics"). Mirror of
 * `scheduledBackup.ts` minus encryption: the ZIP is stored LOCALLY (IndexedDB,
 * see `localExports.ts`) instead of uploaded, so a run needs no vault key and
 * writes no server-side copy.
 *
 * Same injected clock and I/O, same order — schedule → due? → export →
 * store — so the two runners can be compared line by line. No backfill: a
 * missed period is skipped, and the next open tab produces one archive for
 * the then-current period (docs/11).
 *
 * A locked vault does NOT block a run: the server's fail-closed default
 * (`vault=locked`) writes `E2EE:<ciphertext>` where the plaintext would be,
 * which is an honest archive — exactly what the app itself would render for
 * that session. Passing `vault=unlocked` is what the Settings button does
 * when the vault is open.
 */

import type { ExportSchedule, VaultState } from "../api/export";
import type { StoredExportMeta } from "./localExports";
import { isDue, periodFor } from "./scheduleMath";

/** Runner cadence: AppShell mounts the runner and re-ticks every 5 minutes. */
export const EXPORT_TICK_MS = 300_000;

/** What one tick did. `export_failed` / `store_failed` write nothing and are
 * retried on the next tick; `storage_unsupported` is terminal for the tab. */
export type ExportRunOutcome =
  | "off"
  | "skipped"
  | "stored"
  | "export_failed"
  | "store_failed"
  | "storage_unsupported";

export interface NewStoredExport extends StoredExportMeta {
  blob: Blob;
}

export interface ScheduledExportDeps {
  /** Injected clock — every due computation reads time through here. */
  now(): Date;
  getSchedule(): Promise<ExportSchedule>;
  /** Metadata only, newest first. */
  listExports(): Promise<StoredExportMeta[]>;
  /** `GET /export/zip` — a read, so it works under privacy mode (docs/11). */
  exportZip(vault: VaultState): Promise<Blob>;
  /** IndexedDB upsert + keep-last-N; rejects when storage is unavailable. */
  storeExport(record: NewStoredExport, keepLastN: number): Promise<unknown>;
  /** This session's vault state (`locked` when the vault is closed). */
  vaultState(): VaultState;
}

/**
 * One tick. Returns `stored` only after the blob is in IndexedDB, so the
 * panel's list is the single visible record of what actually succeeded.
 */
export async function runScheduledExport(
  deps: ScheduledExportDeps,
  options: { force?: boolean } = {},
): Promise<ExportRunOutcome> {
  const schedule = await deps.getSchedule();
  const frequency = schedule.frequency;
  if (frequency === "off") return "off";

  const now = deps.now();
  const latest = (await deps.listExports())[0] ?? null;
  if (!options.force && !isDue(schedule, latest?.createdAt ?? null, now)) {
    return "skipped";
  }

  let blob: Blob;
  try {
    blob = await deps.exportZip(deps.vaultState());
  } catch {
    return "export_failed";
  }

  const period = periodFor(frequency, now);
  try {
    await deps.storeExport(
      {
        period,
        createdAt: now.toISOString(),
        sizeBytes: blob.size,
        filename: `fin-tracker-export-${period}.zip`,
        blob,
      },
      schedule.keep_last_n,
    );
  } catch {
    return "store_failed";
  }
  return "stored";
}
