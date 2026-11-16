// @vitest-environment node
import "fake-indexeddb/auto";
import { describe, it, expect, beforeEach } from "vitest";
import {
  createVault,
  unlockVault,
  lockVault,
  isUnlocked,
  getDEK,
  getIndexKey,
  storeVault,
  loadVault,
} from "../src/lib/vault";
import { encryptField, decryptField } from "../src/lib/fields";

beforeEach(() => lockVault());

describe("vault enrollment + unlock", () => {
  it("starts locked and rejects key access", () => {
    expect(isUnlocked()).toBe(false);
    expect(() => getDEK()).toThrow("Vault is locked.");
    expect(() => getIndexKey()).toThrow("Vault is locked.");
  });

  it("createVault leaves the session unlocked with working keys", async () => {
    await createVault("correct horse");
    expect(isUnlocked()).toBe(true);
    const roundTrip = await decryptField(
      await encryptField("NETFLIX.COM", getDEK()),
      getDEK(),
    );
    expect(roundTrip).toBe("NETFLIX.COM");
  });

  it("lockVault drops access until unlock with the same bundle", async () => {
    const bundle = await createVault("s3cret");
    lockVault();
    expect(isUnlocked()).toBe(false);
    await unlockVault("s3cret", bundle);
    expect(isUnlocked()).toBe(true);
  });

  it("wrong passphrase stays locked (GCM auth failure on unwrap)", async () => {
    const bundle = await createVault("right");
    lockVault();
    await expect(unlockVault("wrong", bundle)).rejects.toThrow();
    expect(isUnlocked()).toBe(false);
  });

  it("persists and reloads the opaque bundle through IndexedDB", async () => {
    const bundle = await createVault("persist-me");
    await storeVault("u1", bundle);
    lockVault();
    const loaded = await loadVault("u1");
    expect(loaded).not.toBeNull();
    await unlockVault("persist-me", loaded!);
    expect(isUnlocked()).toBe(true);
  });

  it("loadVault returns null when nothing is enrolled", async () => {
    expect(await loadVault("nobody")).toBeNull();
  });
});
