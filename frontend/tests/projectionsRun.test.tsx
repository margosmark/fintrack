// M157 (M135 §2 K-12) — behaviour: the projection RUN is SERVER-computed.
//
// Before M61 #2 the panel POSTed and the read-only demo tenant 403ed it (403
// demo_account_restricted), which replaced the whole calculator with an
// ErrorPanel before a single chart line drew — so the run moved into the
// browser. That posture was a DEMO WRITE-GUARD artifact, not an E2EE one, and
// M157 removed the artifact instead of the POST: `/projections/run` joined
// `DEMO_MUTATION_EXEMPT_PATHS` (pure request-body math, no row is written),
// and this panel calls the endpoint again.
//
// These assertions pin the flip:
//
//   1. clicking "Run projections" POSTs `runProjections` exactly once with the
//      payload the panel built — the client engine is never asked to compute
//      (its mock resolves to undefined, so a leftover `runProjectionEngine`
//      call would return undefined and no chart would render).
//   2. a demo session reaches the server like any other tenant, and still gets
//      exactly ONE read-only message — the toast, at most once per page
//      (m61 #2) — because nothing is ever saved.
//   3. a 422 `validation_error` envelope renders as ONE ErrorPanel with the
//      bounds copy, not as a bare status text (`detail` is a pydantic-shaped
//      LIST in the envelope, so the shared ApiError cannot carry a sentence).
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../src/api/client";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { ToastProvider } from "../src/components/ui/Toast";
import { ProjectionsPanel } from "../src/components/reports/ProjectionsPanel";

const a = vi.hoisted(() => ({
  user: null as { email: string } | null,
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: a.user }),
}));
// Both exports this time: the panel POSTs the run (M157) and still pre-fills
// from GET /projections/defaults.
vi.mock("../src/api/projections", () => ({
  getProjectionDefaults: vi.fn(),
  runProjections: vi.fn(),
}));
// The mirror stays alive as the offline/E2EE fallback (docs/33 §Server-first
// rule 5) but the panel must no longer ASK it to compute: `runProjectionEngine`
// below resolves to undefined, so any leftover call renders nothing and the
// chart assertions fail.
vi.mock("../src/lib/projectionEngine", () => ({
  ProjectionInputError: class ProjectionInputError extends Error {
    readonly code = "validation_error";
  },
  runProjectionEngine: vi.fn(),
}));

// jsdom has no matchMedia; ThemeProvider and useReducedMotion both call it.
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

// ResponsiveContainer measures the DOM, which jsdom cannot do (donut.test.ts):
// hand every chart a fixed box so the axes and lines actually render.
vi.mock("recharts", async () => {
  const mod = await import("recharts");
  const ReactMod = await import("react");
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => {
      const child = Array.isArray(children) ? children[0] : children;
      if (child && typeof child === "object" && "type" in (child as object)) {
        return ReactMod.cloneElement(child as React.ReactElement, {
          width: 480,
          height: 320,
        });
      }
      return child;
    },
  };
});

function series(values: number[]) {
  return values.map((value_cad, i) => ({
    year: 2026 + i,
    contributed_cad: "0.00",
    value_cad: String(value_cad),
    value_real_cad: String(value_cad),
  }));
}

const PROJECTION = {
  start_value_cad: "0",
  years: 5,
  inflation_pct: "2",
  scenarios: [
    {
      label: "Balanced",
      series: series([0, 300000, 600000, 900000, 1200000]),
      final_value_cad: "1200000.00",
      total_contributed_cad: "0.00",
      total_gain_cad: "1200000.00",
    },
    {
      label: "Conservative",
      series: series([0, 250000, 500000, 750000, 1000000]),
      final_value_cad: "1000000.00",
      total_contributed_cad: "0.00",
      total_gain_cad: "1000000.00",
    },
  ],
};

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <ThemeProvider>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <ProjectionsPanel />
        </ToastProvider>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("theme", "light");
  a.user = null;
  vi.clearAllMocks();
});

