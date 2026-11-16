// @vitest-environment node
// Wrapped-DEK sync across devices — docs/33 Phase 4 (M53) → Verification #1-3.
//
// The fake server below enforces the SAME rules as backend/app/api/vault.py
// (active-only list, oldest-first order, revoked_own_device, device_exists,
// too_many_devices, envelope_too_large, tenancy), so a contract change on
// either side breaks this file rather than production. Local key stores are
// per-device maps, which is what IndexedDB would give us across two browser
// profiles — the point of the whole feature.
import { describe, expect, it } from "vitest";
import { decryptField, encryptField, indexToken } from "../src/lib/fields";
import { createVault, getDEK, lockVault } from "../src/lib/vault";
import {
  decodeBundle,
  deviceLabel,
  encodeBundle,
  enrollVaultSynced,
  getDeviceId,
  MAX_BLOB_CHARS,
  revokeEnvelope,
  rotatePassphrase,
  unlockVaultSynced,
  type VaultSyncDeps,
} from "../src/lib/vaultSync";
import type { VaultEnvelope, VaultEnvelopeList } from "../src/api/vault";

const PASSPHRASE = "correct horse battery";
const OTHER_PASSPHRASE = "a totally different phrase";

// ── Fake server (mirrors the router's rules, docs/08 § Vault key sync) ───────

interface FakeRow extends VaultEnvelope {
  user_id: string;
}

class FakeVaultServer {
  rows: FakeRow[] = [];
  nextId = 1;
  /** When set, every create() rejects with this envelope `code`. */
  failCreateCode: string | null = null;
  /** When set, list() rejects (network / 5xx → the client must fail closed). */
  failList = false;
  /** Rows whose replace() fails (partial-rotation scenario). */
  failReplaceIds = new Set<number>();

  list = async (deviceId: string): Promise<VaultEnvelopeList> => {
    if (this.failList) throw new Error("network down");
    const items = this.rows
      .filter((r) => r.revoked_at === null)
      .sort((a, b) => {
        const ka = a.created_at ?? "";
        const kb = b.created_at ?? "";
        return ka === kb ? a.id - b.id : ka < kb ? -1 : 1;
      })
      .map(({ user_id: _u, ...public_ }) => public_);
    const rowsHere = this.rows.filter((r) => r.device_id === deviceId);
    return {
      items,
      revoked_own_device:
        !items.some((r) => r.device_id === deviceId) && rowsHere.length > 0,
      active_count: items.length,
    };
  };

  create = async (payload: {
    device_id: string;
    label?: string;
    wrapped_blob: string;
  }): Promise<VaultEnvelope> => {
    if (this.failCreateCode !== null) throw { code: this.failCreateCode };
    if (!/^[0-9a-f]{32}$/.test(payload.device_id)) {
      throw { code: "validation_error" };
    }
    if (payload.wrapped_blob.length > MAX_BLOB_CHARS) {
      throw { code: "envelope_too_large" };
    }
    if (
      this.rows.some((r) => r.device_id === payload.device_id && r.revoked_at === null)
    ) {
      throw { code: "device_exists" };
    }
    if (this.rows.filter((r) => r.revoked_at === null).length >= 10) {
      throw { code: "too_many_devices" };
    }
    const now = new Date(this.nextId * 1000).toISOString();
    const row: FakeRow = {
      id: this.nextId++,
      user_id: "u1",
      device_id: payload.device_id,
      label: payload.label ?? null,
      wrapped_blob: payload.wrapped_blob,
      revoked_at: null,
      created_at: now,
      updated_at: now,
    };
    this.rows.push(row);
    const { user_id: _u, ...public_ } = row;
    return public_;
  };

