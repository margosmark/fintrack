// M102 (docs/09 "Scroll regions"): every horizontal scroll box in `src/` must
// be keyboard-reachable — WCAG 2.1.1, axe rule `scrollable-region-focusable`
// (serious; 8 of 24 page×theme×width runs failed on /investments + /taxes at
// 390/412 before this card — 12 violating nodes).
//
// Why a SOURCE scan: axe only flags a box whose content overflows *right now*,
// which at 1440 never happens and at 390 depends on the seed data — a browser
// sweep covers today's pages, not tomorrow's. So this parses every `.ts`/`.tsx`
// under `src/` with the TypeScript parser and pins the contract instead:
//
//   * a JSX element whose `className` carries `overflow-x-auto` must itself be
//     focusable (`tabIndex` literal >= 0) AND carry an accessible name
//     (`aria-label` / `aria-labelledby`) — the `role="region"` + name shape
//     TableScroller has shipped since M64; or
//   * it is a `<TableScroller>`, which owns that shape internally.
//
// Every `overflow-x-auto` string literal must be attributable to such a
// className, so a class hidden behind a variable fails too — a new page cannot
// quietly reintroduce an unreachable scroll box.

import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Card } from "../src/components/ui/Card";
import ts from "typescript";

const SRC = "src";
const TOKEN = "overflow-x-auto";

/** Components that own the focusable scroll box themselves (docs/09 M64). */
const FOCUSABLE_WRAPPERS = new Set(["TableScroller"]);

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) walk(path, out);
    else if (/\.tsx?$/.test(entry.name)) out.push(path);
  }
  return out;
}

/** Static text chunks of a `className={…}` attribute (empty when dynamic-only). */
function classNameChunks(attr: ts.JsxAttribute): string[] {
  const init = attr.initializer;
  if (!init) return [];
  if (ts.isStringLiteral(init)) return [init.text];
  if (ts.isJsxExpression(init) && init.expression) {
    const expr = init.expression;
    if (ts.isStringLiteral(expr) || ts.isNoSubstitutionTemplateLiteral(expr)) return [expr.text];
    if (ts.isTemplateExpression(expr)) {
      return [expr.head.text, ...expr.templateSpans.map((s) => s.literal.text)];
    }
  }
  return [];
}

/** `tabIndex` must be a non-negative literal — a computed value cannot be verified here. */
function tabIndexIsFocusable(attr: ts.JsxAttribute): boolean {
  const init = attr.initializer;
  if (!init) return false;
  if (ts.isStringLiteral(init)) return Number(init.text) >= 0;
  if (ts.isJsxExpression(init) && init.expression) {
    const expr = init.expression;
    if (ts.isNumericLiteral(expr)) return Number(expr.text) >= 0;
    if (ts.isStringLiteral(expr)) return Number(expr.text) >= 0;
  }
  return false;
}

interface Scan {
  /** `file:line — reason` for every contract breach. */
  failures: string[];
  /** `file:line — reason` for every TOKEN literal not inside a scanned className. */
  strays: string[];
  /** Number of JSX elements whose className scrolls horizontally. */
  regions: number;
}

function scanFile(path: string): Scan {
  const text = readFileSync(path, "utf8");
  const sf = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TSX);
  const rel = relative(process.cwd(), path);
  const failures: string[] = [];
  const strays: string[] = [];
  const classNameRanges: Array<{ start: number; end: number }> = [];
  let regions = 0;

  const at = (pos: number) => `${rel}:${sf.getLineAndCharacterOfPosition(pos).line + 1}`;

  const visit = (node: ts.Node): void => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tagName = node.tagName.getText(sf);
      const attrs = node.attributes.properties.filter(ts.isJsxAttribute);
      const className = attrs.find((a) => a.name.getText(sf) === "className");
      if (className && classNameChunks(className).some((c) => c.includes(TOKEN))) {
        regions += 1;
        classNameRanges.push({ start: className.getStart(sf), end: className.getEnd() });
        if (!FOCUSABLE_WRAPPERS.has(tagName)) {
          const where = at(node.getStart(sf));
          const tabIndex = attrs.find((a) => a.name.getText(sf) === "tabIndex");
          if (!tabIndex) {
            failures.push(`${where} — <${tagName}> has \`${TOKEN}\` but no tabIndex`);
          } else if (!tabIndexIsFocusable(tabIndex)) {
            failures.push(
              `${where} — <${tagName}> tabIndex must be a non-negative literal, got \`${tabIndex.initializer?.getText(sf) ?? ""}\``,
            );
          }
          const named = attrs.some((a) => {
            const name = a.name.getText(sf);
            return name === "aria-label" || name === "aria-labelledby";
          });
          if (!named) {
            failures.push(
              `${where} — <${tagName}> has \`${TOKEN}\` but no aria-label/aria-labelledby (a focusable region needs a name)`,
            );
          }
        }
      }
    }

    if (
      (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) &&
      node.text.includes(TOKEN)
    ) {
      const pos = node.getStart(sf);
      // The owning element's className range is pushed before its attributes
      // are visited, so coverage is already known here.
      const covered = classNameRanges.some((r) => pos >= r.start && pos < r.end);
      if (!covered) {
        strays.push(`${at(pos)} — \`${TOKEN}\` outside a JSX className (class hidden in a variable?)`);
      }
    }

    ts.forEachChild(node, visit);
  };
  visit(sf);

  return { failures, strays, regions };
}

const files = walk(SRC);
const scanned = () => files.map(scanFile);

describe("M102 scroll regions — every overflow-x-auto box is keyboard-reachable", () => {
  it("parses the whole tree (a broken scan must fail here, not pass vacuously)", () => {
    expect(files.length).toBeGreaterThan(150);
    expect(scanned().reduce((n, s) => n + s.regions, 0)).toBeGreaterThanOrEqual(26);
  });

  it("finds no unreachable scroll box anywhere in src/", () => {
    expect(scanned().flatMap((s) => s.failures)).toEqual([]);
  });

  it("attributes every overflow-x-auto literal to a scanned className", () => {
    expect(scanned().flatMap((s) => s.strays)).toEqual([]);
  });

  it("Card forwards the scroll-region attributes to its DOM node", () => {
    // DividendsPanel/ProjectionsPanel scroll the CARD itself (their table is
    // the card's only child), so the contract rides on Card's prop spread —
    // if that ever stops forwarding, those two sites silently lose focus.
    const { container } = render(
      <Card tabIndex={0} role="region" aria-label="Outcome per scenario table" className="overflow-x-auto p-4">
        <table>
          <tbody>
            <tr>
              <td>row</td>
            </tr>
          </tbody>
        </table>
      </Card>,
    );
    const card = container.firstElementChild as HTMLElement;
    expect(card.className).toContain("overflow-x-auto");
    expect(card.tabIndex).toBe(0);
    expect(card.getAttribute("role")).toBe("region");
    expect(card.getAttribute("aria-label")).toBe("Outcome per scenario table");
    expect(screen.getByRole("region", { name: "Outcome per scenario table" })).toBe(card);
  });
});