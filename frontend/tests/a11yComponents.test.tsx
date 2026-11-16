// Component-level accessibility contracts (docs/09 "Accessibility — WCAG 2.1
// AA (M58)", channel B companion). axe-core proves the DOM of the six audited
// pages is clean; these tests pin the PRIMITIVES those pages are built from, so
// a new dialog/chart/error surface inherits the contract instead of re-deciding
// it: chart text alternatives, error announcements, the shared dialog focus
// hand-off, and the visually-hidden value pattern that replaced the prohibited
// `aria-label` on a generic <span>.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatedNumber } from "../src/components/ui/AnimatedNumber";
import { ChartFrame } from "../src/components/charts/ChartFrame";
import { ErrorPanel } from "../src/components/ui/EmptyState";
import {
  chartAriaLabel,
  dateRange,
  fromToFact,
  granularityLabel,
  shareFact,
} from "../src/components/charts/aria";
import { useDialogFocus, useEscapeToClose } from "../src/components/ui/dialogA11y";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ChartFrame + chartAriaLabel (docs/12 \"Chart accessibility\")", () => {
  it("renders one role=img leaf with the summary as its accessible name", () => {
    const label = chartAriaLabel({
      title: "Net worth trend",
      kind: "monthly buckets",
      range: "2026-01-31 – 2026-12-31",
      points: 12,
      facts: ["value $100,000 to $151,800", "peak $152,000"],
    });
    render(
      <ChartFrame label={label}>
        <svg aria-hidden>
          <path d="M0 0" />
        </svg>
      </ChartFrame>,
    );
    const frame = screen.getByRole("img");
    expect(frame.getAttribute("aria-label")).toBe(
      "Net worth trend — monthly buckets, 2026-01-31 – 2026-12-31, 12 points. " +
        "value $100,000 to $151,800; peak $152,000.",
    );
    // The chart itself stays in the tree (it is presentational inside role=img).
    expect(frame.querySelector("svg")).not.toBeNull();
  });

  it("says 'No data in range.' instead of facts when the series is empty", () => {
    expect(
      chartAriaLabel({ title: "Dividends", kind: "monthly buckets", points: 0 }),
    ).toBe("Dividends — monthly buckets, 0 points. No data in range.");
  });

  it("uses the singular for one point and omits the range for snapshots", () => {
    expect(chartAriaLabel({ title: "Holdings", kind: "categorical breakdown", points: 1 })).toBe(
      "Holdings — categorical breakdown, 1 point.",
    );
  });

  it("maps the docs/12 granularity enum to readable words", () => {
    expect(granularityLabel("day")).toBe("daily buckets");
    expect(granularityLabel("week")).toBe("weekly buckets");
    expect(granularityLabel("year")).toBe("yearly buckets");
    expect(granularityLabel("month")).toBe("monthly buckets");
    expect(granularityLabel(undefined)).toBe("monthly buckets");
  });

  it("builds range / endpoint / share facts", () => {
    expect(dateRange("2026-01-01", "2026-01-01")).toBe("2026-01-01");
    expect(dateRange("2026-01-01", "2026-12-31")).toBe("2026-01-01 – 2026-12-31");
    expect(dateRange(null, "2026-12-31")).toBe("no range");
    expect(fromToFact(100000, 151800, "value")).toContain("100,000");
    expect(shareFact("Groceries", 34, 100)).toBe("Groceries 34%");
    expect(shareFact("Groceries", 0, 0)).toBe("Groceries");
  });
});

describe("AnimatedNumber (docs/09 \"Naming & semantics contract\")", () => {
  it("keeps the tween aria-hidden and ships the exact figure as sr-only text", () => {
    const { container } = render(<AnimatedNumber value={60} />);
    const tabular = container.querySelector(".tabular");
    expect(tabular).not.toBeNull();
    // aria-label is PROHIBITED on a generic <span> (axe aria-prohibited-attr).
    expect(tabular?.hasAttribute("aria-label")).toBe(false);
    expect(tabular?.querySelector("[aria-hidden]")).not.toBeNull();
    expect(tabular?.querySelector(".sr-only")?.textContent).toBe("$60.00");
  });
});

describe("ErrorPanel (docs/09 error announcement)", () => {
  it("is a live region carrying the API detail", () => {
    render(<ErrorPanel message="boom 422 invalid_symbol" />);
    const panel = screen.getByRole("alert");
    expect(panel.textContent).toContain("boom 422 invalid_symbol");
    // The decorative icon must not add noise to the announcement.
    expect(panel.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
  });
});

function DialogHarness({ onClose }: { onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useDialogFocus(ref);
  useEscapeToClose(onClose);
  return (
    <div ref={ref} role="dialog" aria-modal="true" tabIndex={-1}>
      <button type="button">First field</button>
    </div>
  );
}

/** Mirrors `ModalPortal`: children land in `document.body` a tick late. */
function LatePortalDialog() {
  const ref = useRef<HTMLDivElement>(null);
  const [attached, setAttached] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setAttached(true), 20);
    return () => window.clearTimeout(timer);
  }, []);
  useDialogFocus(ref);
  if (!attached) return null;
  return createPortal(
    <div ref={ref} role="dialog" aria-modal="true" tabIndex={-1}>
      <button type="button">Late</button>
    </div>,
    document.body,
  );
}

describe("dialogA11y (docs/09 \"Keyboard contract\")", () => {
  it("moves focus into the dialog on open and hands it back on close", () => {
    const opener = document.createElement("button");
    opener.textContent = "Open";
    document.body.appendChild(opener);
    opener.focus();
    expect(document.activeElement).toBe(opener);

    const view = render(<DialogHarness onClose={() => undefined} />);
    const dialog = screen.getByRole("dialog");
    expect(document.activeElement).toBe(dialog);

    view.unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("focuses a dialog whose portal attaches one commit later", async () => {
    // Regression (M58b): `TransactionEditor` mounted its portal from its own
    // effect, so `ref.current` was still null when `useDialogFocus` ran and
    // focus never moved — the dialog opened behind the page (WCAG 2.4.3).
    const view = render(<LatePortalDialog />);
    expect(document.activeElement?.getAttribute("role")).not.toBe("dialog");
    await waitFor(() =>
      expect(document.activeElement?.getAttribute("role")).toBe("dialog"),
    );
    view.unmount();
  });

  it("closes on Escape from anywhere in the document, and stops listening on unmount", () => {
    const onClose = vi.fn();
    render(<DialogHarness onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    // Escape from OUTSIDE the dialog container still counts (document-level).
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("removes its document listener when unmounted", () => {
    const onClose = vi.fn();
    const view = render(<DialogHarness onClose={onClose} />);
    view.unmount();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("dialog markup contract", () => {
  it("keeps dialogs modal + focusable so assistive tech scopes the content", () => {
    render(<DialogHarness onClose={() => undefined} />);
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    // tabIndex={-1} is what lets useDialogFocus move focus onto the container.
    expect(dialog.getAttribute("tabindex")).toBe("-1");
  });
});
