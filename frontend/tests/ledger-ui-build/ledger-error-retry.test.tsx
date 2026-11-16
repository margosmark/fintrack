// M185 ledger-ui-build — F-RETRY: the ErrorPanel's `Try again` is real.
//
// Tester finding F-RETRY: docs/47 §3 / acceptance example 15 require a working
// `error-panel-retry` bound to the query's own `refetch()`, and at candidate
// `82a379a` `frontend/src` had ZERO `onRetry` call sites — the component
// carried the button and nothing in the product could ever render it. These
// tests pin the contract that closes the gap, through a REAL product surface
// (TaxAlertsPanel, `GET /reports/tax-alerts`), a real `useQuery`, and the
// shape the Settings/scheduled panels use.
//
//   1. a transient failure (5xx / no envelope) renders the panel with the
//      envelope `detail` VERBATIM and an enabled `error-panel-retry`;
//   2. one press is ONE request — no second query, no duplicate POST — and the
//      failure is answered in place: either the surface shows its in-flight
//      state (TanStack v5 clears `error` while the refetch runs, so an
//      `isError`-gated panel hands off to the surface's own loading row) or,
//      where the panel is held open by the rest of its gate, the control
//      itself goes DISABLED + `Retrying…` + announced via `role="status"`;
//   3. a retry that succeeds recovers in place; a retry that fails again keeps
//      the detail and re-arms the button;
//   4. a POLICY refusal (`demo_account_restricted`, `encryption_required`,
//      `privacy_mode_blocks_external_api`) and any deterministic 4xx render
//      the panel WITHOUT a retry — a dead control dressed as a way back is
//      exactly what docs/47 §3/§4 forbid.
//
// House matcher style: plain DOM reads, no jest-dom.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../src/api/client";
import { getTaxLossAlerts } from "../../src/api/taxAlerts";
import type { TaxAlertsResponse } from "../../src/api/taxAlerts";
import { TaxAlertsPanel } from "../../src/components/reports/TaxAlertsPanel";
import { ErrorPanel } from "../../src/components/ui/EmptyState";
import { QueryErrorPanel } from "../../src/components/ui/QueryErrorPanel";
import { isRetryableError } from "../../src/lib/retry";

vi.mock("../../src/api/taxAlerts", () => ({
  getTaxLossAlerts: vi.fn(),
}));

const taxAlerts = vi.mocked(getTaxLossAlerts);

function payload(): TaxAlertsResponse {
  return {
    alerts: [
      {
        security_id: 7,
        symbol: "AAA",
        account_id: 1,
        account_name: "TFSA",
        quantity: "100",
        book_cost_cad: "12500.00",
        market_value_cad: "11999.90",
        unrealized_loss_cad: "500.10",
        estimated_tax_benefit_cad: "75.02",
        superficial_loss_risk: false,
      },
    ],
    summary: {
      count: 1,
      total_unrealized_loss_cad: "500.10",
      total_estimated_tax_benefit_cad: "75.02",
    },
  };
}

