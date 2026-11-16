// M150 — the FX staleness chip (docs/09 §FX staleness).
//
// The load-bearing claim: an FX gap is VISIBLE. Every exact-date conversion
// carries forward up to 7 days (docs/07), so a rate book that stopped being
// fetched keeps answering — off an older rate, with nothing erroring. The chip
// is the user-visible half of the fix, and this pins all four of its states:
// current, stale, a pair with no rate at all, and the deliberate NOTHING it
// renders when the tenant holds no foreign currency (there is no FX dependency
// to report on, and a status chip is never worth breaking a page for).
//
// M151 adds the PERMANENT version of "no rate at all": DKK left the curated
// board because Bank of Canada publishes no live FXDKKCAD series, so no
// scheduled refresh will ever fill a DKK row — that missing state never
// heals, and the chip has to say so just as loudly as a transient gap.
//
// House matcher style: `.getAttribute(...)`, no jest-dom. Dates are derived
// from the clock at assertion time (AGENTS §6 — never pin a date literal).
import type { ReactElement } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Currencies } from "../src/pages/Currencies";
import { FxStalenessChip } from "../src/components/ui/FxStalenessChip";
import type { FxPairStatus, FxStatus } from "../src/api/fx";

const mocks = vi.hoisted(() => ({ getFxStatus: vi.fn(), getFxBoard: vi.fn() }));

vi.mock("../src/api/fx", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/fx")>();
  return { ...actual, getFxStatus: mocks.getFxStatus, getFxBoard: mocks.getFxBoard };
});

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
    user: { id: "1", email: "mark@example.com", currency_tracker_enabled: true },
  }),
}));

function renderUi(node: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function isoDaysAgo(days: number): string {
  const day = new Date();
  day.setDate(day.getDate() - days);
  return day.toISOString().slice(0, 10);
}

/** A pair as the API reports it: `ageDays` null means no rate at all. */
function pair(base: string, ageDays: number | null): FxPairStatus {
  return {
    base,
    quote: "CAD",
    as_of: ageDays === null ? null : isoDaysAgo(ageDays),
    age_days: ageDays,
    stale: ageDays === null || ageDays > 7,
    source: ageDays === null ? null : "boc_valet",
  };
}

function status(pairs: FxPairStatus[]): FxStatus {
  const dated = pairs.filter((p) => p.as_of !== null);
  const oldest = dated.length > 0 ? dated.reduce((a, b) => ((a.age_days ?? 0) > (b.age_days ?? 0) ? b : a)) : null;
  return {
    reporting_currency: "CAD",
    as_of: oldest?.as_of ?? null,
    age_days: oldest?.age_days ?? null,
    stale: pairs.some((p) => p.stale),
    pairs,
  };
}

beforeEach(() => {
  mocks.getFxStatus.mockReset();
  mocks.getFxBoard.mockReset();
  mocks.getFxBoard.mockResolvedValue({ currencies: [] });
});

describe("FxStalenessChip", () => {
  it("names the date and age of a current book", async () => {
    mocks.getFxStatus.mockResolvedValue(status([pair("USD", 1)]));

    renderUi(<FxStalenessChip />);

    await screen.findByText(`FX · ${isoDaysAgo(1)} · 1d`);
  });

  it("raises a pulsing danger badge once the book is past the carry-forward window", async () => {
    mocks.getFxStatus.mockResolvedValue(status([pair("USD", 9)]));

    renderUi(<FxStalenessChip />);

    const chip = await screen.findByText("FX rates stale · 9d");
    // The pulse lives on the Badge's leading dot (Badge renders it there).
    expect(chip.parentElement?.querySelector(".animate-pulse")).not.toBeNull();
  });

  it("says RATES MISSING when a pair has no row at all — the worst state", async () => {
    mocks.getFxStatus.mockResolvedValue(status([pair("USD", null)]));

    renderUi(<FxStalenessChip />);

    await screen.findByText("FX rates missing");
  });

  it("names only the missing currency when the rest of the book is fine", async () => {
    mocks.getFxStatus.mockResolvedValue(status([pair("USD", 1), pair("EUR", null)]));

    renderUi(<FxStalenessChip />);

    await screen.findByText("FX rates missing · EUR");
  });

  it("renders nothing when the tenant holds no foreign currency", async () => {
    mocks.getFxStatus.mockResolvedValue(status([]));

    const { container } = renderUi(<FxStalenessChip />);

    await waitFor(() => expect(mocks.getFxStatus).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when the status call fails — a chip never breaks a page", async () => {
    mocks.getFxStatus.mockRejectedValue(new Error("boom"));

    const { container } = renderUi(<FxStalenessChip />);

    await waitFor(() => expect(mocks.getFxStatus).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  // M151 — "the board cannot price this currency". BoC publishes no live
  // FXDKKCAD series, so DKK left the curated board (docs/07 §Curated board
  // membership) and NO refresh tick will ever fill it: a DKK holder's missing
  // state is permanent, which is exactly when a silent chip would be worst.
  it("says FX rates missing for a currency no refresh will ever fill (M151 · DKK)", async () => {
    mocks.getFxStatus.mockResolvedValue(status([pair("DKK", null)]));

    renderUi(<FxStalenessChip />);

    const chip = await screen.findByText("FX rates missing");
    // DKK is the only pair in use, so the "· NAMES" suffix has nothing left
    // to contrast against and is dropped — the bare sentence is the answer.
    expect(chip.textContent).not.toContain("·");
    expect(screen.getByTitle(/DKK\/CAD missing/)).not.toBeNull();
  });
});

describe("Currencies page", () => {
  it("carries the chip in its header, above a board it says nothing about", async () => {
    mocks.getFxStatus.mockResolvedValue(status([pair("USD", 2)]));

    renderUi(<Currencies />);

    await screen.findByText(`FX · ${isoDaysAgo(2)} · 2d`);
    await screen.findByRole("heading", { name: "Currencies" });
  });
});
