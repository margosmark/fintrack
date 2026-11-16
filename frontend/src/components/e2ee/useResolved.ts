/**
 * Mixed-mode display hooks (docs/33 Phase 2): resolve rows to displayable
 * {payee, raw, amount, locked} using the session vault when rows carry
 * ciphertext, falling back to legacy plaintext. Re-resolves on vault
 * lock/unlock via the `vaultchange` window event. Decrypted values are cached
 * per session (module-level) so list re-renders don't re-decrypt.
 */

import { useEffect, useState } from "react";
import type { Transaction } from "../../api/transactions";
import type { Account } from "../../api/accounts";
import {
  resolveAccountName,
  resolveAmount,
  resolvePayee,
  resolveRawDescription,
} from "../../lib/e2ee";
import { getDEK, isUnlocked } from "../../lib/vault";

export interface ResolvedTx {
  payee: string;
  raw: string;
  amount: string;
  locked: boolean;
}

const txCache = new Map<string, string>();

/**
 * djb2 over the row's ciphertext (docs/29 F9): folded into the cache key so
 * a re-encrypted row (vault rotation / backfill) changes the key and
 * resolves fresh instead of serving the stale plaintext. Key includes the
 * row id plus a 32-bit cipher digest, so a same-value collision across
 * rows/session sizes is practically excluded.
 */
function cipherHash(cipher: string): string {
  let h = 5381;
  for (let i = 0; i < cipher.length; i++) {
    h = ((h << 5) + h + cipher.charCodeAt(i)) | 0;
  }
  return (h >>> 0).toString(36);
}

async function cached(key: string, run: () => Promise<string>): Promise<string> {
  const hit = txCache.get(key);
  if (hit !== undefined) return hit;
  const value = await run();
  txCache.set(key, value);
  return value;
}

function dekOrNull(): CryptoKey | null {
  return isUnlocked() ? getDEK() : null;
}

/** For tests: drop cached decryptions. */
export function clearResolvedCache(): void {
  txCache.clear();
}

export function useResolvedTransactions(items: Transaction[]): Map<number, ResolvedTx> {
  const [tick, setTick] = useState(0);
  const [resolved, setResolved] = useState<Map<number, ResolvedTx>>(new Map());

  useEffect(() => {
    const onVault = () => {
      txCache.clear();
      setTick((t) => t + 1);
    };
    window.addEventListener("vaultchange", onVault);
    return () => window.removeEventListener("vaultchange", onVault);
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function run(): Promise<void> {
      const dek = dekOrNull();
      const next = new Map<number, ResolvedTx>();
      await Promise.all(
        items.map(async (tx) => {
          const [payee, raw, amount] = await Promise.all([
            tx.payee_cipher != null && dek !== null
              ? cached(`p${tx.id}:${cipherHash(tx.payee_cipher)}`, () =>
                  resolvePayee(tx, dek).then((r) => r.payee),
                )
              : resolvePayee(tx, dek).then((r) => r.payee),
            tx.raw_desc_cipher != null && dek !== null
              ? cached(`r${tx.id}:${cipherHash(tx.raw_desc_cipher)}`, () =>
                  resolveRawDescription(tx, dek).then((r) => r.payee),
                )
              : resolveRawDescription(tx, dek).then((r) => r.payee),
            tx.amount_cipher != null && dek !== null
              ? cached(`a${tx.id}:${cipherHash(tx.amount_cipher)}`, () =>
                  resolveAmount(tx, dek).then((r) => r.amount),
                )
              : resolveAmount(tx, dek).then((r) => r.amount),
          ]);
          const locked =
            (tx.payee_cipher != null || tx.amount_cipher != null) && dek === null;
          next.set(tx.id, { payee, raw, amount, locked });
        }),
      );
      if (!cancelled) setResolved(next);
    }
    void run();
    return () => {
      cancelled = true;
    };
    // items identity changes per fetch; tick covers vault flips.
  }, [items, tick]);

  return resolved;
}

export function useResolvedAccounts(items: Account[]): Map<number, string> {
  const [tick, setTick] = useState(0);
  const [resolved, setResolved] = useState<Map<number, string>>(new Map());

  useEffect(() => {
    const onVault = () => {
      txCache.clear();
      setTick((t) => t + 1);
    };
    window.addEventListener("vaultchange", onVault);
    return () => window.removeEventListener("vaultchange", onVault);
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function run(): Promise<void> {
      const dek = dekOrNull();
      const next = new Map<number, string>();
      await Promise.all(
        items.map(async (account) => {
          const name =
            account.name_cipher != null && dek !== null
              ? await cached(`n${account.id}:${cipherHash(account.name_cipher)}`, () =>
                  resolveAccountName(account, dek).then((r) => r.payee),
                )
              : (await resolveAccountName(account, dek)).payee;
          next.set(account.id, name);
        }),
      );
      if (!cancelled) setResolved(next);
    }
    void run();
    return () => {
      cancelled = true;
    };
    // items identity changes per fetch; tick covers vault flips.
  }, [items, tick]);

  return resolved;
}
