// Receipt images (M108 P4, docs/09 §Receipt images, docs/08).
//
// Three calls, one image per transaction:
//   PUT    /transactions/{id}/receipt   multipart `file` → metadata (replaces)
//   GET    /transactions/{id}/receipt   → the bytes, with their image type
//   DELETE /transactions/{id}/receipt   → idempotent 204
//
// The LIST never carries the image: `Transaction.has_receipt` is the boolean
// the row chip reads, so a page of 200 transactions moves two hundred flags
// and zero megabytes. No cloud, no email — the image stays on this server and
// only its owner's session can fetch it (the Monarch-scan privacy posture).

import { apiBinary } from "./client";

/** The server's hard cap; the client must never POST more than this. */
export const RECEIPT_MAX_BYTES = 2 * 1024 * 1024;

export interface TransactionReceipt {
  transaction_id: number;
  content_type: string;
  size_bytes: number;
}

/** Attach (or replace) the transaction's one receipt image. */
export async function uploadReceipt(
  transactionId: number,
  blob: Blob,
  filename: string,
): Promise<TransactionReceipt> {
  const form = new FormData();
  // No Content-Type here on purpose: the browser must add its own boundary.
  form.append("file", blob, filename);
  const response = await apiBinary(`/transactions/${transactionId}/receipt`, {
    method: "PUT",
    body: form,
  });
  return (await response.json()) as TransactionReceipt;
}

/** The image itself — a blob so it can be opened without an auth-carrying URL. */
export async function fetchReceiptBlob(transactionId: number): Promise<Blob> {
  const response = await apiBinary(`/transactions/${transactionId}/receipt`);
  return response.blob();
}

/** Remove it. Idempotent server-side: 204 whether or not one existed. */
export async function deleteReceipt(transactionId: number): Promise<void> {
  await apiBinary(`/transactions/${transactionId}/receipt`, { method: "DELETE" });
}