function client() {
  // `retry: false` so a failing query settles at once — the panel is the
  // subject, not TanStack's own backoff.
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderPanel() {
  return render(
    <QueryClientProvider client={client()}>
      <TaxAlertsPanel />
    </QueryClientProvider>,
  );
}

/** A promise a test resolves by hand, so the in-flight state can be observed. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("isRetryableError (lib/retry) — a repeat must be able to change the answer", () => {
  it("retries a transient class: no envelope, 5xx, 408, 429", () => {
    // A network failure carries no envelope at all — nothing deterministic.
    expect(isRetryableError(new TypeError("Failed to fetch"))).toBe(true);
    expect(isRetryableError(new Error("NetworkError when attempting to fetch"))).toBe(true);
    expect(isRetryableError(new ApiError(500, "Internal server error", "internal_error"))).toBe(true);
    expect(isRetryableError(new ApiError(502, "LLM upstream request failed", "llm_upstream_error"))).toBe(true);
    expect(isRetryableError(new ApiError(503, "Market data unavailable", "market_data_unavailable"))).toBe(true);
    expect(isRetryableError(new ApiError(408, "Request timed out", "timeout"))).toBe(true);
    expect(isRetryableError(new ApiError(429, "Too many requests", "rate_limited"))).toBe(true);
  });

  it("never retries a policy refusal or a deterministic 4xx", () => {
    expect(
      isRetryableError(new ApiError(403, "The demo account cannot be invited", "demo_account_restricted")),
    ).toBe(false);
    expect(
      isRetryableError(new ApiError(403, "Privacy mode blocks this host", "privacy_mode_blocks_external_api")),
    ).toBe(false);
    // The E2EE write guard — a vault state, not a hiccup (docs/33).
    expect(isRetryableError(new ApiError(422, "Encryption required", "encryption_required"))).toBe(false);
    expect(isRetryableError(new ApiError(422, "Invalid date range: from > to", "invalid_date_range"))).toBe(false);
    expect(isRetryableError(new ApiError(422, "Request validation failed", "validation_error"))).toBe(false);
    expect(isRetryableError(new ApiError(404, "Not found", "not_found"))).toBe(false);
    expect(isRetryableError(new ApiError(409, "Duplicate transaction", "duplicate_transaction"))).toBe(false);
    // 401 belongs to the refresh-and-continue path (docs/09, docs/38), not to
    // a button that would just re-trigger the same rejection.
    expect(isRetryableError(new ApiError(401, "Invalid or expired access token", "invalid_token"))).toBe(false);
  });
});

describe("ErrorPanel retry state (docs/47 §3, acceptance 15)", () => {
  it("disables itself and announces while a retry is in flight", () => {
    const onRetry = vi.fn();
    render(<ErrorPanel message="Boom" onRetry={onRetry} retrying />);
    const button = screen.getByTestId<HTMLButtonElement>("error-panel-retry");
    expect(button.textContent).toBe("Retrying…");
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("aria-busy")).toBe("true");
    // Announced, not just painted — and the region exists at rest too, so the
    // text change is spoken.
    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.textContent).toBe("Retrying…");
    // A disabled control cannot fire the handler.
    fireEvent.click(button);
    expect(onRetry).not.toHaveBeenCalled();
  });

  it("restores `Try again` once the round trip settles", () => {
    const onRetry = vi.fn();
    const { rerender } = render(<ErrorPanel message="Boom" onRetry={onRetry} />);
    rerender(<ErrorPanel message="Boom" onRetry={onRetry} retrying={false} />);
    const button = screen.getByTestId<HTMLButtonElement>("error-panel-retry");
    expect(button.textContent).toBe("Try again");
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-busy")).toBeNull();
    fireEvent.click(button);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("a real surface: TaxAlertsPanel (GET /reports/tax-alerts)", () => {
  it("transient failure → ONE refetch → recovery, with the envelope detail verbatim", async () => {
    const inFlight = deferred<TaxAlertsResponse>();
    taxAlerts
      .mockRejectedValueOnce(new ApiError(503, "Market data unavailable", "market_data_unavailable"))
      .mockImplementationOnce(() => inFlight.promise);

    renderPanel();

    // (1) the failure renders with the envelope `detail` VERBATIM + a retry.
    const panel = await screen.findByTestId("error-panel");
    expect(panel.getAttribute("role")).toBe("alert");
    expect(panel.textContent).toContain("Market data unavailable");
    expect(screen.getByTestId("error-panel-retry").textContent).toBe("Try again");
    expect(taxAlerts).toHaveBeenCalledTimes(1);

    // (2) one press is ONE request against the same query.
    fireEvent.click(screen.getByTestId("error-panel-retry"));
    await waitFor(() => expect(taxAlerts).toHaveBeenCalledTimes(2));

    // The retry is in flight: TanStack v5 clears `error` for the refetch, so
    // this surface hands off to its own loading row — and with the panel gone
    // there is no second control to press, which is the structural half of
    // "never two requests for one press". No data has rendered yet.
    expect(screen.queryByTestId("error-panel")).toBeNull();
    expect(screen.queryByTestId("error-panel-retry")).toBeNull();
    expect(screen.queryByText("AAA")).toBeNull();

    // (3) the API answers → the surface recovers in place, still two calls.
    inFlight.resolve(payload());
    expect(await screen.findByText("AAA")).toBeTruthy();
    expect(screen.queryByTestId("error-panel")).toBeNull();
    expect(taxAlerts).toHaveBeenCalledTimes(2);
  });

  it("a retry that fails again keeps the detail and re-arms the button", async () => {
    taxAlerts
      .mockRejectedValueOnce(new ApiError(500, "Internal server error", "internal_error"))
      .mockRejectedValueOnce(new ApiError(500, "Internal server error", "internal_error"));

    renderPanel();

    fireEvent.click(await screen.findByTestId("error-panel-retry"));
    await waitFor(() => expect(taxAlerts).toHaveBeenCalledTimes(2));
    const panel = await screen.findByTestId("error-panel");
    expect(panel.textContent).toContain("Internal server error");
    const again = screen.getByTestId<HTMLButtonElement>("error-panel-retry");
    expect(again.textContent).toBe("Try again");
    expect(again.disabled).toBe(false);
  });

  it("a network failure (no envelope at all) still offers the way back", async () => {
    taxAlerts
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(payload());

    renderPanel();

    await screen.findByTestId("error-panel");
    fireEvent.click(screen.getByTestId("error-panel-retry"));
    expect(await screen.findByText("AAA")).toBeTruthy();
    expect(taxAlerts).toHaveBeenCalledTimes(2);
  });

  it("a policy refusal renders the panel with NO retry (never a dead control)", async () => {
    taxAlerts.mockRejectedValueOnce(
      new ApiError(403, "The demo account cannot be invited", "demo_account_restricted"),
    );

    renderPanel();

    const panel = await screen.findByTestId("error-panel");
    expect(panel.textContent).toContain("The demo account cannot be invited");
    expect(screen.queryByTestId("error-panel-retry")).toBeNull();
  });
});

/**
 * The shape the Settings and scheduled-backup/export panels use: the panel is
 * up while `isError` OR the derived value is still null, so it STAYS on screen
 * through the refetch — which is where the button's own pending state is what
 * the user (and a screen reader) sees.
 */
function HeldHarness({ queryFn }: { queryFn: () => Promise<unknown> }) {
  const query = useQuery({ queryKey: ["held-harness"], queryFn, retry: false });
  const draft = query.data === undefined ? null : query.data;
  if (draft === null) return <QueryErrorPanel query={query} />;
  return <p data-testid="held-data">{JSON.stringify(draft)}</p>;
}

function renderHeld(queryFn: () => Promise<unknown>) {
  return render(
    <QueryClientProvider client={client()}>
      <HeldHarness queryFn={queryFn} />
    </QueryClientProvider>,
  );
}

describe("a surface that holds the panel open through the retry", () => {
  it("shows Retrying… + one request only, and recovers", async () => {
    const inFlight = deferred<unknown>();
    const queryFn = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(503, "Market data unavailable", "market_data_unavailable"))
      .mockImplementationOnce(() => inFlight.promise);

    renderHeld(queryFn);

    const button = await screen.findByTestId("error-panel-retry");
    expect(queryFn).toHaveBeenCalledTimes(1);
    fireEvent.click(button);

    // Pending state, on the control the user just pressed.
    const busy = screen.getByTestId<HTMLButtonElement>("error-panel-retry");
    await waitFor(() => expect(busy.textContent).toBe("Retrying…"));
    expect(busy.disabled).toBe(true);
    expect(busy.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByRole("status").textContent).toBe("Retrying…");
    // The envelope detail is still on screen while the answer is in flight.
    expect(screen.getByTestId("error-panel").textContent).toContain("Market data unavailable");

    // Mashing it during the flight adds NOTHING — one press, one request.
    fireEvent.click(busy);
    fireEvent.click(busy);
    fireEvent.click(busy);
    expect(queryFn).toHaveBeenCalledTimes(2);

    inFlight.resolve({ ok: true });
    await waitFor(() => expect(screen.getByTestId("held-data")).toBeTruthy());
    expect(screen.queryByTestId("error-panel")).toBeNull();
    expect(queryFn).toHaveBeenCalledTimes(2);
  });

  it("offers no retry for the E2EE write guard (a vault state, not a hiccup)", async () => {
    renderHeld(() => Promise.reject(new ApiError(422, "Encryption required", "encryption_required")));

    await screen.findByText("Encryption required");
    expect(screen.queryByTestId("error-panel-retry")).toBeNull();
  });

  it("offers no retry for a validation error — the request itself is wrong", async () => {
    renderHeld(() =>
      Promise.reject(new ApiError(422, "Invalid date range: 2026-05-01 > 2026-04-01", "invalid_date_range")),
    );

    await screen.findByText("Invalid date range: 2026-05-01 > 2026-04-01");
    expect(screen.queryByTestId("error-panel-retry")).toBeNull();
  });
});
