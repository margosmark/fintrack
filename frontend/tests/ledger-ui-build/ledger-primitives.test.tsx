// M185 ledger-ui-build — the wave's new UI primitives (docs/47 §3, §4, §9.3).
//
// What is pinned here is the CONTRACT, not the styling: the state row is
// exactly one compact row (never a spinner), ErrorPanel's retry is bound to
// the caller's refetch, a comparison never appears without its basis and its
// accessible text is the full sentence, and the DemoLock renders only for a
// demo session on a genuinely blocked write — a live tenant's DOM carries
// zero padlocks (acceptance example 1).

import { fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DemoLock, useDemoRefusal, useDemoWriteLock } from "../../src/components/ui/DemoLock";
import { ComparisonChip } from "../../src/components/ui/ComparisonChip";
import { ErrorPanel } from "../../src/components/ui/EmptyState";
import { StateRow } from "../../src/components/ui/StateRow";

/** Which user the shell sees — the demo guard's first condition. */
let email = "demo@fintracker.local";
const toastInfo = vi.fn();

vi.mock("../../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: email === null ? null : { email } }),
}));
vi.mock("../../src/components/ui/Toast", () => ({
  useToast: () => ({ info: toastInfo, success: vi.fn(), error: vi.fn() }),
}));

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  email = "demo@fintracker.local";
  toastInfo.mockClear();
});
afterEach(() => {
  email = "demo@fintracker.local";
});

describe("StateRow (docs/47 §3)", () => {
  it("loading is ONE compact skeleton row with a status role — no spinner", () => {
    render(<StateRow variant="loading" title="Anything" />);
    const row = screen.getByTestId("state-row-loading");
    expect(row.getAttribute("role")).toBe("status");
    expect(row.querySelectorAll(".skeleton").length).toBeGreaterThan(0);
    // Exactly one state row: a wall of shimmer would be several.
    expect(screen.getAllByTestId(/state-row/)).toHaveLength(1);
  });

  it("keeps the CTA's box (flex-none) and passes its testid through", () => {
    render(
      <StateRow
        variant="warn"
        title="Some prices are not fresh"
        detail="2 holdings last priced more than a week ago"
        action={{ label: "Refresh prices", onClick: () => {}, testId: "stale-refresh" }}
        testId="stale-strip"
      />,
    );
    const row = screen.getByTestId("stale-strip");
    expect(row.textContent).toContain("2 holdings");
    const cta = screen.getByTestId("stale-refresh");
    // The artifact's 360px CTA rule: the action never shrinks below its label.
    expect(cta.className).toContain("whitespace-nowrap");
    expect(cta.parentElement?.className).toContain("flex-none");
    fireEvent.click(cta);
  });
});

describe("ErrorPanel retry (docs/47 §3, acceptance 15)", () => {
  it("carries the pinned testid and calls the query's own refetch", () => {
    const onRetry = vi.fn();
    render(<ErrorPanel message="Boom: upstream" onRetry={onRetry} />);
    const panel = screen.getByTestId("error-panel");
    expect(panel.getAttribute("role")).toBe("alert");
    // The envelope detail renders VERBATIM (docs/47 §3).
    expect(panel.textContent).toContain("Boom: upstream");
    fireEvent.click(screen.getByTestId("error-panel-retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders no retry when the caller passes none (additive contract)", () => {
    render(<ErrorPanel message="Nope" />);
    expect(screen.getByTestId("error-panel")).toBeTruthy();
    expect(screen.queryByTestId("error-panel-retry")).toBeNull();
  });
});

describe("ComparisonChip (docs/47 §9.3 / §6.5)", () => {
  it("a comparison never appears without its basis", () => {
    render(<ComparisonChip delta="$2,140.55" tone="gain" basis="vs Sep 13" />);
    const basis = screen.getByText("vs Sep 13");
    const pill = basis.parentElement as HTMLElement;
    expect(pill.textContent).toContain("$2,140.55");
    expect(pill.className).toContain("text-gain");
  });

  it("the accessible text is the FULL SENTENCE, not the arrow glyph", () => {
    render(
      <ComparisonChip
        delta="$2,140.55"
        tone="gain"
        basis="vs Sep 13"
        ariaLabel="Net worth up $2,140.55 versus September 13"
      />,
    );
    expect(screen.getByText("Net worth up $2,140.55 versus September 13")).toBeTruthy();
    // The visible body is decorative to assistive tech behind the sentence.
    const sr = screen.getByText(/Net worth up/).closest("span")?.parentElement;
    expect(sr?.querySelector("[aria-hidden]")).not.toBeNull();
  });
});

describe("DemoLock (docs/47 §4, acceptance 1–2)", () => {
  it("renders the padlock for a demo session on a blocked write", () => {
    email = "demo@fintracker.local";
    const { result } = renderHook(() => useDemoWriteLock("PATCH", "/transactions/1"), {
      wrapper,
    });
    expect(result.current).toBe(true);
    render(<DemoLock />);
    expect(screen.getByTestId("demo-lock")).toBeTruthy();
  });

  it("a live tenant renders ZERO padlocks (acceptance example 1)", () => {
    email = "mark@example.com";
    const { result } = renderHook(() => useDemoWriteLock("PATCH", "/transactions/1"), {
      wrapper,
    });
    expect(result.current).toBe(false);
    // The glyph exists as a component, but nothing asks for it on a live
    // tenant: the guard's verdict IS what the page renders.
    const { container } = render(
      <span>{result.current ? <DemoLock /> : null}</span>,
    );
    expect(container.querySelector('[data-testid="demo-lock"]')).toBeNull();
  });

  it("a read or an exempt path never locks, even on demo", () => {
    email = "demo@fintracker.local";
    for (const [method, path] of [
      ["GET", "/export/zip"],
      ["POST", "/projections/run"],
      ["POST", "/auth/mcp-tokens/rotate"],
    ] as const) {
      const { result } = renderHook(() => useDemoWriteLock(method, path), { wrapper });
      expect(result.current, `${method} ${path}`).toBe(false);
    }
  });
});

describe("useDemoRefusal (docs/47 §4: one message per page, from the REAL 403)", () => {
  it("swallows exactly the demo refusal — one info toast, no other code", async () => {
    const { result } = renderHook(() => useDemoRefusal(), { wrapper });

    // A non-demo error is NOT swallowed (the caller keeps its ErrorPanel path).
    expect(result.current.onError("Edit", { code: "server_error" })).toBe(false);
    expect(toastInfo).not.toHaveBeenCalled();

    // The real 403 becomes a local, visible refusal + ONE info toast.
    expect(
      result.current.onError("Edit", { code: "demo_account_restricted" }),
    ).toBe(true);
    await waitFor(() => expect(result.current.refusal).toBe("Edit"));
    expect(toastInfo).toHaveBeenCalledTimes(1);
    expect(toastInfo).toHaveBeenCalledWith("Edit — the demo account is read-only");
  });
});