  replace = async (
    id: number,
    payload: { wrapped_blob: string; label?: string | null },
  ): Promise<VaultEnvelope> => {
    const row = this.rows.find((r) => r.id === id);
    if (row === undefined || row.revoked_at !== null) throw { code: "not_found" };
    if (this.failReplaceIds.has(id)) throw { code: "not_found" };
    if (payload.wrapped_blob.length > MAX_BLOB_CHARS) {
      throw { code: "envelope_too_large" };
    }
    row.wrapped_blob = payload.wrapped_blob;
    if (payload.label !== undefined) row.label = payload.label;
    row.updated_at = new Date(this.nextId * 1000).toISOString();
    const { user_id: _u, ...public_ } = row;
    return public_;
  };

  revoke = async (id: number): Promise<VaultEnvelope> => {
    const row = this.rows.find((r) => r.id === id);
    if (row === undefined) throw { code: "not_found" };
    if (row.revoked_at === null) {
      row.revoked_at = new Date(this.nextId * 1000).toISOString();
      row.wrapped_blob = null;
      row.updated_at = row.revoked_at;
    }
    const { user_id: _u, ...public_ } = row;
    return public_;
  };
}

// ── Per-device deps (in-memory local store, like a browser profile) ──────────

interface Device {
  deps: VaultSyncDeps;
  local: Map<string, unknown>;
}

function makeDevice(server: FakeVaultServer, deviceId: string, label: string): Device {
  const local = new Map<string, unknown>();
  const deps: VaultSyncDeps = {
    deviceId,
    userId: "u1",
    label,
    list: server.list,
    create: server.create,
    replace: server.replace,
    revoke: server.revoke,
    loadLocal: async (userId) => (local.get(userId) ?? null) as never,
    storeLocal: async (userId, bundle) => {
      local.set(userId, bundle);
    },
    clearLocal: async (userId) => {
      local.delete(userId);
    },
  };
  return { deps, local };
}

const DEVICE_A = "a".repeat(32);
const DEVICE_B = "b".repeat(32);

// ── Round trip ───────────────────────────────────────────────────────────────

describe("multi-device round trip (docs/33 Phase 4 → Verification #1)", () => {
  it("device B unlocks with the same passphrase and reads what device A encrypted", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    const b = makeDevice(server, DEVICE_B, "B · test");

    // Device A enrolls the account's FIRST vault (server positively empty).
    const enrolled = await enrollVaultSynced(PASSPHRASE, a.deps);
    expect(enrolled).toEqual({ ok: true, synced: true });
    expect(server.rows).toHaveLength(1);
    expect(a.local.size).toBe(1);

    // A encrypts something with the account DEK.
    const cipher = await encryptField("Grocery Store", getDEK());
    lockVault();

    // Device B — fresh profile, empty local store, no row of its own.
    const unlockedB = await unlockVaultSynced(PASSPHRASE, b.deps);
    expect(unlockedB).toEqual({ ok: true, source: "enrolled" });

    // Same key hierarchy: B decrypts A's ciphertext, and B now has its OWN row.
    expect(await decryptField(cipher, getDEK())).toBe("Grocery Store");
    expect(server.rows).toHaveLength(2);
    expect(server.rows.map((r) => r.device_id).sort()).toEqual([DEVICE_A, DEVICE_B]);
    expect(b.local.size).toBe(1);

    // And B's blob is a re-wrap for B under its own salt, not a copy of A's.
    const rowA = server.rows.find((r) => r.device_id === DEVICE_A)!;
    const rowB = server.rows.find((r) => r.device_id === DEVICE_B)!;
    expect(rowB.wrapped_blob).not.toBe(rowA.wrapped_blob);
  });

  it("unlock-from-bundle keeps the index key usable (audit finding F1)", async () => {
    // Before F1 was fixed, `unwrap()` imported the session keys
    // non-extractable, so after any lock→unlock cycle `indexToken()` threw
    // `Index key is not exportable.` and exact payee search silently died.
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    lockVault();

    const result = await unlockVaultSynced(PASSPHRASE, a.deps);
    expect(result).toEqual({ ok: true, source: "own" });
    await expect(indexToken("grocery store", "payee-v1", getDEK())).resolves.toMatch(
      /^[0-9a-f]{64}$/,
    );
    lockVault();
  });

  it("wrong passphrase is wrong_passphrase and stays locked on both paths", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    const b = makeDevice(server, DEVICE_B, "B · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    lockVault();

    expect(await unlockVaultSynced(OTHER_PASSPHRASE, a.deps)).toEqual({
      ok: false,
      reason: "wrong_passphrase",
    });
    expect(await unlockVaultSynced(OTHER_PASSPHRASE, b.deps)).toEqual({
      ok: false,
      reason: "wrong_passphrase",
    });
    // Enrollment from a donor envelope fails the same way.
    expect(server.rows).toHaveLength(1);
    lockVault();
  });
});

