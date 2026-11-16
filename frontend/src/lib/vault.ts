/**
 * Session vault (docs/33 Phase 1): holds the user's data-encryption key (DEK)
 * and blind-index key in memory ONLY — never persisted, never sent anywhere.
 *
 * Key hierarchy: encryption passphrase -> PBKDF2 KEK -> unwraps DEK + index
 * key. The wrapped bundle (`{ salt, wrappedDEK, wrappedIndexKey, createdAt }`)
 * is opaque to anyone without the passphrase and lives in IndexedDB
 * (per-browser, same precedent as the docs/11 local backup). Forgetting the
 * passphrase is unrecoverable by construction — the export/backup is the
 * safety net.
 */

const DB_NAME = "fintrack-vault";
const DB_VERSION = 1;
const STORE = "keys";

const PBKDF2_ITERATIONS = 600_000;
const SALT_BYTES = 16;
const IV_BYTES = 12;

/** Opaque persisted bundle — useless without the passphrase. */
export interface WrappedVault {
  salt: Uint8Array;
  wrappedDEK: ArrayBuffer;
  wrappedIndexKey: ArrayBuffer;
  createdAt: string;
}

function vaultKey(userId: string): string {
  return `user_${userId}`;
}

async function deriveKEK(passphrase: string, salt: Uint8Array): Promise<CryptoKey> {
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

/**
 * Wrap IVs are derived deterministically from the salt (fresh-random per
 * vault, so no (key, iv) pair repeats across vaults) with a domain-separation
 * byte so the DEK and index key never share an IV under the same KEK.
 */
function wrapIV(salt: Uint8Array, domain: number): Uint8Array {
  const iv = new Uint8Array(IV_BYTES);
  iv.set(salt.subarray(0, Math.min(SALT_BYTES, IV_BYTES)));
  iv[IV_BYTES - 1] ^= domain;
  return iv;
}

const DEK_DOMAIN = 0x01;
const INDEX_DOMAIN = 0x02;

async function unwrap(
  kek: CryptoKey,
  salt: Uint8Array,
  wrapped: ArrayBuffer,
  domain: number,
): Promise<CryptoKey> {
  const raw = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: wrapIV(salt, domain) },
    kek,
    wrapped,
  );
  // EXTRACTABLE on purpose (docs/33 Phase 4, audit finding F1): the session
  // index key must be exportable so `indexToken()` can re-import it as an HMAC
  // signing key, and the session DEK must be exportable so passphrase rotation
  // can re-wrap the same key bytes under a new KEK. A non-extractable import
  // broke both paths after any lock→unlock cycle. The threat model excludes a
  // compromised bundle, and non-extractable keys do not stop an attacker who
  // can already run JavaScript in this page.
  return crypto.subtle.importKey("raw", raw, "AES-GCM", true, [
    "encrypt",
    "decrypt",
  ]);
}

// ── Session memory (the only place raw keys exist) ──────────────────────────

let sessionDEK: CryptoKey | null = null;
let sessionIndexKey: CryptoKey | null = null;

/** True while raw keys are held in memory. */
export function isUnlocked(): boolean {
  return sessionDEK !== null && sessionIndexKey !== null;
}

/** Raw DEK for field encryption; throws when the vault is locked. */
export function getDEK(): CryptoKey {
  if (sessionDEK === null) throw new Error("Vault is locked.");
  return sessionDEK;
}

/** Raw blind-index key; throws when the vault is locked. */
export function getIndexKey(): CryptoKey {
  if (sessionIndexKey === null) throw new Error("Vault is locked.");
  return sessionIndexKey;
}

/**
 * Drops all raw key material from memory. CryptoKey objects are
 * non-extractable, so dropping every reference is the complete erasure
 * available to us — after this, decryption requires the passphrase again.
 */
export function lockVault(): void {
  sessionDEK = null;
  sessionIndexKey = null;
  dispatchVaultChange();
}

/** Notifies UI listeners (hooks) that lock state changed. No key material. */
function dispatchVaultChange(): void {
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    window.dispatchEvent(new CustomEvent("vaultchange"));
  }
}

// ── Enrollment + unlock ─────────────────────────────────────────────────────

function randomKey(): Promise<CryptoKey> {
  return crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, [
    "encrypt",
    "decrypt",
  ]);
}

/**
 * Wraps a DEK + index key under `passphrase` + `salt`. Shared by first-time
 * enrollment (`createVault`) and by Phase-4 sync/rotation (`wrapSessionVault`)
 * so both produce byte-identical bundle shapes.
 */
