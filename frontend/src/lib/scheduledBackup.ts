/**
 * Scheduled backup — the run step (M56, docs/11 § "Due semantics"). The step
 * takes its clock and I/O as injected dependencies, so the schedule rules are
 * testable without a server, a vault or a timer
 * (frontend/tests/scheduledBackup.test.ts).
 *
 * There is deliberately no server-side scheduler: only the browser holds the
 * DEK, so a run needs an open tab (docs/11 storage decision). Missed periods
 * are not backfilled one by one — the next open produces one artifact for the
 * then-current period.
 *
 * The period/due math (`periodFor`, `nextDue`, `isDue`) now lives in
 * `scheduleMath.ts`, shared with M75's scheduled-export runner (docs/11) —
 * one implementation, so the weekday-encoding bridge and the ISO week-year
 * boundary cannot drift between the two runners. This module re-exports them,
 * so its public API (and its test file) are unchanged.
 */

import type { BackupArtifact, BackupSchedule } from "../api/backup";
import type { ArtifactWire } from "../api/backup";
import { ArtifactTooLargeError, buildPayload, encryptArtifact } from "./backupArtifact";
import { isDue, periodFor } from "./scheduleMath";

export { isDue, nextDue, periodFor } from "./scheduleMath";

/** Runner cadence: AppShell mounts the runner and re-ticks every 5 minutes. */
export const TICK_MS = 300_000;

/**
 * What one tick did. `export_failed` / `failed` / `too_large` / `vault_locked`
 * are all retried on the next tick — no artifact was written for them.
 */
export type BackupRunOutcome =
  | "off"
  | "skipped"
  | "vault_locked"
  | "too_large"
  | "export_failed"
  | "failed"
  | "uploaded";

export interface ScheduledBackupDeps {
  /** Injected clock — every due computation reads time through here. */
  now(): Date;
  getSchedule(): Promise<BackupSchedule>;
  /** Metadata only, newest first (the server's list endpoint). */
  listArtifacts(): Promise<{ items: BackupArtifact[] }>;
  /** `GET /export/full` — no LLM/market-data dependency (docs/11 step 7). */
  exportSnapshot(): Promise<unknown>;
  /** Session DEK, or `null` while the vault is locked (docs/33). */
  getDEK(): CryptoKey | null;
  upload(wire: ArtifactWire): Promise<unknown>;
}

/**
 * One tick. Order is the spec's: schedule → due? → vault → export → encrypt
 * → upload. Every failure mode returns a distinct outcome with NO artifact
 * written, so the next tick simply tries again.
 */
export async function runScheduledBackup(
  deps: ScheduledBackupDeps,
  options: { force?: boolean } = {},
): Promise<BackupRunOutcome> {
  const schedule = await deps.getSchedule();
  if (schedule.frequency === "off") return "off";

  const now = deps.now();
  const latest = (await deps.listArtifacts()).items[0] ?? null;
  if (!options.force && !isDue(schedule, latest?.created_at ?? null, now)) {
    return "skipped";
  }

  const dek = deps.getDEK();
  // Due but locked: no artifact, retried next tick. There is no server-side
  // fallback — the server cannot encrypt with a key it does not have.
  if (dek === null) return "vault_locked";

  let snapshot: unknown;
  try {
    snapshot = await deps.exportSnapshot();
  } catch {
    return "export_failed";
  }

  let wire: ArtifactWire;
  try {
    // `exported_at` is the real run time, never a backfilled due instant.
    const payload = buildPayload(
      snapshot,
      periodFor(schedule.frequency, now),
      now,
    );
    wire = await encryptArtifact(payload, dek);
  } catch (err) {
    if (err instanceof ArtifactTooLargeError) return "too_large";
    // Malformed export body (error envelope, truncated JSON) → retry.
    return "export_failed";
  }

  try {
    await deps.upload(wire);
  } catch {
    return "failed";
  }
  return "uploaded";
}