// ── Revocation ───────────────────────────────────────────────────────────────

describe("revocation (docs/33 revoke runbook)", () => {
  it("A revokes B: B's next unlock fails device_revoked and B drops its mirror", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    const b = makeDevice(server, DEVICE_B, "B · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    await unlockVaultSynced(PASSPHRASE, b.deps); // enrolls B
    expect(server.rows).toHaveLength(2);
    expect(b.local.size).toBe(1);
    lockVault();

    const rowB = server.rows.find((r) => r.device_id === DEVICE_B)!;
    await revokeEnvelope(rowB.id, a.deps);

    // Blob is gone from the server, not merely flagged.
    expect(server.rows.find((r) => r.id === rowB.id)!.wrapped_blob).toBeNull();

    expect(await unlockVaultSynced(PASSPHRASE, b.deps)).toEqual({
      ok: false,
      reason: "device_revoked",
    });
    expect(b.local.size).toBe(0); // the disk copy is dropped, not just ignored

    // The surviving device is untouched.
    expect(await unlockVaultSynced(PASSPHRASE, a.deps)).toEqual({
      ok: true,
      source: "own",
    });
    lockVault();
  });

  it("a revoked device that enrolls a NEW identity behaves like any new device", async () => {
    // This is the escape hatch docs/33 accepts: revoke cuts an identity, not
    // the passphrase. A revoked profile that clears site data gets a new id.
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    const b = makeDevice(server, DEVICE_B, "B · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    await unlockVaultSynced(PASSPHRASE, b.deps);
    const rowB = server.rows.find((r) => r.device_id === DEVICE_B)!;
    await revokeEnvelope(rowB.id, a.deps);
    lockVault();

    // Reusing the revoked id is cut off…
    expect(await unlockVaultSynced(PASSPHRASE, b.deps)).toEqual({
      ok: false,
      reason: "device_revoked",
    });
    // …but a fresh id (cleared site data) enrolls through the donor path.
    const reissued = makeDevice(server, "c".repeat(32), "B · reissued");
    expect(await unlockVaultSynced(PASSPHRASE, reissued.deps)).toEqual({
      ok: true,
      source: "enrolled",
    });
    lockVault();
  });
});

// ── Fail-closed unlock ───────────────────────────────────────────────────────

describe("unlock is fail-closed (docs/33 Phase 4 threat model)", () => {
  it("sync_unreachable — a reachable local mirror is NOT enough to unlock", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    lockVault();

    server.failList = true;
    expect(await unlockVaultSynced(PASSPHRASE, a.deps)).toEqual({
      ok: false,
      reason: "sync_unreachable",
    });
    // Fail-closed: the local copy was not consulted, so nothing unlocked.
    expect(a.local.size).toBe(1);
  });

  it("no_envelope when neither the server nor this browser has a vault", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const b = makeDevice(server, DEVICE_B, "B · test");
    expect(await unlockVaultSynced(PASSPHRASE, b.deps)).toEqual({
      ok: false,
      reason: "no_envelope",
    });
  });

  it("a garbage blob from the server fails closed, never half-parses", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    lockVault();
    server.rows[0].wrapped_blob = "not-a-bundle";

    expect(await unlockVaultSynced(PASSPHRASE, a.deps)).toEqual({
      ok: false,
      reason: "wrong_passphrase",
    });
  });
});

// ── First-time enrollment guards ─────────────────────────────────────────────

