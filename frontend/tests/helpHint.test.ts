// M97 E1 — the tooltip CONTRACT of the shared `HelpHint` primitive
// (docs/09 §Tooltips & in-app help).
//
// The pre-M97 primitive was a native `title=` attribute: hover-only, invisible
// to keyboard focus, undismissable, and clipped by any scrolling container.
// This file pins the replacement's behaviour, not its copy:
//
//   1. content is in the tree AT ALL TIMES (`sr-only` while closed) behind a
//      wired `aria-describedby`, so assistive tech reads the explanation from
//      the focused trigger with no hover anywhere in the loop;
//   2. focus opens it (a superset of `:focus-visible`), Escape dismisses it
//      while focus stays put;
//   3. a TAP opens it at a 390px viewport — pointer events of type `touch`
//      deliberately do NOT hover-open, because browsers fire pointerenter on
//      touch too and that would open-then-instantly-close it;
//   4. axe-core reports zero violations on both states (structure/ARIA —
//      `color-contrast` is channel A's job in a painted browser and
//      docs/09's channel B in tokens; jsdom paints nothing).
import { createElement } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import axe from "axe-core";
import { HelpHint } from "../src/components/ui/HelpHint";

const TEXT = "Assets − Liabilities, as of 2026-10-03: assets $1,200.00 − liabilities $200.00.";

const originalWidth = window.innerWidth;

afterEach(() => {
  cleanup();
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: originalWidth });
});

/** Trigger by its stable accessible name (the explanation rides the
 *  description, not the name — see HelpHint's docblock). */
function trigger(): HTMLButtonElement {
  return screen.getByRole("button", { name: "What does this mean?" }) as HTMLButtonElement;
}

function bubble(): HTMLElement {
  return screen.getByRole("tooltip");
}

/** jsdom splits what a browser does atomically: `.focus()` moves
 *  `document.activeElement` but does not deliver the focus event React's
 *  `onFocus` handler listens for, while `fireEvent.focus` delivers the event
 *  without moving focus. Do both so the assertions see the real contract. */
function focusTrigger(): void {
  const target = trigger();
  act(() => {
    target.focus();
    fireEvent.focus(target);
  });
}

function isOpen(): boolean {
  const cls = bubble().className;
  return cls.includes("fixed") && !cls.includes("sr-only");
}

function setViewport(width: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: width });
  window.dispatchEvent(new Event("resize"));
}

describe("HelpHint — the replaceable gap (docs/09, M82-F2)", () => {
  it("keeps the default ml-1 gap unless the caller replaces it", () => {
    render(createElement(HelpHint, { text: "Default gap." }));
    expect(trigger().classList.contains("ml-1")).toBe(true);
    cleanup();

    // className REPLACES the gap (the /rules Priority label uses this to fit
    // a 36.7px 2-track cell at 768/200% root font): a leftover `ml-1` would
    // fight `ml-0.5` for margin-left and win by stylesheet order.
    render(createElement(HelpHint, { text: "Tight gap.", className: "ml-0.5 lg:ml-1" }));
    const tight = trigger();
    expect(tight.classList.contains("ml-0.5")).toBe(true);
    expect(tight.classList.contains("lg:ml-1")).toBe(true);
    expect(tight.classList.contains("ml-1")).toBe(false);
    // The rest of the contract is the component's own, never the caller's.
    expect(tight.classList.contains("size-4")).toBe(true);
  });
});

describe("HelpHint — contrast tokens (docs/09 pairs 2 / 12, both themes)", () => {
  it("the bubble is painted with the documented opaque pair, open and closed", () => {
    render(createElement(HelpHint, { text: TEXT }));
    // Closed, the bubble is sr-only (it is the DESCRIPTION, not a painting).
    expect(bubble().className).toBe("sr-only");
    focusTrigger();
    // Changing the fill/ink/edge off these tokens would silently invalidate the
    // documented pairs — channel B measures the TOKENS, this pins the choice.
    const open = bubble().className;
    expect(open).toContain("bg-surface-raised");
    expect(open).toContain("text-text");
    expect(open).toContain("border-control");
  });
});

