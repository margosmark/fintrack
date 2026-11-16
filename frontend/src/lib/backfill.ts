/**
 * Client-side backfill (docs/33 Phase 2 migration): the server can never
 * encrypt legacy rows (it has no key), so an unlocked browser fetches
 * plaintext legacy rows and writes ciphertext + blind-index tokens back.
 * Idempotent — rows already carrying ciphers are skipped. Fail-closed: throws
 * when the vault is locked (getDEK).
 */

import { encryptTransaction, encryptAccountName } from "./e2ee";
import { getDEK, getIndexKey } from "./vault";

export interface BackfillTxRow {
  id: number;
  normalized_payee: string;
  raw_description: string;
  amount_native: string;
  cad_amount: string | null;
  payee_cipher: string | null;
}

export interface BackfillAccountRow {
  id: number;
  name: string;
  name_cipher: string | null;
}

export interface BackfillDeps {
  listTransactions: (limit: number, offset: number) => Promise<{
    items: BackfillTxRow[];
    total: number;
  }>;
  updateTransaction: (id: number, patch: Record<string, string | null>) => Promise<unknown>;
  listAccounts: () => Promise<{ items: BackfillAccountRow[] }>;
  updateAccount: (id: number, patch: Record<string, string | null>) => Promise<unknown>;
  onProgress?: (done: number, total: number) => void;
}

export interface BackfillCounts {
  transactions: number;
  accounts: number;
  skipped: number;
}

const PAGE = 200;

/** Encrypts every legacy row missing ciphertext. Safe to re-run. */
export async function backfillE2EE(deps: BackfillDeps): Promise<BackfillCounts> {
  const dek = getDEK();
  const indexKey = getIndexKey();
  const counts: BackfillCounts = { transactions: 0, accounts: 0, skipped: 0 };

  let offset = 0;
  let total = Infinity;
  let done = 0;
  for (;;) {
    const page = await deps.listTransactions(PAGE, offset);
    total = page.total;
    if (page.items.length === 0) break;
    for (const row of page.items) {
      if (row.payee_cipher != null) {
        counts.skipped += 1;
      } else {
        const bundle = await encryptTransaction(
          {
            normalizedPayee: row.normalized_payee,
            rawDescription: row.raw_description,
            amountNative: row.amount_native,
            cadAmount: row.cad_amount,
          },
          dek,
          indexKey,
        );
        await deps.updateTransaction(row.id, { ...bundle });
        counts.transactions += 1;
      }
      done += 1;
      deps.onProgress?.(done, total);
    }
    offset += page.items.length;
    if (done >= total) break;
  }

  const accounts = await deps.listAccounts();
  for (const account of accounts.items) {
    if (account.name_cipher != null) {
      counts.skipped += 1;
      continue;
    }
    await deps.updateAccount(account.id, {
      name_cipher: await encryptAccountName(account.name, dek),
    });
    counts.accounts += 1;
  }
  return counts;
}
