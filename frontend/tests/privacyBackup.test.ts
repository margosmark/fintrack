// @vitest-environment node
import { describe, it, expect } from "vitest";
import {
  encryptSnapshot,
  decryptSnapshot,
  type StoredBackup,
} from "../src/lib/privacyBackup";

describe("privacyBackup AES-GCM round-trip", () => {
  it("encrypts then decrypts a snapshot under the passphrase", async () => {
    const snapshot = {
      accounts: [{ id: 1, name: "cash" }],
      total: 42,
      nested: { ok: true },
    };
    const record = await encryptSnapshot(snapshot, "s3cret");
    const restored = await decryptSnapshot(record, "s3cret");
    expect(restored).toEqual(snapshot);
  });

  it("produces a different ciphertext per random salt", async () => {
    const a = await encryptSnapshot({ x: 1 }, "pw");
    const b = await encryptSnapshot({ x: 1 }, "pw");
    const av = Array.from(new Uint8Array(a.ciphertext));
    const bv = Array.from(new Uint8Array(b.ciphertext));
    expect(av).not.toEqual(bv);
  });

  it("throws on a wrong passphrase (AES-GCM authentication failure)", async () => {
    const record = await encryptSnapshot({ secret: 1 }, "right");
    await expect(decryptSnapshot(record, "wrong")).rejects.toThrow(/Decryption failed/);
  });

  it("throws on an invalid record shape", async () => {
    await expect(
      decryptSnapshot({} as StoredBackup, "pw"),
    ).rejects.toThrow(/Invalid backup record/);
  });
});