describe("HelpHint — tooltip contract (M97)", () => {
  it("wires aria-describedby to the tooltip, whose content is in the tree even when closed", () => {
    render(createElement(HelpHint, { text: TEXT }));
    const tip = bubble();
    expect(trigger().getAttribute("aria-describedby")).toBe(tip.id);
    // Closed = visually hidden, NOT absent: a screen reader reading the focused
    // trigger gets the explanation without any hover/focus dance.
    expect(tip.className).toContain("sr-only");
    expect(tip.textContent).toBe(TEXT);
    expect(isOpen()).toBe(false);
  });

  it("opens on FOCUS and keeps the trigger focused", () => {
    render(createElement(HelpHint, { text: TEXT }));
    focusTrigger();
    expect(isOpen()).toBe(true);
    expect(bubble().textContent).toBe(TEXT);
    expect(document.activeElement).toBe(trigger());
  });

  it("dismisses on Escape while focus stays on the trigger", () => {
    render(createElement(HelpHint, { text: TEXT }));
    focusTrigger();
    expect(isOpen()).toBe(true);
    fireEvent.keyDown(trigger(), { key: "Escape" });
    expect(isOpen()).toBe(false);
    expect(document.activeElement).toBe(trigger());
    // Still described after dismissal — the content never leaves the a11y tree.
    expect(trigger().getAttribute("aria-describedby")).toBe(bubble().id);
  });

  it("opens on a TAP at a 390px viewport (phone contract) and toggles closed", () => {
    setViewport(390);
    render(createElement(HelpHint, { text: TEXT }));
    fireEvent.click(trigger());
    expect(isOpen()).toBe(true);
    expect(bubble().textContent).toBe(TEXT);
    // The bubble is width-clamped against the VIEWPORT, not a fixed rem, so a
    // long derivation cannot overflow a 390px screen.
    expect(bubble().className).toContain("max-w-[min(20rem,calc(100vw-1.5rem))]");
    fireEvent.click(trigger());
    expect(isOpen()).toBe(false);
  });

  it("ignores touch pointer enter/leave — tap owns the mobile path", () => {
    render(createElement(HelpHint, { text: TEXT }));
    fireEvent.pointerEnter(trigger(), { pointerType: "touch" });
    expect(isOpen()).toBe(false);
    fireEvent.pointerLeave(trigger(), { pointerType: "touch" });
    expect(isOpen()).toBe(false);
    fireEvent.click(trigger());
    expect(isOpen()).toBe(true);
    // A touch leave (what a phone fires after the tap) must not close it.
    fireEvent.pointerLeave(trigger(), { pointerType: "touch" });
    expect(isOpen()).toBe(true);
  });

  it("opens on a mouse hover and closes when the pointer leaves", () => {
    render(createElement(HelpHint, { text: TEXT }));
    fireEvent.pointerEnter(trigger(), { pointerType: "mouse" });
    expect(isOpen()).toBe(true);
    fireEvent.pointerLeave(trigger(), { pointerType: "mouse" });
    expect(isOpen()).toBe(false);
  });

  it("keeps hover + focus independent: Escape closes it and hover must re-enter to reopen", () => {
    render(createElement(HelpHint, { text: TEXT }));
    fireEvent.pointerEnter(trigger(), { pointerType: "mouse" });
    focusTrigger();
    expect(isOpen()).toBe(true);
    fireEvent.keyDown(trigger(), { key: "Escape" });
    expect(isOpen()).toBe(false);
    fireEvent.pointerEnter(trigger(), { pointerType: "mouse" });
    expect(isOpen()).toBe(true);
  });

  it("the bubble lives in a NAMED landmark layer, not loose at <body>", () => {
    // axe's `region` rule counts every rendered node outside a landmark; the
    // closed bubble is content (the trigger's aria-describedby), so it has to
    // sit somewhere the audit accepts. This is the M97 regression: portalling
    // to document.body put 12 of them on /dashboard and broke docs/09's
    // zero-violation channel-A baseline.
    render(createElement(HelpHint, { text: TEXT }));
    const layer = document.getElementById("help-hint-layer");
    expect(layer, "the hint layer must exist").not.toBeNull();
    expect(layer!.getAttribute("role")).toBe("region");
    expect(layer!.getAttribute("aria-label")).toBeTruthy();
    expect(layer!.contains(bubble())).toBe(true);
    // Inside the layer, the bubble is reachable from the trigger either way.
    expect(trigger().getAttribute("aria-describedby")).toBe(bubble().id);
  });

  it("axe-core: zero violations over the whole document, closed and open", async () => {
    // document.body, NOT the render container: the bubble is portalled into
    // the hint layer, which lives outside the container — scoping to the
    // container would skip exactly the node this test exists to protect.
    // color-contrast is disabled because jsdom paints nothing — docs/09's
    // channel B computes every token pair from source and channel A runs axe
    // in a real painted browser.
    render(createElement("main", null, createElement(HelpHint, { text: TEXT })));
    const options: axe.RunOptions = {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false } },
    };
    const closed = await axe.run(document.body, options);
    expect(closed.violations.map((v) => v.id)).toEqual([]);
    focusTrigger();
    const open = await axe.run(document.body, options);
    expect(open.violations.map((v) => v.id)).toEqual([]);
  });
});
