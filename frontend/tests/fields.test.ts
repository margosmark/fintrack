// @vitest-environment node
import { describe, it, expect, beforeEach } from "vitest";
import { createVault, lockVault, getDEK, getIndexKey } from "../src/lib/vault";
import {
  encryptField,
  decryptField,
  isEncrypted,
  indexToken,
} from "../src/lib/fields";

beforeEach(async () => {
  lockVault();
  await createVault("test-passphrase");
});

describe("v1 field envelope", () => {
  it("round-trips a value under the DEK", async () => {
    const env = await encryptField("42.50", getDEK());
    expect(isEncrypted(env)).toBe(true);
    expect(await decryptField(env, getDEK())).toBe("42.50");
  });

  it("fresh IV per field: same plaintext, different envelopes", async () => {
    const a = await encryptField("same", getDEK());
    const b = await encryptField("same", getDEK());
    expect(a).not.toBe(b);
  });

  it("rejects plaintext and malformed envelopes (no silent v0)", async () => {
    await expect(decryptField("plain text", getDEK())).rejects.toThrow(
      "Not a v1 encrypted field.",
    );
    await expect(decryptField("v1.only-two", getDEK())).rejects.toThrow(
      "Not a v1 encrypted field.",
    );
    await expect(decryptField("v1.!!!.???", getDEK())).rejects.toThrow(
      "Not a v1 encrypted field.",
    );
  });

  it("fails closed on tampered ciphertext", async () => {
    const env = await encryptField("secret", getDEK());
    const parts = env.split(".");
    const tampered = `${parts[0]}.${parts[1]}.${parts[2].slice(0, -4)}AAAA`;
    await expect(decryptField(tampered, getDEK())).rejects.toThrow(
      "Field decryption failed",
    );
  });

  it("isEncrypted is false for plaintext", () => {
    expect(isEncrypted("NETFLIX")).toBe(false);
    expect(isEncrypted(42)).toBe(false);
  });
});

describe("blind index tokens", () => {
  it("deterministic per (value, purpose), distinct across purposes", async () => {
    const a = await indexToken("netflix", "payee", getIndexKey());
    const b = await indexToken("netflix", "payee", getIndexKey());
    const c = await indexToken("netflix", "symbol", getIndexKey());
    expect(a).toBe(b);
    expect(a).not.toBe(c);
    expect(a).toMatch(/^[0-9a-f]{64}$/);
  });

  it("different values give different tokens", async () => {
    const a = await indexToken("aaa", "payee", getIndexKey());
    const b = await indexToken("bbb", "payee", getIndexKey());
    expect(a).not.toBe(b);
  });
});
