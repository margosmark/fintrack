// M140 — the sweep that makes "no re-render exceptions" enforceable
// (docs/46 §D6).
//
// The M140 fix is mechanical and therefore only durable if something fails
// when the next money surface is added without a subscription. Two scripts in
// `scripts/` walk the AST of `frontend/src`:
//
//   sweep_money.mjs       every money-formatter CALL → the component that owns
//                         it, and whether that component calls useMasked().
//                         Also fails on money built inside useMemo/useCallback/
//                         useEffect (a re-render does not re-run those) and on
//                         currency formatters that bypass the mask-aware
//                         helpers entirely (the `fmtDelta` class).
//   hook_call_guard.mjs   a component that calls useMasked() is a hook
//                         consumer, so a plain `Name(...)` call of it anywhere
//                         is an "Invalid hook call" crash TypeScript will not
//                         catch.
//
// They run as tests so `vitest run` — which every gate already runs — fails on
// a regression, rather than depending on someone remembering a script.
import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// vitest's cwd is frontend/; the scripts live one level up.
const SCRIPTS = join(process.cwd(), "..", "scripts");

function run(script: string, ...args: string[]): { code: number; out: string } {
  try {
    const out = execFileSync("node", [join(SCRIPTS, script), ...args], { encoding: "utf8" });
    return { code: 0, out };
  } catch (err) {
    const e = err as { status?: number; stdout?: string; stderr?: string };
    return { code: e.status ?? 1, out: `${e.stdout ?? ""}${e.stderr ?? ""}` };
  }
}

describe("M140 sweep — every money-rendering component subscribes", () => {
  it("finds a substantial surface to audit (the script is not silently scanning nothing)", () => {
    const { code, out } = run("sweep_money.mjs", "--json");
    expect(code).toBe(0);
    const report = JSON.parse(out) as {
      rows: Array<{ file: string; component: string; subscribes: boolean; calls: string[] }>;
    };
    // A vacuous pass would be worse than no test: if the walker broke, the
    // sweep would report zero components and "pass". Pin a floor well under
    // the real count but far above zero.
    expect(report.rows.length).toBeGreaterThanOrEqual(50);
    expect(report.rows.every((r) => r.calls.length > 0)).toBe(true);
    expect(report.rows.every((r) => r.subscribes)).toBe(true);
  });

  it("reports no memoized money, no module-scope money, no raw currency builders", () => {
    const { code, out } = run("sweep_money.mjs", "--check");
    expect(code, out).toBe(0);
    expect(out).toContain("M140 sweep OK");
  });

  it("the check FAILS when a money component stops subscribing (canary)", () => {
    // Prove the guard has teeth: add a component that renders money through a
    // mask-aware helper without subscribing, and require the sweep to object.
    // Deliberately NOT src/pages: tests/routeCodeSplit.test.tsx enumerates
    // that directory and vitest runs test files in parallel, so a canary
    // parked there would fail an unrelated test on a race.
    const canaryDir = join(process.cwd(), "src", "__m140_canary__");
    const canary = join(canaryDir, "canary.tsx");
    mkdirSync(canaryDir, { recursive: true });
    writeFileSync(
      canary,
      [
        'import { formatCad } from "../lib/format";',
        "",
        "export function M140Canary() {",
        '  return <span>{formatCad("100.00")}</span>;',
        "}",
        "",
      ].join("\n"),
      "utf8",
    );
    try {
      const { code, out } = run("sweep_money.mjs", "--check");
      expect(code).toBe(1);
      expect(out).toContain("M140Canary");
    } finally {
      rmSync(canaryDir, { recursive: true, force: true });
    }
  });
});

describe("M140 hook guard — a subscribed component is only ever rendered as JSX", () => {
  it("passes, and the walker sees the subscribed set", () => {
    const { code, out } = run("hook_call_guard.mjs", "--check");
    expect(code, out).toBe(0);
    // Same vacuous-pass guard: the codemod subscribed 60+ components.
    const match = out.match(/(\d+) subscribed components/);
    expect(match).not.toBeNull();
    expect(Number(match?.[1] ?? 0)).toBeGreaterThanOrEqual(50);
  });
});
