// M105 — the TWR/MWR display trim: the engine's fixed 8-decimal wire strings
// must read like a person's number WITHOUT touching the wire contract
// (docs/05 §Total return → Display, docs/09 §Tooltips & in-app help).
//
//   1. `formatPct` SLICES to a 2-decimal display — a trim, never a re-round —
//      and fail-opens on any shape that is not exactly `[-]digits.8dp`;
//   2. `toneOf` reads the RAW string, so a value that merely TRIMS to zero
//      keeps its gain/danger colour instead of going muted;
//   3. the card shows `+21.38%` while the full `21.38091492%` stays reachable
//      through the span's `aria-label` and the value's own `HelpHint`
//      (`21.38091492% precise`) — the trim is display sugar, not loss;
//   4. axe-core reports zero violations with that precision hint open (the
//      `aria-label` must not cost the card its clean audit).
//
// The values in the render fixture are the two from Mark's screenshot.
import { createElement, type ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import axe from "axe-core";

import { ThemeProvider } from "../src/theme/ThemeProvider";
import {
  TotalReturnMetrics,
  formatPct,
  toneOf,
} from "../src/components/investments/TotalReturnMetrics";
// M160-R2 (t_85311c34): getTotalReturn lives in `api/reports.ts` again, a
// thin read of `GET /reports/total-return`.
import { getTotalReturn } from "../src/api/reports";

vi.mock("../src/api/reports", () => ({ getTotalReturn: vi.fn() }));

// jsdom has no matchMedia; ThemeProvider/useReducedMotion call it.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => cleanup());

function withProviders(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const q = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  // `<main>` so axe's `region` rule has a landmark to file the card under —
  // the same reason helpHint.test.ts renders its trigger inside one.
  return createElement(ThemeProvider, null, createElement(q, null, createElement("main", null, ui)));
}

function mockReturn(twr_pct: string | null, mwr_pct: string | null) {
  vi.mocked(getTotalReturn).mockResolvedValue({
    from: "2026-01-01",
    to: "2026-09-30",
    status: "ok",
    start_value_cad: "100000.00",
    end_value_cad: "121381.00",
    net_flow_cad: "0.00",
    income_cad: "2400.00",
    twr_pct,
    mwr_pct,
    valuation_days: 272,
    active_days: 270,
    flow_days: 0,
    account_count: 3,
  } as never);
}

/** The `HelpHint` trigger sitting next to a given displayed value — the value's
 *  own precision hint, not the label's derivation hint. */
function hintBeside(displayPrefix: string): HTMLButtonElement {
  const all = [...document.querySelectorAll<HTMLButtonElement>(
    'button[aria-label="What does this mean?"]',
  )];
  const hit = all.find((b) => (b.parentElement?.textContent ?? "").trim().startsWith(displayPrefix));
  if (!hit) throw new Error(`no hint beside ${JSON.stringify(displayPrefix)}`);
  return hit;
}

function tipOf(btn: HTMLButtonElement): HTMLElement {
  const id = btn.getAttribute("aria-describedby");
  const tip = id === null ? null : document.getElementById(id);
  if (!tip) throw new Error("aria-describedby did not resolve to an element");
  return tip;
}

// --- the pure display helpers ----------------------------------------------

describe("formatPct — display-only 2dp trim of the 8-decimal wire string", () => {
  it("slices an 8-decimal wire value to 2 decimals, sign preserved", () => {
    expect(formatPct("21.38091492")).toBe("+21.38%");
    expect(formatPct("20.65817326")).toBe("+20.65%");
    expect(formatPct("-0.07000000")).toBe("-0.07%");
    expect(formatPct("-20.65817326")).toBe("-20.65%");
  });

  it("trims the engine's trailing zeros to the same 2-decimal reading", () => {
    expect(formatPct("10.90000000")).toBe("+10.90%");
    expect(formatPct("0.50000000")).toBe("+0.50%");
    expect(formatPct("123.00000000")).toBe("+123.00%");
  });

  it("an exact zero stays unsigned and muted — same language as before", () => {
    expect(formatPct("0.00000000")).toBe("0.00%");
    expect(toneOf("0.00000000")).toBe("text-muted");
  });

  it("null is still the M65 'No data', never a confident 0.00%", () => {
    expect(formatPct(null)).toBe("No data");
  });

  it("FAILS OPEN: any shape that is not exactly [-]digits.8dp passes through unchanged", () => {
    expect(formatPct("10.5")).toBe("+10.5%");
    expect(formatPct("-0.07")).toBe("-0.07%");
    expect(formatPct("21.380914920")).toBe("+21.380914920%"); // 9 dp — not our wire
    expect(formatPct("21.38")).toBe("+21.38%"); // already 2 dp
    expect(formatPct("1E-8")).toBe("+1E-8%"); // scientific notation, not plain
    expect(formatPct("0.0000000000")).toBe("+0.0000000000%"); // 10 dp zero is not THE zero
  });

  it("tone comes from the RAW string, never from the trimmed display", () => {
    // A return that trims to "0.00" is still a return: green, not muted.
    expect(formatPct("0.00000123")).toBe("+0.00%");
    expect(toneOf("0.00000123")).toBe("text-gain");
    expect(formatPct("-0.00000123")).toBe("-0.00%");
    expect(toneOf("-0.00000123")).toBe("text-danger");
    expect(toneOf("21.38091492")).toBe("text-gain");
    expect(toneOf("-0.07000000")).toBe("text-danger");
    // A negative zero from the wire is not THE wire zero either — it keeps the
    // sign and the danger tone, exactly as the raw-string rule demands.
    expect(formatPct("-0.00000000")).toBe("-0.00%");
    expect(toneOf("-0.00000000")).toBe("text-danger");
  });
});

// --- the card ----------------------------------------------------------------

describe("TotalReturnMetrics — a person's number with the precision kept", () => {
  it("shows 2 decimals, carries the full 8 in aria-label + hint, keeps the tone", async () => {
    mockReturn("21.38091492", "20.65817326");
    render(withProviders(createElement(TotalReturnMetrics, { from: "2026-01-01", to: "2026-09-30" })));

    await waitFor(() => {
      expect(screen.getByText("+21.38%")).toBeDefined();
      expect(screen.getByText("+20.65%")).toBeDefined();
    });

    // The wire string never renders as visible text…
    const twr = screen.getByText("+21.38%");
    expect(twr.tagName).toBe("SPAN");
    // …it is the span's aria-label, so assistive tech gets the real number.
    expect(twr.getAttribute("aria-label")).toBe("21.38091492%");
    expect(screen.getByText("+20.65%").getAttribute("aria-label")).toBe("20.65817326%");
    // and the gain/danger colour still comes from the raw sign.
    expect(twr.parentElement?.className).toContain("text-gain");

    // The full precision is one hover/tap away on the value itself.
    const hint = hintBeside("+21.38%");
    const tip = tipOf(hint);
    expect(tip.classList.contains("sr-only")).toBe(true);
    expect(tip.textContent).toBe("21.38091492% precise");
    act(() => {
      hint.focus();
      fireEvent.focus(hint);
    });
    expect(tip.classList.contains("sr-only")).toBe(false);
    fireEvent.keyDown(hint, { key: "Escape" });
    expect(tip.classList.contains("sr-only")).toBe(true);

    // The M97 derivation hint on the label still owns its own slot.
    const labelHint = hintBeside("Time-weighted (TWR)");
    expect(tipOf(labelHint).textContent).toContain("chain-linked");
  });

  it("a raw exact zero renders muted '0.00%' with its precision still on offer", async () => {
    mockReturn("0.00000000", "0.00000000");
    render(withProviders(createElement(TotalReturnMetrics, { from: "2026-01-01", to: "2026-09-30" })));

    const zeros = await screen.findAllByText("0.00%"); // both metrics are zero
    expect(zeros.length).toBe(2);
    const zero = zeros[0];
    expect(zero.parentElement?.className).toContain("text-muted");
    expect(zero.getAttribute("aria-label")).toBe("0.00000000%");
    expect(tipOf(hintBeside("0.00%")).textContent).toBe("0.00000000% precise");
  });

  it("a null return renders 'No data' with no precision affordance at all", async () => {
    mockReturn(null, null);
    render(withProviders(createElement(TotalReturnMetrics, { from: "2026-01-01", to: "2026-09-30" })));

    expect((await screen.findAllByText("No data")).length).toBe(2); // TWR and MWR
    // Two label hints per metric… four labels, and nothing on the values
    // themselves — the precision affordance only earns its space when the
    // display actually hides digits.
    const triggers = document.querySelectorAll('button[aria-label="What does this mean?"]');
    expect(triggers.length).toBe(4);
    expect([...triggers].some((b) => b.parentElement?.textContent?.includes("precise"))).toBe(false);
  });

  it("axe-core reports zero violations with the precision hint open", async () => {
    mockReturn("21.38091492", "20.65817326");
    render(withProviders(createElement(TotalReturnMetrics, { from: "2026-01-01", to: "2026-09-30" })));
    await screen.findByText("+21.38%");

    const options: axe.RunOptions = {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false } },
    };
    const closed = await axe.run(document.body, options);
    expect(closed.violations.map((v) => v.id)).toEqual([]);

    const hint = hintBeside("+21.38%");
    act(() => {
      hint.focus();
      fireEvent.focus(hint);
    });
    const open = await axe.run(document.body, options);
    expect(open.violations.map((v) => v.id)).toEqual([]);
  });
});
