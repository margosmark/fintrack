/**
 * Wrapped-DEK sync across devices (docs/33 Phase 4, M53).
 *
 * The account has ONE data-encryption key; each browser holds its own copy of
 * it wrapped under a KEK derived from the encryption passphrase + that
 * device's salt. The server stores those opaque blobs (`vault_envelopes`) and
 * can never open one. This module owns the client-side flows that turn that
 * into "a second browser unlocks with the same passphrase":
 *
 * - unlock is FAIL-CLOSED: it always asks the server first, so a device the
 *   account revoked cannot unlock from a stale local mirror,
 * - a device with no row of its own enrolls from the OLDEST active envelope
 *   (same passphrase ⇒ same DEK) and then creates its own row,
 * - a brand-new vault is only ever minted when the server positively reports
 *   zero active envelopes (two DEKs would make each device's ciphertext
 *   unreadable to the other),
 * - passphrase rotation re-wraps every active envelope under a new KEK and
 *   rewrites no ciphertext at all.
 *
 * Every operation takes an injectable `VaultSyncDeps`, so the multi-device
 * tests run against an in-memory fake that enforces the same rules as the
 * router (docs/33 Phase 4 → Verification).
 */

import type {
  VaultEnvelope,
  VaultEnvelopeCreate,
  VaultEnvelopeList,
  VaultEnvelopeUpdate,
} from "../api/vault";
import {
  createEnvelope,
  listEnvelopes,
  replaceEnvelope,
  revokeEnvelope as revokeEnvelopeApi,
} from "../api/vault";
import { fromBase64, toBase64 } from "./fields";
import {
  clearVault,
  createVault,
  isUnlocked,
  loadVault,
  lockVault,
  storeVault,
  unlockVault,
  wrapSessionVault,
  type WrappedVault,
} from "./vault";

/** localStorage key for this browser profile's device identity (not a secret). */
export const DEVICE_ID_KEY = "fintrack-device-id";
/** Client-side mirror of the server's `MAX_BLOB_CHARS` (docs/08). */
export const MAX_BLOB_CHARS = 8000;
/** Version byte of the synced bundle format. */
const BUNDLE_VERSION = 1;

// ── Device identity ──────────────────────────────────────────────────────────

let unpersistedDeviceId: string | null = null;

function randomHex(bytes: number): string {
  const buf = crypto.getRandomValues(new Uint8Array(bytes));
  return Array.from(buf)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Stable identity for this browser profile: 32 lowercase hex chars in
 * localStorage. Clearing site data mints a new one, which is deliberate —
 * that is how a revoked device re-enters (docs/33 revoke runbook).
 */
export function getDeviceId(): string {
  if (typeof localStorage === "undefined") {
    // No storage (tests/SSR): keep one id per session rather than per call.
    unpersistedDeviceId ??= randomHex(16);
    return unpersistedDeviceId;
  }
  const existing = localStorage.getItem(DEVICE_ID_KEY);
  if (existing !== null && /^[0-9a-f]{32}$/.test(existing)) return existing;
  const id = randomHex(16);
  localStorage.setItem(DEVICE_ID_KEY, id);
  return id;
}

/** Human label for the device list — plaintext metadata the server accepts. */
export function deviceLabel(): string {
  const ua = typeof navigator === "undefined" ? "" : navigator.userAgent;
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /OPR\//.test(ua)
      ? "Opera"
      : /Firefox\//.test(ua)
        ? "Firefox"
        : /Chrome\//.test(ua)
          ? "Chrome"
          : /Safari\//.test(ua)
            ? "Safari"
            : "Browser";
  const os = /Windows/.test(ua)
    ? "Windows"
    : /Android/.test(ua)
      ? "Android"
      : /iPhone|iPad|iPod/.test(ua)
        ? "iOS"
        : /Mac OS X/.test(ua)
          ? "macOS"
          : /Linux/.test(ua)
            ? "Linux"
            : "";
  return (os ? `${browser} · ${os}` : browser).slice(0, 64);
}

// ── Bundle encoding (the server sees only this string) ───────────────────────

function toArrayBuffer(u8: Uint8Array): ArrayBuffer {
  return u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength) as ArrayBuffer;
}