async function wrapKeys(
  passphrase: string,
  salt: Uint8Array,
  dek: CryptoKey,
  indexKey: CryptoKey,
): Promise<WrappedVault> {
  const kek = await deriveKEK(passphrase, salt);
  const wrap = async (key: CryptoKey, domain: number): Promise<ArrayBuffer> => {
    const raw = await crypto.subtle.exportKey("raw", key);
    return crypto.subtle.encrypt({ name: "AES-GCM", iv: wrapIV(salt, domain) }, kek, raw);
  };
  const [wrappedDEK, wrappedIndexKey] = await Promise.all([
    wrap(dek, DEK_DOMAIN),
    wrap(indexKey, INDEX_DOMAIN),
  ]);
  return { salt, wrappedDEK, wrappedIndexKey, createdAt: new Date().toISOString() };
}

/**
 * Creates a fresh vault: random DEK + index key, wrapped under a
 * passphrase-derived KEK. Leaves the vault unlocked in this session and
 * returns the opaque bundle for persistence.
 */
export async function createVault(passphrase: string): Promise<WrappedVault> {
  const salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES));
  const dek = await randomKey();
  const indexKey = await randomKey();
  const bundle = await wrapKeys(passphrase, salt, dek, indexKey);
  sessionDEK = dek;
  sessionIndexKey = indexKey;
  dispatchVaultChange();
  return bundle;
}

/**
 * Wraps the keys ALREADY in this session under `passphrase` + `salt`
 * (fresh random salt when omitted) — the Phase-4 operations:
 * - enrollment on a second browser, after unlocking from another device's
 *   envelope: re-wrap the same DEK so this device gets its own row, and
 * - passphrase rotation: re-wrap the same DEK under a new KEK (fresh salt per
 *   device), which rewrites no ciphertext at all (docs/33 Phase 4).
 * Throws when the vault is locked.
 */
export async function wrapSessionVault(
  passphrase: string,
  opts: { salt?: Uint8Array } = {},
): Promise<WrappedVault> {
  const dek = getDEK();
  const indexKey = getIndexKey();
  const salt = opts.salt ?? crypto.getRandomValues(new Uint8Array(SALT_BYTES));
  return wrapKeys(passphrase, salt, dek, indexKey);
}

/**
 * Unlocks from a stored bundle. Throws (and stays locked) on a wrong
 * passphrase — AES-GCM authentication failure on unwrap.
 */
export async function unlockVault(
  passphrase: string,
  stored: WrappedVault,
): Promise<void> {
  const kek = await deriveKEK(passphrase, stored.salt);
  const [dek, indexKey] = await Promise.all([
    unwrap(kek, stored.salt, stored.wrappedDEK, DEK_DOMAIN),
    unwrap(kek, stored.salt, stored.wrappedIndexKey, INDEX_DOMAIN),
  ]);
  sessionDEK = dek;
  sessionIndexKey = indexKey;
  dispatchVaultChange();
}

// ── Wrapped-bundle persistence (opaque to the server/disk thief) ────────────

function openVaultDb(): Promise<IDBDatabase> {
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

function isWrappedVault(value: unknown): value is WrappedVault {
  if (typeof value !== "object" || value === null) return false;
  const r = value as Partial<WrappedVault>;
  return (
    r.salt instanceof Uint8Array &&
    r.wrappedDEK instanceof ArrayBuffer &&
    r.wrappedIndexKey instanceof ArrayBuffer &&
    typeof r.createdAt === "string"
  );
}

/** Persists the opaque wrapped bundle (never raw keys). */
export async function storeVault(userId: string, vault: WrappedVault): Promise<void> {
  const db = await openVaultDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      const req = tx.objectStore(STORE).put(vault, vaultKey(userId));
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error ?? new Error("Vault store failed"));
    });
  } finally {
    db.close();
  }
}

/** Loads the opaque wrapped bundle, or null when no vault is enrolled. */
export async function loadVault(userId: string): Promise<WrappedVault | null> {
  const db = await openVaultDb();
  try {
    const stored: unknown = await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(vaultKey(userId));
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error ?? new Error("Vault load failed"));
    });
    return isWrappedVault(stored) ? stored : null;
  } finally {
    db.close();
  }
}

/**
 * Deletes this browser's local copy of the bundle (Phase 4 revocation): a
 * device the account has revoked must not keep a usable mirror on its own disk
 * (docs/33 revoke runbook). Idempotent.
 */
export async function clearVault(userId: string): Promise<void> {
  const db = await openVaultDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      const req = tx.objectStore(STORE).delete(vaultKey(userId));
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error ?? new Error("Vault clear failed"));
    });
  } finally {
    db.close();
  }
}
