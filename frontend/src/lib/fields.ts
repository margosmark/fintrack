/**
 * Field-level crypto (docs/33 Phase 1): the v1 envelope for encrypted column
 * values plus HMAC blind-index tokens for exact-match lookup without
 * decryption.
 *
 * Envelope: ASCII `v1.<base64(iv)>.<base64(ciphertext)>`, AES-GCM-256 with a
 * fresh 96-bit IV per field. Plaintext is UTF-8 of the canonical value —
 * callers serialize (numbers as strings, per the money-invariant) before
 * calling; this module never touches floats.
 */

const ENVELOPE_VERSION = "v1";
const IV_BYTES = 12;

export function toBase64(bytes: Uint8Array | ArrayBuffer): string {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (let i = 0; i < u8.length; i++) binary += String.fromCharCode(u8[i]);
  if (typeof globalThis.btoa === "function") return globalThis.btoa(binary);
  // Node fallback (unit tests run in node per vitest.config.ts).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const buf = (globalThis as any).Buffer?.from(binary, "binary");
  if (buf === undefined) throw new Error("No base64 encoder available.");
  return buf.toString("base64");
}

export function fromBase64(text: string): Uint8Array {
  if (typeof globalThis.atob === "function") {
    const binary = globalThis.atob(text);
    const out = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
    return out;
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const buf = (globalThis as any).Buffer?.from(text, "base64");
  if (buf === undefined) throw new Error("No base64 decoder available.");
  return new Uint8Array(buf);
}

/** True for values already in v1 envelope form (safe to store as-is). */
export function isEncrypted(value: unknown): value is string {
  return typeof value === "string" && value.startsWith(`${ENVELOPE_VERSION}.`);
}

/** Encrypts one canonical plaintext string under the DEK. */
export async function encryptField(plaintext: string, dek: CryptoKey): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    dek,
    new TextEncoder().encode(plaintext),
  );
  return `${ENVELOPE_VERSION}.${toBase64(iv)}.${toBase64(ciphertext)}`;
}

/**
 * Decrypts a v1 envelope under the DEK. Throws on anything else —
 * legacy `v0`/plaintext must be handled by the caller's migration path
 * (docs/33), never silently accepted here.
 */
export async function decryptField(envelope: string, dek: CryptoKey): Promise<string> {
  const parts = envelope.split(".");
  if (parts.length !== 3 || parts[0] !== ENVELOPE_VERSION) {
    throw new Error("Not a v1 encrypted field.");
  }
  let iv: Uint8Array;
  let ciphertext: Uint8Array;
  try {
    iv = fromBase64(parts[1]);
    ciphertext = fromBase64(parts[2]);
  } catch {
    throw new Error("Not a v1 encrypted field.");
  }
  if (iv.length !== IV_BYTES) throw new Error("Not a v1 encrypted field.");
  try {
    const plaintext = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv },
      dek,
      ciphertext,
    );
    return new TextDecoder().decode(plaintext);
  } catch {
    throw new Error("Field decryption failed — wrong key or tampered data.");
  }
}

/**
 * Blind index token for exact-match lookup without decryption:
 * `HMAC-SHA256(indexKey, purpose ‖ "\0" ‖ normalizedValue)`, hex-encoded.
 * The purpose string domain-separates tokens (a payee token never equals a
 * symbol token), so token columns for different fields are unlinkable.
 */
export async function indexToken(
  normalizedValue: string,
  purpose: string,
  indexKey: CryptoKey,
): Promise<string> {
  // Index keys are generated extractable in the vault; re-import the raw
  // bytes with HMAC usage for signing.
  const raw = await exportRaw(indexKey);
  const hmacKey = await crypto.subtle.importKey(
    "raw",
    raw,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    hmacKey,
    new TextEncoder().encode(`${purpose}\0${normalizedValue}`),
  );
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function exportRaw(key: CryptoKey): Promise<ArrayBuffer> {
  try {
    return (await crypto.subtle.exportKey("raw", key)) as ArrayBuffer;
  } catch {
    throw new Error("Index key is not exportable.");
  }
}
