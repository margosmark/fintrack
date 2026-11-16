// Key-order-insensitive deep diff for the M48 parity harness (test infra only).
//
// Rules, deliberately strict — money is Decimal-as-string on both sides, so a
// parity miss must be reported, never rounded away:
//   - object key order is irrelevant; a missing/extra key is a diff
//   - arrays compare positionally (server sort order is part of the contract)
//   - a type change (number vs string, null vs object) is its own diff
//   - numbers compare with `===`: 100 !== "100", 0.1+0.2 style drift is real

import type { Json } from "./types.ts";

export type DiffKind = "type" | "value" | "missing" | "extra" | "length";

export interface Diff {
  /** JSON path of the first divergence, e.g. `$.items[3].amount_native`. */
  path: string;
  kind: DiffKind;
  expected: string;
  actual: string;
}

const DEFAULT_LIMIT = 200;

function kindOf(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function show(value: unknown): string {
  if (value === undefined) return "<missing>";
  let text: string;
  try {
    text = JSON.stringify(value) ?? String(value);
  } catch {
    text = String(value);
  }
  return text.length > 160 ? `${text.slice(0, 157)}...` : text;
}

/**
 * Collect every divergence between `expected` and `actual`.
 * Returns `[]` only when the two are structurally identical.
 */
export function deepDiff(
  expected: unknown,
  actual: unknown,
  path = "$",
  out: Diff[] = [],
  limit = DEFAULT_LIMIT,
): Diff[] {
  if (out.length >= limit) return out;
  if (Object.is(expected, actual)) return out;

  const expectedKind = kindOf(expected);
  const actualKind = kindOf(actual);
  if (expectedKind !== actualKind) {
    out.push({
      path,
      kind: "type",
      expected: `${expectedKind}: ${show(expected)}`,
      actual: `${actualKind}: ${show(actual)}`,
    });
    return out;
  }

  if (expectedKind === "array") {
    const e = expected as unknown[];
    const a = actual as unknown[];
    if (e.length !== a.length) {
      out.push({ path, kind: "length", expected: String(e.length), actual: String(a.length) });
    }
    const shared = Math.min(e.length, a.length);
    for (let i = 0; i < shared && out.length < limit; i += 1) {
      deepDiff(e[i], a[i], `${path}[${i}]`, out, limit);
    }
    return out;
  }

  if (expectedKind === "object") {
    const e = expected as Record<string, unknown>;
    const a = actual as Record<string, unknown>;
    const keys = [...new Set([...Object.keys(e), ...Object.keys(a)])].sort();
    for (const key of keys) {
      if (out.length >= limit) break;
      const keyPath = `${path}.${key}`;
      if (!(key in e)) {
        out.push({ path: keyPath, kind: "extra", expected: "<missing>", actual: show(a[key]) });
        continue;
      }
      if (!(key in a)) {
        out.push({ path: keyPath, kind: "missing", expected: show(e[key]), actual: "<missing>" });
        continue;
      }
      deepDiff(e[key], a[key], keyPath, out, limit);
    }
    return out;
  }

  out.push({ path, kind: "value", expected: show(expected), actual: show(actual) });
  return out;
}

/** Convenience: diff two API bodies typed as the fixture stores them. */
export function diffBodies(expected: Json, actual: Json): Diff[] {
  return deepDiff(expected, actual);
}