/** `WrappedVault` → base64url of `{v, salt, wrappedDEK, wrappedIndexKey, createdAt}`. */
export function encodeBundle(vault: WrappedVault): string {
  const json = JSON.stringify({
    v: BUNDLE_VERSION,
    salt: toBase64(vault.salt),
    wrappedDEK: toBase64(vault.wrappedDEK),
    wrappedIndexKey: toBase64(vault.wrappedIndexKey),
    createdAt: vault.createdAt,
  });
  return btoa(json).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * base64url → `WrappedVault`. Throws `Not a wrapped vault bundle.` on
 * anything that is not exactly the shape we wrote — a hostile or buggy server
 * handing back garbage must fail closed, never half-parse.
 */
export function decodeBundle(text: string): WrappedVault {
  let parsed: unknown;
  try {
    const b64 = text.replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    parsed = JSON.parse(atob(padded));
  } catch {
    throw new Error("Not a wrapped vault bundle.");
  }
  if (typeof parsed !== "object" || parsed === null) throw new Error("Not a wrapped vault bundle.");
  const r = parsed as Record<string, unknown>;
  if (
    r.v !== BUNDLE_VERSION ||
    typeof r.salt !== "string" ||
    typeof r.wrappedDEK !== "string" ||
    typeof r.wrappedIndexKey !== "string" ||
    typeof r.createdAt !== "string"
  ) {
    throw new Error("Not a wrapped vault bundle.");
  }
  let salt: Uint8Array;
  let wrappedDEK: Uint8Array;
  let wrappedIndexKey: Uint8Array;
  try {
    salt = fromBase64(r.salt);
    wrappedDEK = fromBase64(r.wrappedDEK);
    wrappedIndexKey = fromBase64(r.wrappedIndexKey);
  } catch {
    throw new Error("Not a wrapped vault bundle.");
  }
  if (salt.length === 0 || wrappedDEK.length === 0 || wrappedIndexKey.length === 0) {
    throw new Error("Not a wrapped vault bundle.");
  }
  return {
    salt,
    wrappedDEK: toArrayBuffer(wrappedDEK),
    wrappedIndexKey: toArrayBuffer(wrappedIndexKey),
    createdAt: r.createdAt,
  };
}

// ── Dependencies (injected so the flows are unit-testable) ───────────────────

export interface VaultSyncDeps {
  deviceId: string;
  userId: string;
  label: string;
  list(deviceId: string): Promise<VaultEnvelopeList>;
  create(payload: VaultEnvelopeCreate): Promise<VaultEnvelope>;
  replace(id: number, payload: VaultEnvelopeUpdate): Promise<VaultEnvelope>;
  revoke(id: number): Promise<VaultEnvelope>;
  loadLocal(userId: string): Promise<WrappedVault | null>;
  storeLocal(userId: string, bundle: WrappedVault): Promise<void>;
  clearLocal(userId: string): Promise<void>;
}

export function defaultSyncDeps(userId: string): VaultSyncDeps {
  return {
    deviceId: getDeviceId(),
    userId,
    label: deviceLabel(),
    list: listEnvelopes,
    create: createEnvelope,
    replace: replaceEnvelope,
    revoke: revokeEnvelopeApi,
    loadLocal: loadVault,
    storeLocal: storeVault,
    clearLocal: clearVault,
  };
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** Error-envelope `code` when the failure came from the API, else null. */
function errorCode(err: unknown): string | null {
  if (typeof err === "object" && err !== null && "code" in err) {
    const code = (err as { code: unknown }).code;
    if (typeof code === "string") return code;
  }
  return null;
}

function assertBlobSize(blob: string): void {
  if (blob.length > MAX_BLOB_CHARS) {
    throw new Error(`wrapped_blob exceeds ${MAX_BLOB_CHARS} characters`);
  }
}

// ── Unlock ───────────────────────────────────────────────────────────────────

export type UnlockFailure =
  | "wrong_passphrase"
  | "device_revoked"
  | "sync_unreachable"
  | "no_envelope";

export type UnlockResult =
  | { ok: true; source: "own" | "enrolled" | "local"; syncError?: string }
  | { ok: false; reason: UnlockFailure };

/**
 * Unlock this device against the account's envelopes (docs/33 Phase 4).
 * Fail-closed: no successful `GET` means no unlock.
 */
export async function unlockVaultSynced(
  passphrase: string,
  deps: VaultSyncDeps,
): Promise<UnlockResult> {
  let listing: VaultEnvelopeList;
  try {
    listing = await deps.list(deps.deviceId);
  } catch {
    return { ok: false, reason: "sync_unreachable" };
  }

  if (listing.revoked_own_device) {
    // This identity is cut off: drop the disk copy too (revoke runbook step 2).
    await deps.clearLocal(deps.userId);
    return { ok: false, reason: "device_revoked" };
  }

  const own = listing.items.find((row) => row.device_id === deps.deviceId);
  if (own != null && own.wrapped_blob != null) {
    let bundle: WrappedVault;
    try {
      bundle = decodeBundle(own.wrapped_blob);
      await unlockVault(passphrase, bundle);
    } catch {
      return { ok: false, reason: "wrong_passphrase" };
    }
    await deps.storeLocal(deps.userId, bundle);
    return { ok: true, source: "own" };
  }

  const donor = listing.items[0];
  if (donor != null && donor.wrapped_blob != null) {
    try {
      await unlockVault(passphrase, decodeBundle(donor.wrapped_blob));
    } catch {
      return { ok: false, reason: "wrong_passphrase" };
    }
    // Enrollment: same DEK, wrapped for THIS device under a fresh salt.
    let bundle: WrappedVault | null = null;
    let createError: unknown = null;
    try {
      bundle = await wrapSessionVault(passphrase);
      const blob = encodeBundle(bundle);
      assertBlobSize(blob);
      await deps.create({
        device_id: deps.deviceId,
        label: deps.label,
        wrapped_blob: blob,
      });
    } catch (err) {
      createError = err;
    }
    if (bundle !== null) await deps.storeLocal(deps.userId, bundle);
    if (createError !== null) {
      // The DEK is the donor's, so a local-only mirror introduces NO second
      // key hierarchy — keep it and let the next unlock retry the POST.
      // (This is unlike `enrollVaultSynced`, which mints a brand-new DEK.)
      return { ok: true, source: "enrolled", syncError: message(createError) };
    }
    return { ok: true, source: "enrolled" };
  }

  const local = await deps.loadLocal(deps.userId);
  if (local != null) {
    try {
      await unlockVault(passphrase, local);
    } catch {
      return { ok: false, reason: "wrong_passphrase" };
    }
    return { ok: true, source: "local" };
  }
  return { ok: false, reason: "no_envelope" };
}

// ── First-time enrollment ────────────────────────────────────────────────────

export type EnrollFailure =
  | "device_revoked"
  | "sync_unreachable"
  | "already_enrolled_elsewhere"
  | "sync_failed";

export type EnrollResult =
  | { ok: true; synced: true }
  | { ok: true; synced: false; code: "demo_account_restricted" }
  | { ok: false; reason: EnrollFailure; message: string };

/**
 * Mint the account's FIRST vault. A new DEK may only be created when the
 * server positively reports zero active envelopes — otherwise every device
 * would run its own key hierarchy and neither could read the other's rows.
 */
export async function enrollVaultSynced(
  passphrase: string,
  deps: VaultSyncDeps,
): Promise<EnrollResult> {
  let listing: VaultEnvelopeList;
  try {
    listing = await deps.list(deps.deviceId);
  } catch (err) {
    return { ok: false, reason: "sync_unreachable", message: message(err) };
  }
  if (listing.revoked_own_device) {
    return {
      ok: false,
      reason: "device_revoked",
      message: "This browser's key was revoked from another device.",
    };
  }
  if (listing.items.length > 0) {
    return {
      ok: false,
      reason: "already_enrolled_elsewhere",
      message: "This account already has a vault — unlock with your passphrase instead.",
    };
  }

  const bundle = await createVault(passphrase);
  try {
    const blob = encodeBundle(bundle);
    assertBlobSize(blob);
    await deps.create({ device_id: deps.deviceId, label: deps.label, wrapped_blob: blob });
  } catch (err) {
    if (errorCode(err) === "demo_account_restricted") {
      // The demo tenant cannot write (shared plaintext sample data): keep the
      // Phase-1 local-only vault and say so honestly.
      await deps.storeLocal(deps.userId, bundle);
      return { ok: true, synced: false, code: "demo_account_restricted" };
    }
    lockVault();
    return { ok: false, reason: "sync_failed", message: message(err) };
  }
  await deps.storeLocal(deps.userId, bundle);
  return { ok: true, synced: true };
}

// ── Revocation + passphrase rotation ─────────────────────────────────────────

export async function revokeEnvelope(id: number, deps: VaultSyncDeps): Promise<VaultEnvelope> {
  return deps.revoke(id);
}

export async function listVaultEnvelopes(deps: VaultSyncDeps): Promise<VaultEnvelopeList> {
  return deps.list(deps.deviceId);
}

export interface RotateResult {
  ok: boolean;
  /** Envelopes re-wrapped under the new passphrase (including this device). */
  replaced: number;
  /** Active envelopes known at the start of the run. */
  active: number;
  reason?: "vault_locked" | "sync_unreachable" | "sync_failed";
  message?: string;
}

/**
 * Change the encryption passphrase: re-wrap EVERY active envelope under a KEK
 * derived from the new passphrase + a fresh per-device salt. The DEK, the
 * index key and every ciphertext byte are untouched, so no data is rewritten.
 *
 * Order is recoverable at every step (docs/33 runbook): this device's envelope
 * goes first (aborting there leaves everyone on the old passphrase), then its
 * local mirror, then the rest — a partial failure leaves those devices on the
 * old passphrase and can simply be re-run.
 */
export async function rotatePassphrase(
  newPassphrase: string,
  deps: VaultSyncDeps,
): Promise<RotateResult> {
  if (!isUnlocked()) {
    return {
      ok: false,
      replaced: 0,
      active: 0,
      reason: "vault_locked",
      message: "Unlock the vault first.",
    };
  }
  let listing: VaultEnvelopeList;
  try {
    listing = await deps.list(deps.deviceId);
  } catch (err) {
    return { ok: false, replaced: 0, active: 0, reason: "sync_unreachable", message: message(err) };
  }
  const active = listing.active_count;
  const own = listing.items.find((row) => row.device_id === deps.deviceId);
  let replaced = 0;

  if (own != null && own.wrapped_blob != null) {
    try {
      const bundle = await wrapSessionVault(newPassphrase);
      const blob = encodeBundle(bundle);
      assertBlobSize(blob);
      await deps.replace(own.id, { wrapped_blob: blob });
      await deps.storeLocal(deps.userId, bundle);
      replaced += 1;
    } catch (err) {
      return { ok: false, replaced: 0, active, reason: "sync_failed", message: message(err) };
    }
  } else {
    // Local-only vault (demo / never synced): rotate just the disk copy.
    await deps.storeLocal(deps.userId, await wrapSessionVault(newPassphrase));
  }

  let lastError = "";
  for (const row of listing.items) {
    if (own != null && row.id === own.id) continue;
    try {
      const blob = encodeBundle(await wrapSessionVault(newPassphrase));
      assertBlobSize(blob);
      await deps.replace(row.id, { wrapped_blob: blob });
      replaced += 1;
    } catch (err) {
      lastError = message(err);
    }
  }
  if (lastError !== "") {
    return { ok: false, replaced, active, reason: "sync_failed", message: lastError };
  }
  return { ok: true, replaced, active };
}
