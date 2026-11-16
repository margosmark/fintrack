import { api } from "./client";

/**
 * Backup endpoints (M56, docs/11 § "Scheduled backup & restore"): schedule
 * round trip, artifact list/upload/download/delete and restore.
 *
 * The server only ever receives `ciphertext` — a `v1.` AES-GCM envelope
 * produced in the browser under the vault DEK — plus `period` and
 * `created_at`. It never sees a passphrase, the KEK or the DEK (docs/33
 * key-holding model), so nothing here can decrypt anything.
 */

export type BackupFrequency = "off" | "daily" | "weekly" | "monthly";

/** The five `users` columns behind `GET/PUT /backup/schedule` (docs/11). */
export interface BackupSchedule {
  frequency: BackupFrequency;
  /** 0–23, local time of the machine running the tab. */
  hour: number;
  /** 0–6, 0 = Monday (weekly only). */
  weekday: number;
  /** 1–28 (monthly only). */
  day_of_month: number;
  /** 1–20 retention. */
  keep_last_n: number;
}

/** Metadata row — the list endpoint never carries `ciphertext`. */
export interface BackupArtifact {
  id: number;
  period: string;
  created_at: string | null;
  size_bytes: number;
}

export interface BackupArtifactDetail extends BackupArtifact {
  ciphertext: string;
}

/** Exactly what `POST /backup/artifacts` accepts and a `.ftbackup.json` holds. */
export interface ArtifactWire {
  period: string;
  ciphertext: string;
}

export interface ArtifactList {
  items: BackupArtifact[];
}

export interface RestoreResult {
  restored: Record<string, number>;
  absent_tables: string[];
  skipped_tables: string[];
  fx_rates_inserted: number;
}

export const SCHEDULE_PATH = "/backup/schedule";
export const ARTIFACTS_PATH = "/backup/artifacts";
export const RESTORE_PATH = "/backup/restore";

export function getSchedule(): Promise<BackupSchedule> {
  return api<BackupSchedule>(SCHEDULE_PATH);
}

export function putSchedule(schedule: BackupSchedule): Promise<BackupSchedule> {
  return api<BackupSchedule>(SCHEDULE_PATH, { method: "PUT", body: schedule });
}

/** Metadata only, newest first (docs/11) — cheap even at 8 MB per artifact. */
export function listArtifacts(): Promise<ArtifactList> {
  return api<ArtifactList>(ARTIFACTS_PATH);
}

export function uploadArtifact(wire: ArtifactWire): Promise<BackupArtifact> {
  return api<BackupArtifact>(ARTIFACTS_PATH, { method: "POST", body: wire });
}

export function getArtifact(id: number): Promise<BackupArtifactDetail> {
  return api<BackupArtifactDetail>(`${ARTIFACTS_PATH}/${id}`);
}

/** 204 — `api` resolves the empty body to `undefined`. */
export function deleteArtifact(id: number): Promise<void> {
  return api<void>(`${ARTIFACTS_PATH}/${id}`, { method: "DELETE" });
}

/**
 * Posts the DECRYPTED payload for restore. The server cannot decrypt
 * artifacts (it has no key), so the browser does that step and sends the
 * plaintext payload back — confirm is the typed literal docs/11 pins so a
 * stray POST cannot wipe a tenant.
 */
export function restoreBackup(
  payload: unknown,
  confirm = "restore",
): Promise<RestoreResult> {
  return api<RestoreResult>(RESTORE_PATH, { method: "POST", body: { confirm, payload } });
}

/** `fin-tracker-backup-<period>.ftbackup.json` (docs/11 offline escape hatch). */
export function artifactFilename(period: string): string {
  return `fin-tracker-backup-${period}.ftbackup.json`;
}

/**
 * Saves one artifact to disk as the wire object
 * `{ period, ciphertext }` — still encrypted, so the file is as useless as
 * the server's copy without the vault passphrase.
 */
export function downloadArtifact(artifact: BackupArtifactDetail): string {
  const wire = { period: artifact.period, ciphertext: artifact.ciphertext };
  const blob = new Blob([JSON.stringify(wire, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = artifactFilename(artifact.period);
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
  return artifactFilename(artifact.period);
}
