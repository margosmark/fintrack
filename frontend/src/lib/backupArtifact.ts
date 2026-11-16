/**
 * Artifact envelope for scheduled backup (M56, docs/11 § "Artifact format").
 *
 * The payload is one JSON object — `v`, `kind`, `period`, `exported_at`,
 * `snapshot` — and EVERY field lives inside the ciphertext, so a hostile
 * server cannot alter the period or metadata without breaking AES-GCM
 * authentication. Encryption is `encryptField` from `fields.ts` (the existing
 * `v1.<base64(iv)>.<base64(ciphertext)>` field envelope, AES-GCM-256, fresh
 * 96-bit IV) under the session DEK from `vault.ts::getDEK()`. No new cipher
 * and no second key hierarchy: the DEK never leaves the tab (docs/33).
 *
 * `snapshot` is exactly the `GET /export/full` response body — the backend
 * derives its table set from the model registry, so this module deliberately
 * keeps NO table list of its own (the same audit finding as
 * `privacyBackup.ts`: a duplicated list is what makes a backup silently
 * incomplete).
 */

import { decryptField, encryptField } from "./fields";
import type { ArtifactWire } from "../api/backup";

/** Mirrors `backend/app/api/backup.py::MAX_ARTIFACT_CHARS` — raise both. */
export const MAX_ARTIFACT_CHARS = 8_000_000;

export const BACKUP_KIND = "fintrack-backup";
export const BACKUP_VERSION = 1;

/** Plaintext of one artifact, before `encryptField` (docs/11 pinned shape). */
export interface BackupPayload {
  v: number;
  kind: string;
  period: string;
  exported_at: string;
  snapshot: Record<string, unknown[]>;
}

/**
 * Shape check only: a plain object whose values are all row arrays. The KEY
 * set is owned by the backend registry (M13 audit) — never check it here.
 * Rejecting a malformed body means we never store an unrestorable backup.
 */
export function isExportSnapshot(value: unknown): value is Record<string, unknown[]> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const rows = Object.values(value);
  return rows.length > 0 && rows.every((value_) => Array.isArray(value_));
}

/** The artifact is too big for the request body (nginx `10m`, docs/11). */
export class ArtifactTooLargeError extends Error {
  readonly size: number;
  readonly limit: number;

  constructor(size: number, limit: number) {
    super(`Backup artifact is ${size} characters — over the ${limit} limit.`);
    this.name = "ArtifactTooLargeError";
    this.size = size;
    this.limit = limit;
  }
}

/** Throws `ArtifactTooLargeError` past `MAX_ARTIFACT_CHARS` (docs/11). */
export function assertArtifactSize(ciphertext: string): void {
  if (ciphertext.length > MAX_ARTIFACT_CHARS) {
    throw new ArtifactTooLargeError(ciphertext.length, MAX_ARTIFACT_CHARS);
  }
}

/**
 * Builds the plaintext payload for one run. `exported_at` is the REAL run
 * time (missed periods are never backfilled — docs/11 due semantics).
 * Throws when the export body is not a snapshot, so an error envelope or a
 * truncated body can never masquerade as a backup.
 */
export function buildPayload(
  snapshot: unknown,
  period: string,
  exportedAt: Date,
): BackupPayload {
  if (!isExportSnapshot(snapshot)) {
    throw new Error("Refusing to back up a malformed export snapshot.");
  }
  return {
    v: BACKUP_VERSION,
    kind: BACKUP_KIND,
    period,
    exported_at: exportedAt.toISOString(),
    snapshot,
  };
}

/** Encrypts the payload under the DEK and returns the wire object to POST. */
export async function encryptArtifact(
  payload: BackupPayload,
  dek: CryptoKey,
): Promise<ArtifactWire> {
  const ciphertext = await encryptField(JSON.stringify(payload), dek);
  assertArtifactSize(ciphertext);
  return { period: payload.period, ciphertext };
}

/**
 * Decrypts a stored artifact's ciphertext back into the payload. Throws on a
 * wrong key or tampered bytes (AES-GCM authentication), and on a payload that
 * is not a `fintrack-backup` document — a restore must never accept a
 * ciphertext that decrypts to something else.
 */
export async function decryptArtifact(
  ciphertext: string,
  dek: CryptoKey,
): Promise<BackupPayload> {
  const plaintext = await decryptField(ciphertext, dek);
  let parsed: unknown;
  try {
    parsed = JSON.parse(plaintext);
  } catch {
    throw new Error("Backup artifact is not valid JSON.");
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Backup artifact is not a payload object.");
  }
  const payload = parsed as Partial<BackupPayload>;
  if (
    payload.v !== BACKUP_VERSION ||
    payload.kind !== BACKUP_KIND ||
    typeof payload.period !== "string" ||
    typeof payload.exported_at !== "string" ||
    !isExportSnapshot(payload.snapshot)
  ) {
    throw new Error("Not a fin-track backup payload.");
  }
  return payload as BackupPayload;
}

/** Parses a `.ftbackup.json` file the user picked (offline escape hatch). */
export async function readWireFile(file: File): Promise<ArtifactWire> {
  const text = await file.text();
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("That file is not JSON.");
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("That file is not a backup artifact.");
  }
  const wire = parsed as Partial<ArtifactWire>;
  if (typeof wire.period !== "string" || typeof wire.ciphertext !== "string") {
    throw new Error("That file is not a backup artifact.");
  }
  assertArtifactSize(wire.ciphertext);
  return { period: wire.period, ciphertext: wire.ciphertext };
}
