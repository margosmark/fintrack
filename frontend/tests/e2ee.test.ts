// @vitest-environment node
import { describe, it, expect, beforeEach } from "vitest";
import { createVault, lockVault, getDEK, getIndexKey } from "../src/lib/vault";
import {
  normalizePayee,
  encryptTransaction,
  resolvePayee,
  resolveAmount,
  resolveAccountName,
  encryptAccountName,
  PAYEE_TOKEN_PURPOSE,
} from "../src/lib/e2ee";
import { decryptField, indexToken } from "../src/lib/fields";

beforeEach(async () => {
  lockVault();
  await createVault("e2ee-test");
});

describe("payee normalization parity", () => {
  it("collapses whitespace like the backend", () => {
    expect(normalizePayee("  CAFE   LUNA\n")).toBe("CAFE LUNA");
    expect(normalizePayee("NETFLIX.COM")).toBe("NETFLIX.COM");
  });
});

describe("transaction bundle", () => {
  it("encrypts every field and tokens the normalized payee", async () => {
    const bundle = await encryptTransaction({
      normalizedPayee: "CAFE  LUNA",
      rawDescription: "CAFE LUNA #42",
      amountNative: "-12.50",
      cadAmount: "-12.50",
    });
    expect(await decryptField(bundle.payee_cipher, getDEK())).toBe("CAFE LUNA");
    expect(await decryptField(bundle.raw_desc_cipher, getDEK())).toBe("CAFE LUNA #42");
    expect(await decryptField(bundle.amount_cipher, getDEK())).toBe("-12.50");
    expect(await decryptField(bundle.cad_amount_cipher!, getDEK())).toBe("-12.50");
    expect(bundle.payee_token).toBe(
      await indexToken("CAFE LUNA", PAYEE_TOKEN_PURPOSE, getIndexKey()),
    );
  });

  it("null cad amount stays null", async () => {
    const bundle = await encryptTransaction({
      normalizedPayee: "X",
      rawDescription: "X",
      amountNative: "5.00",
      cadAmount: null,
    });
    expect(bundle.cad_amount_cipher).toBeNull();
  });
});

describe("mixed-mode display resolve", () => {
  it("prefers ciphertext when unlocked", async () => {
    const payee_cipher = await encryptFieldFor("Dining Out");
    expect(await resolvePayee({ normalized_payee: "LEGACY", payee_cipher })).toEqual({
      payee: "Dining Out",
      locked: false,
    });
  });

  it("shows the lock marker when ciphertext exists but vault is locked", async () => {
    const payee_cipher = await encryptFieldFor("Dining Out");
    lockVault();
    expect(
      await resolvePayee({ normalized_payee: "LEGACY", payee_cipher }, null),
    ).toEqual({ payee: "🔒", locked: true });
  });

  it("falls back to plaintext legacy rows", async () => {
    expect(await resolvePayee({ normalized_payee: "LEGACY" })).toEqual({
      payee: "LEGACY",
      locked: false,
    });
    expect(
      await resolveAmount({ amount_native: "-3.00" }),
    ).toEqual({ amount: "-3.00", locked: false });
    expect(await resolveAccountName({ name: "Cash" })).toEqual({
      payee: "Cash",
      locked: false,
    });
  });

  it("round-trips account names", async () => {
    const name_cipher = await encryptAccountName("Secret Savings");
    expect(await resolveAccountName({ name: "LEGACY", name_cipher })).toEqual({
      payee: "Secret Savings",
      locked: false,
    });
  });
});

async function encryptFieldFor(value: string): Promise<string> {
  const { encryptField } = await import("../src/lib/fields");
  return encryptField(value, getDEK());
}
