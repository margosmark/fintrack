// Client-side receipt re-encode (M108 P4, docs/09 §Receipt images).
//
// "webp-compressed on client before POST" with NO new dependency: the platform
// already ships everything this needs — `createImageBitmap` to decode, a 2D
// canvas to downscale, and `canvas.toBlob` to encode. The card asked for a
// frontend compression library "except if an existing tiny one exists"; none
// does (`package-lock.json` has no image codec), and pulling one in for three
// canvas calls would be the wrong trade on a bundle that is already one
// ~1.2 MB chunk.
//
// What the client guarantees before a byte leaves the browser:
//   * the image is at most {@link RECEIPT_MAX_BYTES} — the server's hard cap,
//     enforced here first so a rejection is a local, instant message instead
//     of a 2 MB round trip;
//   * its longest edge is at most {@link MAX_EDGE} — a 12 MP phone photo
//     shrinks to something a person can actually scroll to;
//   * the encoded type is one the server sniffs back to the same value
//     (`image/webp` / `image/jpeg` both are), so the magic-byte check and the
//     `Content-Type` header can never disagree.

import { RECEIPT_MAX_BYTES } from "../api/receipts";

/** Decode guard, not an upload rule — a receipt photo is never this big. */
const MAX_INPUT_BYTES = 10 * 1024 * 1024;

/** Longest edge after downscaling: enough for OCR-quality detail later. */
export const MAX_EDGE = 1600;

/** Tried in order; first one that fits under the cap wins. */
const ENCODE_PLAN: ReadonlyArray<{ type: string; quality: number; ext: string }> = [
  { type: "image/webp", quality: 0.85, ext: "webp" },
  { type: "image/webp", quality: 0.7, ext: "webp" },
  { type: "image/jpeg", quality: 0.85, ext: "jpg" },
  { type: "image/jpeg", quality: 0.7, ext: "jpg" },
];

export type ReceiptImageFailure =
  | "input_too_large"
  | "decode_failed"
  | "output_too_large";

export class ReceiptImageError extends Error {
  readonly reason: ReceiptImageFailure;

  constructor(reason: ReceiptImageFailure, message: string) {
    super(message);
    this.name = "ReceiptImageError";
    this.reason = reason;
  }
}

export interface EncodedReceipt {
  blob: Blob;
  contentType: string;
  filename: string;
}

/**
 * Scale `(width, height)` so the longest edge is `maxEdge`, never upscaling.
 *
 * Pure and exported so the sizing rule is testable without a canvas — jsdom
 * has neither `createImageBitmap` nor a real 2D context.
 */
export function fitWithin(
  width: number,
  height: number,
  maxEdge: number = MAX_EDGE,
): { width: number; height: number } {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return { width: 1, height: 1 };
  }
  const scale = Math.min(1, maxEdge / Math.max(width, height));
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

function toBlob(canvas: HTMLCanvasElement, type: string, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
}

/**
 * Decode → downscale → encode until the result fits under the upload cap.
 *
 * Throws {@link ReceiptImageError} with a message meant for a person: the
 * caller shows it verbatim, so it says what to DO, not what the API said.
 */
export async function encodeReceiptImage(file: File): Promise<EncodedReceipt> {
  if (file.size > MAX_INPUT_BYTES) {
    throw new ReceiptImageError(
      "input_too_large",
      "That image is too large to open here — 10 MB maximum. Try a photo, not a scan.",
    );
  }

  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new ReceiptImageError("decode_failed", "That file could not be opened as an image.");
  }

  try {
    const { width, height } = fitWithin(bitmap.width, bitmap.height);
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (context === null) {
      throw new ReceiptImageError("decode_failed", "This browser could not prepare the image.");
    }
    context.drawImage(bitmap, 0, 0, width, height);

    for (const plan of ENCODE_PLAN) {
      const blob = await toBlob(canvas, plan.type, plan.quality);
      // `toBlob` resolves `null` when the type is unsupported, and may hand
      // back a DIFFERENT image type than asked for — trust the blob, not the
      // request, since the server sniffs the bytes anyway.
      if (blob === null || blob.size === 0) continue;
      if (blob.size > RECEIPT_MAX_BYTES) continue;
      const type = blob.type.startsWith("image/") ? blob.type : plan.type;
      const ext = type === "image/webp" ? "webp" : type === "image/jpeg" ? "jpg" : plan.ext;
      return { blob, contentType: type, filename: `receipt.${ext}` };
    }

    throw new ReceiptImageError(
      "output_too_large",
      "Even at the smallest quality this image stays over 2 MB. Crop it or take a smaller photo.",
    );
  } finally {
    bitmap.close();
  }
}
