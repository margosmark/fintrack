// M185 ledger-ui-build — the new shell surfaces (docs/47 §1.1–1.3).
//
// TruthBar: every segment is bounded by the payload (§1.3) — privacy always,
// demo banner with the exact copy and role=status, the range only when the
// page published one, and NO vendor string anywhere (the market-data vendor
// renders as "Market data"; `yfinance` never appears in this bar).
// MobileTabBar: five tabs with the centre `Add` as a REAL control (§1.2).
// MorePanel: focus-in on open / return on close, real links only (§1.1).

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Account } from "../../src/api/accounts";
import { TruthBar } from "../../src/components/shell/TruthBar";
import { MobileTabBar } from "../../src/components/shell/MobileTabBar";
import { MorePanel } from "../../src/components/shell/MorePanel";
import { PageMetaProvider, usePublishPageMeta } from "../../src/lib/pageMeta";

let email = "demo@fintracker.local";

vi.mock("../../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: email === null ? null : { email } }),
}));
vi.mock("../../src/api/fx", () => ({
  getFxStatus: vi.fn().mockResolvedValue({
    reporting_currency: "CAD",
    as_of: "2026-10-14",
    age_days: 1,
    stale: false,
    pairs: [],
  }),
}));

function renderTruthBar() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <PageMetaProvider>
          <TruthBar />
        </PageMetaProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A page that publishes its own range, like Dashboard/Transactions do. */
function RangePublisher() {
  usePublishPageMeta({ range: { from: "2026-10-01", to: "2026-10-13" } });
  return <TruthBar />;
}

beforeEach(() => {
  email = "demo@fintracker.local";
});
afterEach(() => {
  email = "demo@fintracker.local";
});

describe("TruthBar (docs/47 §1.3)", () => {
  it("always shows privacy, and the demo banner with the exact copy + status role", () => {
    renderTruthBar();
    // The demo tenant boots masked (docs/46), so the segment reads masked.
    expect(screen.getByTestId("truth-bar").textContent).toContain("Amounts");
    const banner = screen.getByTestId("demo-banner");
    expect(banner.getAttribute("role")).toBe("status");
    expect(banner.textContent).toBe("Read-only demo — changes are not saved");
  });

  it("a live tenant never renders the demo banner", () => {
    email = "mark@example.com";
    renderTruthBar();
    expect(screen.queryByTestId("demo-banner")).toBeNull();
  });

  it("prints NO vendor name — the bar is payload-bounded", () => {
    renderTruthBar();
    const text = screen.getByTestId("truth-bar").textContent ?? "";
    // Forbidden by docs/47 §1.3, even though the FX/price caches use them.
    expect(text).not.toContain("yfinance");
    expect(text).not.toContain("BoC");
    expect(text).not.toContain("cache");
  });

  it("renders no range segment until a page publishes one", () => {
    const { container } = renderTruthBar();
    expect(container.textContent).not.toContain("2026");
  });

  it("renders the page's OWN range once published (payload dates only)", () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <PageMetaProvider>
            <RangePublisher />
          </PageMetaProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // Oct 1 – Oct 13, 2026 — derived from the two published ISO dates only.
    expect(screen.getByTestId("truth-bar").textContent).toContain("Oct 1 – Oct 13, 2026");
  });
});

describe("MobileTabBar (docs/47 §1.2)", () => {
  function renderBar(props: Partial<Parameters<typeof MobileTabBar>[0]> = {}) {
    return render(
      <MemoryRouter initialEntries={["/dashboard"]}>
        <MobileTabBar onAdd={() => {}} onMore={() => {}} moreOpen={false} {...props} />
      </MemoryRouter>,
    );
  }

  it("is five tabs in order — Home · Activity · Add · Portfolio · More", () => {
    renderBar();
    const nav = screen.getByTestId("mobile-tab-bar");
    const children = Array.from(nav.children);
    expect(children).toHaveLength(5);
    expect(children[0].textContent).toContain("Home");
    expect(children[1].textContent).toContain("Activity");
    expect(children[2].textContent).toContain("Add");
    expect(children[3].textContent).toContain("Portfolio");
    expect(children[4].textContent).toContain("More");
    // Six-tab legacy is gone (the spec change is §1.2 of docs/47).
    expect(nav.textContent).not.toContain("Settings");
    expect(nav.textContent).not.toContain("Reports");
  });

  it("Add is a REAL control — it fires the shell's callback, never a no-op", () => {
    const onAdd = vi.fn();
    renderBar({ onAdd });
    const add = screen.getByTestId("tab-add");
    fireEvent.click(add);
    expect(onAdd).toHaveBeenCalledTimes(1);
    expect(add.getAttribute("aria-label")).toBe("Add transaction");
  });

  it("holds the M82 pixel constants and the iOS gesture bar", () => {
    renderBar();
    const nav = screen.getByTestId("mobile-tab-bar");
    expect(nav.className).toContain("pb-[env(safe-area-inset-bottom)]");
    for (const child of Array.from(nav.children)) {
      expect(child.className).toContain("min-h-[44px]");
      expect(child.className).toContain("min-w-[44px]");
    }
  });
});

describe("MorePanel (docs/47 §1.1)", () => {
  const accounts: Account[] = [
    {
      id: 1,
      institution_id: null,
      name: "Everyday chequing",
      type: "chequing",
      currency: "CAD",
      taxable: false,
      llm_autopilot: false,
      cash_balance: "1250.00",
      investment_value: "0.00",
      total_value: "1250.00",
      starting_balance: "0.00",
      name_cipher: null,
    } as Account,
  ];

  function openPanel() {
    const onClose = vi.fn();
    const anchor = document.createElement("button");
    const utils = render(
      <MemoryRouter>
        <MorePanel
          open
          onClose={onClose}
          accounts={accounts}
          toolsEnabled
          anchorRef={{ current: anchor }}
        />
      </MemoryRouter>,
    );
    return { ...utils, onClose };
  }

  it("moves focus into the panel on open and returns it to the anchor on close", async () => {
    const { rerender, onClose } = openPanel();
    const panel = screen.getByTestId("more-panel");
    await waitFor(() => expect(document.activeElement).toBe(panel));

    // Tab is NOT trapped: the panel is a popover of links, not a dialog.
    expect(panel.getAttribute("tabindex")).toBe("-1");

    rerender(
      <MemoryRouter>
        <MorePanel
          open={false}
          onClose={onClose}
          accounts={accounts}
          toolsEnabled
          anchorRef={{ current: document.createElement("button") }}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("more-panel")).toBeNull();
  });

  it("renders only real hrefs — no placeholder links, no dead fragments", () => {
    openPanel();
    const panel = screen.getByTestId("more-panel");
    const links = Array.from(panel.querySelectorAll("a"));
    expect(links.length).toBeGreaterThanOrEqual(15);
    for (const link of links) {
      const href = link.getAttribute("href") ?? "";
      expect(href.startsWith("/")).toBe(true);
      expect(href).not.toContain("#");
    }
  });

  it("gates the Tools group on the currency tracker (the rail's rule, kept)", () => {
    render(
      <MemoryRouter>
        <MorePanel
          open
          onClose={() => {}}
          accounts={[]}
          toolsEnabled={false}
          anchorRef={{ current: document.createElement("button") }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("Primary")).toBeTruthy();
    expect(screen.queryByText("Tools")).toBeNull();
  });
});
