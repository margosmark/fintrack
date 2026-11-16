// M89 G1 — the first-session coach marks (guided tour).
//
// Acceptance under test, in order:
//   * fires on the demo tenant's FIRST dashboard visit only (a second fresh
//     session — localStorage already answered — shows nothing);
//   * never fires for a signed-in account or off the dashboard;
//   * the dismissal is persistent per account, not per page-load;
//   * "Take the tour" resurfaces it after completion;
//   * keyboard: focus starts on the primary control, Escape dismisses, the
//     four marks advance and completion persists;
//   * a measurable target gets a spotlight ring.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Tour } from "../src/components/tour/Tour";
import {
  TOUR_START_EVENT,
  TOUR_STORAGE_PREFIX,
  tourStorageKey,
} from "../src/components/tour/tourState";
import type { User } from "../src/types";

const DEMO = { id: "7", email: "demo@fintracker.local" } as unknown as User;
const SIGNED_IN = { id: "8", email: "sam@example.com" } as unknown as User;

function renderTour(user: User, path = "/dashboard") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Tour user={user} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe("first-session coach marks", () => {
  it("starts on the demo tenant's first dashboard visit", () => {
    renderTour(DEMO);
    expect(screen.getByTestId("tour")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
    // M185: the chrome changed (tabs + More), so the first mark's title
    // followed it (docs/47 §1.1).
    expect(screen.getByText("Everything lives one tap away")).toBeTruthy();
    expect(screen.getByTestId("tour-next").textContent).toBe("Next");
    expect(screen.getByTestId("tour-skip").textContent).toBe("Skip");
    // 4 marks: nav, imports, reports, help
    expect(screen.getByText(/Tour · 1 of 4/)).toBeTruthy();
  });

  // M92 F-M91-1: the imports mark is shown to the demo tenant pointing at the
  // Imports nav row, so it may never imply a live demo upload — that answers
  // 403 demo_account_restricted (docs/20).
  it("the imports mark never promises a live demo upload (M92 F-M91-1)", () => {
    renderTour(DEMO);
    fireEvent.click(screen.getByTestId("tour-next")); // nav -> imports
    expect(screen.getByText("How your data gets in")).toBeTruthy();
    const body = screen.getByRole("dialog").textContent ?? "";
    expect(body).toMatch(/read-only/);
    expect(body).toMatch(/first-import walkthrough/);
    expect(body).not.toMatch(/re-uploading the same file/);
  });

  it("does not fire a second time in a fresh session once completed", async () => {
    const first = renderTour(DEMO);
    fireEvent.click(screen.getByTestId("tour-next")); // nav -> imports
    fireEvent.click(screen.getByTestId("tour-next")); // imports -> reports
    fireEvent.click(screen.getByTestId("tour-next")); // reports -> help
    expect(screen.getByTestId("tour-next").textContent).toBe("Done");
    fireEvent.click(screen.getByTestId("tour-next"));
    first.unmount();

    expect(window.localStorage.getItem(tourStorageKey(DEMO))).toBe("completed");
    renderTour(DEMO);
    expect(screen.queryByTestId("tour")).toBeNull();
  });

  it("persists a skip across sessions", () => {
    const first = renderTour(DEMO);
    fireEvent.click(screen.getByTestId("tour-skip"));
    expect(screen.queryByTestId("tour")).toBeNull();
    expect(window.localStorage.getItem(tourStorageKey(DEMO))).toBe("skipped");
    first.unmount();

    renderTour(DEMO);
    expect(screen.queryByTestId("tour")).toBeNull();
  });

  it("never fires for a signed-in account", () => {
    renderTour(SIGNED_IN);
    expect(screen.queryByTestId("tour")).toBeNull();
  });

  it("never fires off the dashboard", () => {
    renderTour(DEMO, "/help");
    expect(screen.queryByTestId("tour")).toBeNull();
  });

  it("does not restart inside the same tab-session", () => {
    window.sessionStorage.setItem("fin_tracker.tour.session-started", "1");
    renderTour(DEMO);
    expect(screen.queryByTestId("tour")).toBeNull();
  });

  it("resurfaces on request even after completion", () => {
    window.localStorage.setItem(tourStorageKey(DEMO), "completed");
    renderTour(DEMO);
    expect(screen.queryByTestId("tour")).toBeNull();

    act(() => {
      window.dispatchEvent(new CustomEvent(TOUR_START_EVENT));
    });
    expect(screen.getByTestId("tour")).toBeTruthy();
  });

  it("dismisses on Escape and remembers the dismissal", () => {
    renderTour(DEMO);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("tour")).toBeNull();
    expect(window.localStorage.getItem(tourStorageKey(DEMO))).toBe("skipped");
  });

  it("puts focus on the primary control and keeps it inside the card", async () => {
    renderTour(DEMO);
    const next = screen.getByTestId("tour-next");
    const skip = screen.getByTestId("tour-skip");
    await waitFor(() => expect(document.activeElement).toBe(next));

    // Tab on the last control wraps to the first.
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Tab" });
    expect(document.activeElement).toBe(skip);
    // Shift+Tab on the first control wraps back to the last.
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(next);
  });

  it("keys its storage per account", () => {
    expect(tourStorageKey(DEMO)).toBe(`${TOUR_STORAGE_PREFIX}7`);
    expect(tourStorageKey(SIGNED_IN)).toBe(`${TOUR_STORAGE_PREFIX}8`);
    expect(
      window.localStorage.getItem(tourStorageKey({ email: "a@b.c" })),
    ).toBeNull();
  });

  it("spotlights a target that is on screen", () => {
    const target = document.createElement("div");
    target.setAttribute("data-tour", "nav");
    target.getBoundingClientRect = () =>
      ({
        x: 10,
        y: 10,
        left: 10,
        top: 10,
        right: 210,
        bottom: 60,
        width: 200,
        height: 50,
        toJSON: () => ({}),
      }) as DOMRect;
    document.body.appendChild(target);

    renderTour(DEMO);
    expect(screen.getByTestId("tour-spotlight")).toBeTruthy();
    target.remove();
  });
});
