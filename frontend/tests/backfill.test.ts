// @vitest-environment node
import { describe, it, expect, beforeEach, vi } from "vitest";
import { createVault, lockVault, getDEK } from "../src/lib/vault";
import { backfillE2EE } from "../src/lib/backfill";
import { decryptField } from "../src/lib/fields";

beforeEach(async () => {
  lockVault();
  await createVault("backfill-test");
});

function txRow(id: number, cipher: boolean = false) {
  return {
    id,
    normalized_payee: `PAYEE ${id}`,
    raw_description: `raw ${id}`,
    amount_native: `${id}.00`,
    cad_amount: `${id}.00`,
    payee_cipher: cipher ? "v1.present" : null,
  };
}

describe("backfillE2EE", () => {
  it("encrypts legacy rows and skips migrated ones", async () => {
    const updated: Record<number, Record<string, string | null>> = {};
    const counts = await backfillE2EE({
      listTransactions: async (_limit, offset) => ({
        items: offset === 0 ? [txRow(1), txRow(2, true)] : [],
        total: 2,
      }),
      updateTransaction: async (id, patch) => {
        updated[id] = patch;
      },
      listAccounts: async () => ({
        items: [
          { id: 7, name: "Cash", name_cipher: null },
          { id: 8, name: "Done", name_cipher: "v1.present" },
        ],
      }),
      updateAccount: async (id, patch) => {
        updated[1000 + id] = patch;
      },
    });
    expect(counts).toEqual({ transactions: 1, accounts: 1, skipped: 2 });
    expect(await decryptField(updated[1].payee_cipher!, getDEK())).toBe("PAYEE 1");
    expect(updated[1].payee_token).toMatch(/^[0-9a-f]{64}$/);
    expect(await decryptField(updated[1007].name_cipher!, getDEK())).toBe("Cash");
    expect(updated[2]).toBeUndefined();
  });

  it("fails closed when the vault is locked", async () => {
    lockVault();
    await expect(
      backfillE2EE({
        listTransactions: async () => ({ items: [], total: 0 }),
        updateTransaction: async () => {},
        listAccounts: async () => ({ items: [] }),
        updateAccount: async () => {},
      }),
    ).rejects.toThrow("Vault is locked.");
  });

  it("reports progress", async () => {
    const seen: Array<[number, number]> = [];
    await backfillE2EE({
      listTransactions: async (_limit, offset) => ({
        items: offset === 0 ? [txRow(1), txRow(2)] : [],
        total: 2,
      }),
      updateTransaction: async () => {},
      listAccounts: async () => ({ items: [] }),
      updateAccount: async () => {},
      onProgress: (done, total) => {
        seen.push([done, total]);
      },
    });
    expect(seen).toEqual([
      [1, 2],
      [2, 2],
    ]);
  });

  it("pages through more rows than one page", async () => {
    const spy = vi.fn(async (_limit: number, _offset: number) => ({
      items: [] as ReturnType<typeof txRow>[],
      total: 0,
    }));
    await backfillE2EE({
      listTransactions: spy,
      updateTransaction: async () => {},
      listAccounts: async () => ({ items: [] }),
      updateAccount: async () => {},
    });
    expect(spy).toHaveBeenCalledOnce();
  });
});