describe("Projections run on the server (M157 / M135 §2 K-12)", () => {
  it("POSTs the run once and renders the chart and table with no error boundary", async () => {
    const { getProjectionDefaults, runProjections } = await import("../src/api/projections");
    vi.mocked(getProjectionDefaults).mockResolvedValue({ start_value_cad: null });
    vi.mocked(runProjections).mockResolvedValue(PROJECTION as never);

    const { container } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Run projections/ }));

    // The results render AND the endpoint was the thing that produced them.
    expect(await screen.findByText("Outcome per scenario")).toBeTruthy();
    expect(screen.getByText("Projected value over 5 years")).toBeTruthy();
    expect(screen.queryByText(/Something went wrong/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();

    // Exactly one POST, carrying the panel's own inputs (defaults prefilled
    // nothing here, so the start value is the empty-string fallback "0").
    await waitFor(() => expect(runProjections).toHaveBeenCalledTimes(1));
    expect(vi.mocked(runProjections).mock.calls[0]?.[0]).toEqual({
      start_value_cad: "0",
      annual_contribution_cad: "1200",
      years: 25,
      inflation_pct: "2",
      scenarios: [{ label: "Balanced", annual_return_pct: "6.5" }],
    });

    // Both scenarios plus the dashed real line draw on ONE chart — the thing
    // the demo tenant's 403 used to prevent entirely.
    await waitFor(() =>
      expect(container.querySelectorAll(".recharts-line").length).toBeGreaterThanOrEqual(2),
    );
    expect(screen.getByText("Balanced")).toBeTruthy();
    expect(screen.getByText("Conservative")).toBeTruthy();
  });

  it("runs for the demo tenant — the endpoint is no longer a 403 for it", async () => {
    a.user = { email: "demo@fintracker.local" };
    const { getProjectionDefaults, runProjections } = await import("../src/api/projections");
    vi.mocked(getProjectionDefaults).mockResolvedValue({ start_value_cad: null });
    vi.mocked(runProjections).mockResolvedValue(PROJECTION as never);

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Run projections/ }));

    // The demo reaches the server exactly like any other tenant (the route is
    // a documented demo-write exemption now) — no ErrorPanel, chart and table
    // both on screen.
    expect(await screen.findByText("Outcome per scenario")).toBeTruthy();
    expect(runProjections).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).toBeNull();

    expect(await screen.findByText("Demo is read-only — scenario not saved")).toBeTruthy();
    // At most one read-only string per page: the toast is it (the old inline
    // note beside the button is gone by construction).
    const occurrences = document.body.textContent?.match(/read-only/g) ?? [];
    expect(occurrences).toHaveLength(1);
  });

  it("renders the server's 422 as one ErrorPanel with the bounds copy", async () => {
    const { getProjectionDefaults, runProjections } = await import("../src/api/projections");
    vi.mocked(getProjectionDefaults).mockResolvedValue({ start_value_cad: null });
    // The envelope errors.py ships: `detail` is a pydantic-shaped LIST, so
    // `ApiError.message` is only the status text — the panel must not print it.
    vi.mocked(runProjections).mockRejectedValue(
      new ApiError(422, "Unprocessable Entity", "validation_error"),
    );

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Run projections/ }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/An input is outside the allowed range/)).toBeTruthy();
    expect(screen.queryByText("Unprocessable Entity")).toBeNull();
    expect(screen.queryByText("Outcome per scenario")).toBeNull();
    expect(screen.queryByText("Demo is read-only — scenario not saved")).toBeNull();
  });

  it("keeps the generic copy for a failure that carries no validation code", async () => {
    const { getProjectionDefaults, runProjections } = await import("../src/api/projections");
    vi.mocked(getProjectionDefaults).mockResolvedValue({ start_value_cad: null });
    vi.mocked(runProjections).mockRejectedValue(new Error("network down"));

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Run projections/ }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    // Two matches on purpose: ErrorPanel's own heading, plus the generic
    // fallback the panel passes as the detail line.
    expect(screen.getAllByText("Something went wrong")).toHaveLength(2);
    expect(screen.queryByText(/An input is outside the allowed range/)).toBeNull();
  });

  it("non-demo session runs silently — no read-only note at all", async () => {
    a.user = { email: "owner@example.com" };
    const { getProjectionDefaults, runProjections } = await import("../src/api/projections");
    vi.mocked(getProjectionDefaults).mockResolvedValue({ start_value_cad: null });
    vi.mocked(runProjections).mockResolvedValue(PROJECTION as never);

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Run projections/ }));

    expect(await screen.findByText("Outcome per scenario")).toBeTruthy();
    expect(screen.queryByText(/read-only/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
