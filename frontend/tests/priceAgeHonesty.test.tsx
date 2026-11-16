// Price-fetch provenance — two labels, two truths (Option A, t_bd5ec95f).
//
// The prices table now records a REAL provider-fetch event
// (`price_fetched_at`, stamped only at the provider-response seam), so the
// staleness surfaces state BOTH facts and NEVER conflate them:
//
//   market-date label : "Manual · 2026-10-20" / "Market data · today"
//                       — from `price_date`, DAY-ONLY (server-computed age)
//   fetch age         : " · fetched 12s ago" — from `price_fetched_at` ONLY
//
// `price_fetched_at` null/absent = NO evidence → the fetch segment is omitted
// entirely (never "0s", never "just now", never "fetch time unavailable" —
// t_a16b7663). Sub-day formatting of `price_date` would be fabrication, and a
// clock-skew-forward stamp clamps to "0s", never negative. The source guard
// at the bottom pins that every price surface feeds the formatter from
// `price_fetched_at` and from nothing else.

import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { PriceStalenessStrip } from "../src/components/investments/PriceStalenessStrip";
import { WatchRow } from "../src/pages/Investments";
import type { WatchlistItem } from "../src/api/watchlist";
import { formatFetchAge } from "../src/components/charts/format";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** The component's OWN date-label rule, re-derived here (AGENTS date-lock
 *  rule): never compare a rendered calendar date against a hard-coded literal
 *  — a machine whose TZ is ahead of UTC renders the previous day. */
const marketLabel = (priceDate: string) =>
  ` · ${new Date(`${priceDate}T00:00:00`).toLocaleDateString("en", { month: "short", day: "numeric", timeZone: "UTC" })}`;

const watchRow = (extra: Record<string, unknown> = {}): WatchlistItem =>
  ({
    security_id: 7,
    symbol: "AAPL",
    name: "Apple",
    listing_currency: "USD",
    latest_price: "100.00",
    price_date: "2026-10-20",
    price_source: "manual",
    price_age_days: 8,
    price_stale: false,
    dead_symbol: false,
    dead_symbol_reason: null,
    ...extra,
  }) as WatchlistItem;

/** Pin the clock narrowly around ONE interaction (the m95 fake-timer rule —
 *  a file-wide freeze hangs testing-library's waitFor). */
const freezeAt = (iso: string) => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(iso));
};

describe("formatFetchAge (sub-day age from price_fetched_at ONLY)", () => {
  const now = Date.parse("2026-10-28T12:00:00Z");

  it("formats seconds / minutes / hours / days by magnitude", () => {
    expect(formatFetchAge("2026-10-28T11:59:48Z", now)).toBe("12s");
    expect(formatFetchAge("2026-10-28T11:55:00Z", now)).toBe("5m");
    expect(formatFetchAge("2026-10-28T10:00:00Z", now)).toBe("2h");
    expect(formatFetchAge("2026-10-25T12:00:00Z", now)).toBe("3d");
  });

  it("returns null for missing or unparseable input (omit, never placeholder)", () => {
    expect(formatFetchAge(null, now)).toBeNull();
    expect(formatFetchAge(undefined, now)).toBeNull();
    expect(formatFetchAge("not-a-timestamp", now)).toBeNull();
  });

  it("clamps a clock-skew-forward stamp to 0s — never a negative age", () => {
    expect(formatFetchAge("2026-10-28T12:00:30Z", now)).toBe("0s");
  });
});

