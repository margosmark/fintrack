/**
 * E2EE display + backfill helpers (docs/33 Phase 2). Field crypto lives in
 * ./fields, keys in ./vault; this module binds them to API shapes:
 * - whitespace-collapsed payee normalization (mirrors backend
 *   importers/base.normalized_payee so blind-index tokens agree),
 * - per-row encrypt (backfill + new writes) and mixed-mode display resolve
 *   (ciphertext when unlocked, lock placeholder when locked, plaintext legacy).
 */

import { encryptField, decryptField, indexToken } from "./fields";
import { getDEK, getIndexKey, isUnlocked } from "./vault";

/** Blind-index purpose for exact payee match (docs/33). */
export const PAYEE_TOKEN_PURPOSE = "payee-v1";

/**
 * Blind-index token for `GET /search`, computed ONLY while the vault is
 * unlocked (docs/33 S1 / Phase 4 audit row A1): while locked there is no key
 * to derive from, so the client must send no token at all and the server
 * answers `transactions: []`. Never throws at the call site — a key that
 * cannot sign degrades to "no token", never to a request that reveals the
 * query as a match.
 */
export async function blindIndexTokenForQuery(query: string): Promise<string | undefined> {
  if (!isUnlocked()) return undefined;
  try {
    return await indexToken(normalizePayee(query), PAYEE_TOKEN_PURPOSE, getIndexKey());
  } catch {
    return undefined;
  }
}

/** Collapse whitespace — must stay identical to backend normalization. */
export function normalizePayee(value: string): string {
  return value.split(/\s+/).join(" ").trim();
}

export interface TransactionCipherBundle {
  payee_cipher: string;
  raw_desc_cipher: string;
  amount_cipher: string;
  cad_amount_cipher: string | null;
  payee_token: string;
}

/**
 * The payee half of a row's bundle — ciphertext + blind index for ONE payee.
 *
 * Split out (M125) because a BULK rename writes the same payee to every row
 * in the matched set: the two payee fields depend only on that string, so one
 * computation serves the whole run, while `amount_cipher` / `raw_desc_cipher`
 * are per-row and stay untouched. `encryptTransaction` builds on this, so the
 * two can never disagree about how a payee is encrypted.
 */
export async function encryptPayee(
  normalizedPayee: string,
  dek = getDEK(),
  indexKey = getIndexKey(),
): Promise<{ payee_cipher: string; payee_token: string }> {
  const normalized = normalizePayee(normalizedPayee);
  const [payee_cipher, payee_token] = await Promise.all([
    encryptField(normalized, dek),
    indexToken(normalized, PAYEE_TOKEN_PURPOSE, indexKey),
  ]);
  return { payee_cipher, payee_token };
}

/** Encrypts one transaction's sensitive fields; amounts stay canonical strings. */
export async function encryptTransaction(
  args: {
    normalizedPayee: string;
    rawDescription: string;
    amountNative: string;
    cadAmount: string | null;
  },
  dek = getDEK(),
  indexKey = getIndexKey(),
): Promise<TransactionCipherBundle> {
  const [payee, raw_desc_cipher, amount_cipher, cad_amount_cipher] = await Promise.all([
    encryptPayee(args.normalizedPayee, dek, indexKey),
    encryptField(args.rawDescription, dek),
    encryptField(args.amountNative, dek),
    args.cadAmount === null ? Promise.resolve(null) : encryptField(args.cadAmount, dek),
  ]);
  return {
    payee_cipher: payee.payee_cipher,
    payee_token: payee.payee_token,
    raw_desc_cipher,
    amount_cipher,
    cad_amount_cipher,
  };
}

export type LockedMarker = "🔒";

export interface ResolvedPayee {
  payee: string;
  locked: boolean;
}

/**
 * Mixed-mode payee display: ciphertext wins when present (decrypted when
 * unlocked, lock marker when locked), legacy plaintext otherwise.
 */
export async function resolvePayee(
  tx: { normalized_payee: string; payee_cipher?: string | null },
  dek: CryptoKey | null = isUnlocked() ? getDEK() : null,
): Promise<ResolvedPayee> {
  if (tx.payee_cipher != null) {
    if (dek === null) return { payee: "🔒", locked: true };
    return { payee: await decryptField(tx.payee_cipher, dek), locked: false };
  }
  return { payee: tx.normalized_payee, locked: false };
}

export interface ResolvedAmount {
  amount: string;
  locked: boolean;
}

/** Mixed-mode amount display, same rules as resolvePayee. */
export async function resolveAmount(
  // `amount_native` is nullable because the partner's payload nulls it under
  // the OWNER's privacy mode (docs/43 §9, M90 D4/A) — there is no plaintext
  // to show, so the display is the same 🔒 the locked cipher shape uses.
  tx: { amount_native: string | null; amount_cipher?: string | null },
  dek: CryptoKey | null = isUnlocked() ? getDEK() : null,
): Promise<ResolvedAmount> {
  if (tx.amount_cipher != null) {
    if (dek === null) return { amount: "🔒", locked: true };
    return { amount: await decryptField(tx.amount_cipher, dek), locked: false };
  }
  if (tx.amount_native == null) return { amount: "🔒", locked: true };
  return { amount: tx.amount_native, locked: false };
}

/** Mixed-mode raw-description display, same rules as resolvePayee. */
export async function resolveRawDescription(
  tx: { raw_description: string; raw_desc_cipher?: string | null },
  dek: CryptoKey | null = isUnlocked() ? getDEK() : null,
): Promise<ResolvedPayee> {
  if (tx.raw_desc_cipher != null) {
    if (dek === null) return { payee: "🔒", locked: true };
    return { payee: await decryptField(tx.raw_desc_cipher, dek), locked: false };
  }
  return { payee: tx.raw_description, locked: false };
}

/** Mixed-mode account-name display. */
export async function resolveAccountName(
  account: { name: string; name_cipher?: string | null },
  dek: CryptoKey | null = isUnlocked() ? getDEK() : null,
): Promise<ResolvedPayee> {
  if (account.name_cipher != null) {
    if (dek === null) return { payee: "🔒", locked: true };
    return { payee: await decryptField(account.name_cipher, dek), locked: false };
  }
  return { payee: account.name, locked: false };
}

/** Encrypts an account name for create/update payloads. */
export async function encryptAccountName(
  name: string,
  dek = getDEK(),
): Promise<string> {
  return encryptField(name, dek);
}