describe("enroll only mints a DEK when the server is positively empty", () => {
  it("refuses to enroll when the account already has a vault", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    lockVault();

    const b = makeDevice(server, DEVICE_B, "B · test");
    const result = await enrollVaultSynced(PASSPHRASE, b.deps);
    expect(result).toMatchObject({ ok: false, reason: "already_enrolled_elsewhere" });
    // No second key hierarchy was created.
    expect(server.rows).toHaveLength(1);
    expect(b.local.size).toBe(0);
    expect(await unlockVaultSynced(PASSPHRASE, b.deps)).toMatchObject({
      ok: true,
      source: "enrolled",
    });
    lockVault();
  });

  it("sync_unreachable — a failed GET never mints a new DEK", async () => {
    lockVault();
    const server = new FakeVaultServer();
    server.failList = true;
    const a = makeDevice(server, DEVICE_A, "A · test");
    const result = await enrollVaultSynced(PASSPHRASE, a.deps);
    expect(result).toMatchObject({ ok: false, reason: "sync_unreachable" });
    expect(a.local.size).toBe(0);
    expect(server.rows).toHaveLength(0);
  });

  it("device_revoked — a cut-off device cannot mint a replacement vault", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    await revokeEnvelope(server.rows[0].id, a.deps);
    lockVault();

    const result = await enrollVaultSynced(PASSPHRASE, a.deps);
    expect(result).toMatchObject({ ok: false, reason: "device_revoked" });
    expect(a.local.size).toBe(1); // untouched mirror
  });

  it("demo tenant: enrollment stays local-only and says so", async () => {
    lockVault();
    const server = new FakeVaultServer();
    server.failCreateCode = "demo_account_restricted";
    const a = makeDevice(server, DEVICE_A, "demo");
    const result = await enrollVaultSynced(PASSPHRASE, a.deps);
    expect(result).toEqual({
      ok: true,
      synced: false,
      code: "demo_account_restricted",
    });
    expect(a.local.size).toBe(1);
    // Local-only vault still unlocks through the local fallback.
    lockVault();
    expect(await unlockVaultSynced(PASSPHRASE, a.deps)).toEqual({
      ok: true,
      source: "local",
    });
    lockVault();
  });

  it("any other POST failure rolls the session back to locked", async () => {
    lockVault();
    const server = new FakeVaultServer();
    server.failCreateCode = "internal_error";
    const a = makeDevice(server, DEVICE_A, "A · test");
    const result = await enrollVaultSynced(PASSPHRASE, a.deps);
    expect(result).toMatchObject({ ok: false, reason: "sync_failed" });
    expect(a.local.size).toBe(0);
    expect(server.rows).toHaveLength(0);
  });
});

// ── Passphrase rotation ──────────────────────────────────────────────────────

