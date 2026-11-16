/**
 * Encrypted local backup (docs/11 step 4): stores a full-export snapshot in
 * IndexedDB, encrypted with Web Crypto (PBKDF2-derived AES-GCM key). Only
 * `{ salt, iv, ciphertext, createdAt }` is persisted — never the passphrase.
 * The AES-GCM encrypt/decrypt helpers (`encryptSnapshot`/`decryptSnapshot`) are
 * exported so the round-trip is reusable and testable without IndexedDB; the
 * UI remains write-only in this scope (no restore surface) and the downloadable
 * export stays the durable copy (docs/11 step 7).
 *
 * The snapshot stored here is the opaque `GET /export/full` payload: one
 * row-array per table, and the backend derives that table set from its model
 * registry (audit M13 — budgets, goals, debts, tags, etf_holdings,
 * fundamentals and every other post-M7 table included). Deliberately NO
 * client-side table-key list exists here: a duplicated list is what made the
 * backup silently incomplete before, and it would drift again. This module
 * only validates the payload SHAPE below, never the key set.
 */

const DB_NAME = "fintrack-backup";
const DB_VERSION = 1;
const STORE = "backups";

const PBKDF2_ITERATIONS = 600_000;
const SALT_BYTES = 16;
const IV_BYTES = 12;

/** Exactly what reaches disk (docs/11 step 4) — no plaintext, no passphrase. */
export interface StoredBackup {
  salt: Uint8Array;
  iv: Uint8Array;
  ciphertext: ArrayBuffer;
  /** ISO timestamp of when the snapshot was taken. */
  createdAt: string;
}

/**
 * A full-export snapshot: `{ [table]: row[] }` (docs/11 step 2). The key set
 * is owned by the backend's registry-driven export, so it is intentionally
 * open-ended here — this type only pins the two-level shape.
 */
export type ExportSnapshot = Record<string, unknown[]>;

/**
 * Shape check for a snapshot about to be backed up: a plain object whose
 * values are all row arrays. Rejecting anything else means we never persist a
 * backup that could not be restored (docs/11 step 4 lost-passphrase guarantee)
 * — e.g. an error envelope or a truncated body. The key set is NOT checked:
 * coverage comes from the backend registry (audit M13), not from a list here
 * that could go stale.
 */
function isExportSnapshot(value: unknown): value is ExportSnapshot {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  return Object.values(value).every((rows) => Array.isArray(rows));
}

/** Object-store key for a user's backup record (docs/11 step 4). */
function backupKey(userId: string): string {
  return `user_${userId}`;
}

/**
 * Web Crypto subtle needs a secure context and IndexedDB is not present in all
 * embedding contexts; when either is missing the UI falls back to
 * download-only (docs/11 step 4 / contingencies).
 */
export function backupSupported(): boolean {
  return (
    typeof crypto !== "undefined" &&
    crypto.subtle !== undefined &&
    typeof indexedDB !== "undefined"
  );
}

async function deriveKey(passphrase: string, salt: Uint8Array): Promise<CryptoKey> {
  const material = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(passphrase),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

function openBackups(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
  });
}

function requestToPromise<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const db = await openBackups();
  try {
    const tx = db.transaction(STORE, mode);
    return await requestToPromise(run(tx.objectStore(STORE)));
  } finally {
    db.close();
  }
}

function isStoredBackup(value: unknown): value is StoredBackup {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Partial<StoredBackup>;
  return (
    record.salt instanceof Uint8Array &&
    record.iv instanceof Uint8Array &&
    record.ciphertext instanceof ArrayBuffer &&
    typeof record.createdAt === "string"
  );
}

/**
 * Pure AES-GCM encrypt step (no IndexedDB): derives a fresh salt + IV from the
 * passphrase, encrypts the JSON-serialized snapshot, and returns the on-disk
 * `StoredBackup` shape. Exported so the round-trip is reusable and testable
 * independent of IndexedDB (docs/11 step 4).
 */
export async function encryptSnapshot(
  snapshot: unknown,
  passphrase: string,
): Promise<StoredBackup> {
  const salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES));
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const key = await deriveKey(passphrase, salt);
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    new TextEncoder().encode(JSON.stringify(snapshot)),
  );
  return { salt, iv, ciphertext, createdAt: new Date().toISOString() };
}

/**
 * Persists a snapshot under `user_<userId>`, replacing any previous backup
 * (docs/11 step 4). The encryption itself lives in `encryptSnapshot`.
 * Throws when the payload is not a table-keyed export snapshot, so a
 * malformed response can never masquerade as a usable backup.
 */
export async function storeBackup(
  userId: string,
  snapshot: unknown,
  passphrase: string,
): Promise<void> {
  if (!isExportSnapshot(snapshot)) {
    throw new Error("Refusing to back up a malformed export snapshot.");
  }
  const record = await encryptSnapshot(snapshot, passphrase);
  await withStore("readwrite", (store) => store.put(record, backupKey(userId)));
}

/**
 * Pure AES-GCM decrypt step (no IndexedDB): validates the record shape, derives
 * the key from the passphrase, and returns the decrypted snapshot. Throws on a
 * malformed record or a wrong/corrupted passphrase (AES-GCM authentication
 * failure). The UI has no restore surface in this scope; the downloadable
 * export stays the durable copy (docs/11 step 7).
 */
export async function decryptSnapshot(
  record: StoredBackup,
  passphrase: string,
): Promise<unknown> {
  if (!isStoredBackup(record)) {
    throw new Error("Invalid backup record.");
  }
  try {
    const key = await deriveKey(passphrase, record.salt);
    const plaintext = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: record.iv },
      key,
      record.ciphertext,
    );
    return JSON.parse(new TextDecoder().decode(plaintext));
  } catch {
    throw new Error("Decryption failed — wrong passphrase or corrupted backup.");
  }
}

/** Removes the stored backup; disabling the toggle calls this (docs/11 step 4). */
export async function deleteBackup(userId: string): Promise<void> {
  await withStore("readwrite", (store) => store.delete(backupKey(userId)));
}

/**
 * Backup presence plus the `createdAt` timestamp for the Settings display.
 * Returns null when no backup is stored. Reading metadata never needs the
 * passphrase, so the timestamp can show without unlocking anything.
 */
export async function hasBackup(userId: string): Promise<StoredBackup | null> {
  const stored = await withStore("readonly", (store) =>
    store.get(backupKey(userId)),
  );
  return isStoredBackup(stored) ? stored : null;
}