describe("price age honesty — two labels, two truths", () => {
  it("watchlist row without evidence shows the market date and NO fetch segment", () => {
    render(
      <MemoryRouter>
        <WatchRow item={watchRow()} onRemove={() => {}} />
      </MemoryRouter>,
    );
    const row = screen.getByTestId("watch-row-AAPL");
    // WatchRow's badge renders the raw ISO price_date (not the strip's
    // "Oct 20" short form) — assert exactly that shape, no fetch claim.
    expect(row.textContent).toContain("Manual · 2026-10-20");
    expect(row.textContent).not.toMatch(/fetched/i);
    // And never the banned placeholder copy either (t_a16b7663).
    expect(row.textContent).not.toMatch(/0s|just now|unavailable/i);
  });

  it("watchlist row with an explicit null evidence field omits the segment too", () => {
    // The server emits the key as null when no fetch ever stamped the row —
    // null and absent mean the same thing: no evidence, no segment.
    render(
      <MemoryRouter>
        <WatchRow item={watchRow({ price_fetched_at: null })} onRemove={() => {}} />
      </MemoryRouter>,
    );
    const row = screen.getByTestId("watch-row-AAPL");
    expect(row.textContent).toContain("Manual · 2026-10-20");
    expect(row.textContent).not.toMatch(/fetched/i);
  });

  it("watchlist row WITH real evidence states both truths", () => {
    freezeAt("2026-10-28T12:00:00Z");
    render(
      <MemoryRouter>
        <WatchRow
          item={watchRow({ price_fetched_at: "2026-10-28T10:00:00Z" })}
          onRemove={() => {}}
        />
      </MemoryRouter>,
    );
    const row = screen.getByTestId("watch-row-AAPL");
    expect(row.textContent).toContain("Manual · 2026-10-20");
    expect(row.textContent).toContain("fetched 2h ago");
  });

  it("strip as-of list states source · today with no fetch claim (no evidence)", () => {
    freezeAt("2026-10-28T12:00:00Z");
    render(
      <PriceStalenessStrip
        rows={[
          {
            label: "AAPL",
            price_date: "2026-10-28",
            price_source: "yfinance",
            price_age_days: 0,
            price_stale: true,
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("stale-strip-asof-toggle"));
    expect(screen.getByText("Market data · today")).toBeTruthy();
    expect(screen.queryByText(/fetched/i)).toBeNull();
  });

  it("strip as-of list appends the fetch age only when the row carries evidence", () => {
    freezeAt("2026-10-28T12:00:00Z");
    render(
      <PriceStalenessStrip
        rows={[
          {
            label: "AAPL",
            price_date: "2026-10-20",
            price_source: "manual",
            price_age_days: 8,
            price_fetched_at: "2026-10-28T11:59:48Z",
            price_stale: true,
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("stale-strip-asof-toggle"));
    expect(
      screen.getByText(`Manual${marketLabel("2026-10-20")} · fetched 12s ago`),
    ).toBeTruthy();
  });

  it("strip as-of list says 'no price date' for an unpriced row, still no fetch claim", () => {
    render(
      <PriceStalenessStrip
        rows={[
          {
            label: "GONE",
            price_date: null,
            price_source: null,
            price_age_days: null,
            price_stale: true,
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("stale-strip-asof-toggle"));
    expect(screen.getByText("Price · no price date")).toBeTruthy();
    expect(screen.queryByText(/fetched/i)).toBeNull();
  });

  it("sub-day precision comes ONLY from price_fetched_at, never from price_date", () => {
    freezeAt("2026-10-28T12:00:00Z");
    // price_date is TODAY (age 0) but there is NO fetch evidence: the row
    // must show the market-date truth and NO sub-day unit at all — a "12s"
    // derived from a date field would be fabrication.
    render(
      <PriceStalenessStrip
        rows={[
          {
            label: "TODAY",
            price_date: "2026-10-28",
            price_source: "yfinance",
            price_age_days: 0,
            price_fetched_at: null,
            price_stale: true,
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("stale-strip-asof-toggle"));
    expect(screen.getByText("Market data · today")).toBeTruthy();
    expect(screen.queryByText(/fetched/i)).toBeNull();
    expect(screen.queryByText(/\d+[smhd]/)).toBeNull();
  });

  it("a clock-skew-forward stamp clamps — never a negative age", () => {
    freezeAt("2026-10-28T12:00:00Z");
    render(
      <PriceStalenessStrip
        rows={[
          {
            label: "SKEW",
            price_date: "2026-10-28",
            price_source: "yfinance",
            price_age_days: 0,
            price_fetched_at: "2026-10-28T12:00:30Z",
            price_stale: true,
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("stale-strip-asof-toggle"));
    expect(screen.getByText("Market data · today · fetched 0s ago")).toBeTruthy();
    // The clamp means a future stamp can never render a negative age.
    expect(screen.queryByText(/fetched -\d/)).toBeNull();
  });

  it("source guard: every price surface feeds the formatter from price_fetched_at only", () => {
    // Drift guard, inverted from t_29881f89's removal pin: the fetch-age
    // claim is now ALLOWED and must be sourced correctly on every surface.
    // Each file must reference `price_fetched_at`, and no file may ever pass
    // a market-date field into the sub-day formatter.
    const files = [
      "src/components/investments/PriceStalenessStrip.tsx",
      "src/pages/Investments.tsx",
      "src/api/portfolio.ts",
      "src/api/watchlist.ts",
    ];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      expect(source, `${file} must consume price_fetched_at`).toMatch(/price_fetched_at/);
      expect(
        source,
        `${file} must never format a market-date field as a fetch age`,
      ).not.toMatch(/formatFetchAge\(\s*(item|row|position|quote|group)\.price_date/);
    }
    // The formatter itself lives in the shared format module, fed only by
    // the provenance field at every call site.
    const strip = readFileSync("src/components/investments/PriceStalenessStrip.tsx", "utf8");
    expect(strip).toMatch(/formatFetchAge\(\s*row\.price_fetched_at/);
    const page = readFileSync("src/pages/Investments.tsx", "utf8");
    expect(page).toMatch(/formatFetchAge\(\s*position\.price_fetched_at/);
    expect(page).toMatch(/formatFetchAge\(\s*quote\.price_fetched_at/);
    expect(page).toMatch(/formatFetchAge\(\s*item\.price_fetched_at/);
  });
});