describe("rotatePassphrase re-wraps every device and rewrites no data", () => {
  it("every device re-unlocks with the NEW passphrase and the old one stops working", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    const b = makeDevice(server, DEVICE_B, "B · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    await unlockVaultSynced(PASSPHRASE, b.deps); // enrolls B
    const cipher = await encryptField("Rotation survives", getDEK());
    lockVault();

    // A comes back and rotates.
    expect(await unlockVaultSynced(PASSPHRASE, a.deps)).toEqual({
      ok: true,
      source: "own",
    });
    const rotated = await rotatePassphrase(OTHER_PASSPHRASE, a.deps);
    expect(rotated).toEqual({ ok: true, replaced: 2, active: 2 });
    lockVault();

    // Old passphrase is dead on both devices…
    expect(await unlockVaultSynced(PASSPHRASE, a.deps)).toEqual({
      ok: false,
      reason: "wrong_passphrase",
    });
    expect(await unlockVaultSynced(PASSPHRASE, b.deps)).toEqual({
      ok: false,
      reason: "wrong_passphrase",
    });
    // …the new one works, and the DEK never changed: ciphertext still opens.
    expect(await unlockVaultSynced(OTHER_PASSPHRASE, a.deps)).toEqual({
      ok: true,
      source: "own",
    });
    expect(await decryptField(cipher, getDEK())).toBe("Rotation survives");
    lockVault();
    expect(await unlockVaultSynced(OTHER_PASSPHRASE, b.deps)).toEqual({
      ok: true,
      source: "own",
    });
    lockVault();
  });

  it("fails closed when sync is unreachable or the vault is locked", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    lockVault();

    expect(await rotatePassphrase(OTHER_PASSPHRASE, a.deps)).toMatchObject({
      ok: false,
      reason: "vault_locked",
    });

    await unlockVaultSynced(PASSPHRASE, a.deps);
    server.failList = true;
    expect(await rotatePassphrase(OTHER_PASSPHRASE, a.deps)).toMatchObject({
      ok: false,
      reason: "sync_unreachable",
    });
    lockVault();
  });

  it("a partial failure reports how many devices are still on the old passphrase", async () => {
    lockVault();
    const server = new FakeVaultServer();
    const a = makeDevice(server, DEVICE_A, "A · test");
    const b = makeDevice(server, DEVICE_B, "B · test");
    await enrollVaultSynced(PASSPHRASE, a.deps);
    await unlockVaultSynced(PASSPHRASE, b.deps);
    lockVault();
    await unlockVaultSynced(PASSPHRASE, a.deps);

    // B's row becomes unwritable (revoked underneath us mid-run) while still
    // listed as active: this device re-wraps its own envelope first, then
    // reports how many devices are still on the OLD passphrase.
    const rowB = server.rows.find((r) => r.device_id === DEVICE_B)!;
    server.failReplaceIds.add(rowB.id);
    const result = await rotatePassphrase(OTHER_PASSPHRASE, a.deps);
    expect(result).toEqual({
      ok: false,
      replaced: 1,
      active: 2,
      reason: "sync_failed",
      message: expect.any(String),
    });
    // A's own row (and mirror) moved; B's blob is untouched — recoverable by
    // re-running rotation once the failure clears (docs/33 runbook).
    expect(server.rows.find((r) => r.device_id === DEVICE_A)!.wrapped_blob).not.toBe(
      rowB.wrapped_blob,
    );
    lockVault();
    expect(await unlockVaultSynced(OTHER_PASSPHRASE, a.deps)).toEqual({
      ok: true,
      source: "own",
    });
    lockVault();
  });
});

// ── Bundle encoding + device identity ────────────────────────────────────────

describe("bundle encoding and device identity", () => {
  it("round-trips a wrapped vault through base64url", async () => {
    lockVault();
    const bundle = await createVault(PASSPHRASE);
    const decoded = decodeBundle(encodeBundle(bundle));
    expect(decoded.createdAt).toBe(bundle.createdAt);
    expect(new Uint8Array(decoded.wrappedDEK)).toEqual(
      new Uint8Array(bundle.wrappedDEK),
    );
    expect(new Uint8Array(decoded.wrappedIndexKey)).toEqual(
      new Uint8Array(bundle.wrappedIndexKey),
    );
    expect(Array.from(decoded.salt)).toEqual(Array.from(bundle.salt));
    lockVault();
  });

  it("rejects anything that is not exactly our bundle shape", () => {
    expect(() => decodeBundle("bm90LWEtYnVuZGxl")).toThrow("Not a wrapped vault bundle.");
    expect(() => decodeBundle("")).toThrow("Not a wrapped vault bundle.");
    expect(() =>
      decodeBundle(
        btoa(JSON.stringify({ v: 9, salt: "x", wrappedDEK: "y", wrappedIndexKey: "z" })),
      ),
    ).toThrow("Not a wrapped vault bundle.");
  });

  it("derives a stable 32-hex device id when localStorage exists", () => {
    if (typeof localStorage === "undefined") return; // node environment
    const first = getDeviceId();
    expect(first).toMatch(/^[0-9a-f]{32}$/);
    expect(getDeviceId()).toBe(first);
    expect(deviceLabel().length).toBeLessThanOrEqual(64);
  });
});
