import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const { restoreSession } = vi.hoisted(() => ({
  restoreSession: vi.fn<() => Promise<import("../src/types").AuthResponse | null>>(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession };
});

import { usePriceStream } from "../src/hooks/usePriceStream";

const authResponse = {
  access_token: "refreshed",
  refresh_token: "unused",
  token_type: "bearer" as const,
  user: {
    id: "1",
    email: "demo@example.com",
    privacy_mode: false,
    currency_tracker_enabled: true,
    llm_provider: "none",
    llm_base_url: null,
    llm_model: "",
    encryption_required: false,
  },
};

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  close(): void {}
  emit(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
  fail(): void {
    this.onerror?.(new Event("error"));
  }
}

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  }
  return { Wrapper, client };
}

describe("usePriceStream", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    restoreSession.mockReset().mockResolvedValue(null);
    vi.useFakeTimers();
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
      clear: () => store.clear(),
    });
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("preserves cached-price freshness when applying an SSE price update", () => {
    const securityId = 7;
    const oldOverview = {
      security: {
        id: securityId,
        symbol: "RY.TO",
        name: "Royal Bank",
        kind: "stock",
        listing_currency: "CAD",
        exchange: "TSX",
      },
      latest: {
        date: "2026-09-01",
        price: "100.00",
        source: "yfinance",
        age_days: 22,
        stale: true,
        // M129: the day-Δ baseline rides the quote patch untouched.
        previous_close: "99.00",
        day_change_native: "1.00",
        day_change_pct: 1.01,
      },
      stats: { high_52w: "110", low_52w: "90", change_pct: 1, cached_days: 2 },
      history: [],
      positions: [],
      totals: { quantity: "0", book_cost_cad: null, market_value_cad: null },
      watchlisted: false,
      fundamentals: null,
      holdings: null,
      recent_trades: [],
    };
    const { Wrapper, client } = makeWrapper();
    client.setQueryData(["security-overview", securityId], oldOverview);
    renderHook(() => usePriceStream([securityId]), { wrapper: Wrapper });

    const stream = FakeEventSource.instances[FakeEventSource.instances.length - 1];
    expect(stream).toBeDefined();
    act(() => {
      stream!.emit([{
        security_id: securityId,
        symbol: "RY.TO",
        price: "101.00",
        date: "2026-09-22",
        source: "yfinance",
        age_days: 1,
        stale: false,
      }]);
    });

    const updated = client.getQueryData<typeof oldOverview>(["security-overview", securityId]);
    expect(updated?.latest).toEqual({
      date: "2026-09-22",
      price: "101.00",
      source: "yfinance",
      age_days: 1,
      stale: false,
      // The baseline moved with the quote patch — a tick changes the price,
      // never the session it is measured against (M129).
      previous_close: "99.00",
      day_change_native: "1.00",
      day_change_pct: 1.01,
    });
  });

  it("creates a latest quote when the overview previously had no price", () => {
    const securityId = 8;
    const oldOverview = {
      security: {
        id: securityId,
        symbol: "NEW.TO",
        name: "New Security",
        kind: "stock",
        listing_currency: "CAD",
        exchange: "TSX",
      },
      latest: null,
      stats: { high_52w: null, low_52w: null, change_pct: null, cached_days: 0 },
      history: [],
      positions: [],
      totals: { quantity: "0", book_cost_cad: null, market_value_cad: null },
      watchlisted: false,
      fundamentals: null,
      holdings: null,
      recent_trades: [],
    };
    const { Wrapper, client } = makeWrapper();
    client.setQueryData(["security-overview", securityId], oldOverview);
    renderHook(() => usePriceStream([securityId]), { wrapper: Wrapper });

    const stream = FakeEventSource.instances[0];
    act(() => {
      stream!.emit([{
        security_id: securityId,
        symbol: "NEW.TO",
        price: "15.50",
        date: "2026-09-23",
        source: "manual",
        age_days: 0,
        stale: false,
      }]);
    });

    const updated = client.getQueryData<typeof oldOverview>(["security-overview", securityId]);
    expect(updated?.latest).toEqual({
      date: "2026-09-23",
      price: "15.50",
      source: "manual",
      age_days: 0,
      stale: false,
      // No prior session is known yet — the baseline states that honestly.
      previous_close: null,
      day_change_native: null,
      day_change_pct: null,
    });
  });

  it("patches the watchlist cache with streamed freshness", () => {
    const securityId = 7;
    const { Wrapper, client } = makeWrapper();
    client.setQueryData(["watchlist"], {
      total: 1,
      items: [{
        security_id: securityId,
        symbol: "RY.TO",
        name: "Royal Bank",
        listing_currency: "USD",
        latest_price: "100.00",
        price_source: "yfinance",
        price_age_days: 22,
        price_stale: true,
      }],
    });
    renderHook(() => usePriceStream([securityId]), { wrapper: Wrapper });

    act(() => {
      FakeEventSource.instances[0].emit([{
        security_id: securityId,
        symbol: "RY.TO",
        price: "101.00",
        date: "2026-09-23",
        source: "manual",
        age_days: 0,
        stale: false,
      }]);
    });

    const watchlist = client.getQueryData<import("../src/api/watchlist").WatchlistResponse>(["watchlist"]);
    expect(watchlist?.items[0]).toMatchObject({
      latest_price: "101.00",
      price_source: "manual",
      price_age_days: 0,
      price_stale: false,
    });
  });

  it("patches a held row's QUOTE and leaves its valuation to the query cadence", () => {
    const securityId = 7;
    const { Wrapper, client } = makeWrapper();
    const positions = {
      total: 1,
      items: [{
        account_id: 1,
        account_name: "Brokerage",
        security_id: securityId,
        symbol: "RY.TO",
        listing_currency: "USD",
        quantity: "2",
        price_date: "2026-09-01",
        price: "100.00",
        price_source: "yfinance",
        price_age_days: 22,
        price_stale: true,
        book_cost_cad: "260.00",
        spark: [],
        market_value_native: "200.00",
        market_value_cad: "260.00",
      }],
    };
    client.setQueryData(["positions", undefined], positions);
    renderHook(() => usePriceStream([securityId]), { wrapper: Wrapper });

    act(() => {
      FakeEventSource.instances[0].emit([{
        security_id: securityId,
        symbol: "RY.TO",
        price: "101.00",
        date: "2026-09-23",
        source: "manual",
        age_days: 0,
        stale: false,
      }]);
    });

    // M126 L1 (docs/18 §Frontend): the QUOTE is patched client-side so a tick
    // is visible at once — and NOTHING else about the row moves, because the
    // market value / book cost are Decimal valuation only the server owns.
    const patched = client.getQueryData<Record<string, unknown>>(["positions", undefined]) as {
      items: Record<string, unknown>[];
      livePatchedAt?: number;
    };
    expect(patched).not.toBe(positions);
    expect(patched.items[0]).toMatchObject({
      price: "101.00",
      price_date: "2026-09-23",
      price_source: "manual",
      price_age_days: 0,
      price_stale: false,
      // server-owned, byte-identical:
      quantity: "2",
      book_cost_cad: "260.00",
      market_value_native: "200.00",
      market_value_cad: "260.00",
    });
    // The marker is what lets the table say "totals update on next refresh";
    // the next SERVER payload replaces this object and drops it.
    expect(patched.livePatchedAt).toEqual(expect.any(Number));
    // The original payload object is untouched (no in-place mutation).
    expect(positions.items[0].price).toBe("100.00");
  });

  it("invalidates positions/exposure only when the payload actually changes", () => {
    const securityId = 7;
    const { Wrapper, client } = makeWrapper();
    client.setQueryData(["positions", undefined], { total: 0, items: [] });
    client.setQueryData(["exposure", undefined], { total: 0, items: [] });
    renderHook(() => usePriceStream([securityId]), { wrapper: Wrapper });
    const stream = FakeEventSource.instances[0];
    const payload = [{
      security_id: securityId,
      symbol: "RY.TO",
      price: "101.00",
      date: "2026-09-23",
      source: "manual" as const,
      age_days: 0,
      stale: false,
    }];

    // First tick of a connection only records the baseline: mounting a page
    // must not trigger a refetch storm.
    act(() => stream!.emit(payload));
    expect(client.getQueryState(["positions", undefined])?.isInvalidated).toBe(false);

    // The stream repeats an identical payload every 5s — still no invalidation.
    act(() => stream!.emit(payload));
    expect(client.getQueryState(["positions", undefined])?.isInvalidated).toBe(false);
    expect(client.getQueryState(["exposure", undefined])?.isInvalidated).toBe(false);

    // A quote that actually moved means the server's positions are stale.
    act(() => stream!.emit([{ ...payload[0], date: "2026-09-24" }]));
    expect(client.getQueryState(["positions", undefined])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["exposure", undefined])?.isInvalidated).toBe(true);
  });

  it("waits for session restoration before scheduling the reconnect backoff", async () => {
    let finishRefresh: () => void = () => undefined;
    restoreSession.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRefresh = () => resolve(authResponse);
        }),
    );
    const { Wrapper } = makeWrapper();
    renderHook(() => usePriceStream([9]), { wrapper: Wrapper });

    const first = FakeEventSource.instances[0];
    act(() => first!.fail());

    expect(restoreSession).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.instances).toHaveLength(1);

    finishRefresh();
    await act(async () => {
      await Promise.resolve();
    });
    expect(FakeEventSource.instances).toHaveLength(1);

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("stops reconnect attempts when session restoration fails", async () => {
    restoreSession.mockResolvedValueOnce(null);
    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => usePriceStream([9]), { wrapper: Wrapper });

    act(() => FakeEventSource.instances[0].fail());
    await act(async () => {
      await Promise.resolve();
    });

    act(() => vi.advanceTimersByTime(3000));
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(result.current.status).toBe("disconnected");
  });

  it("does not schedule a reconnect after the hook is disposed", async () => {
    let finishRefresh: (session: typeof authResponse | null) => void = () => undefined;
    restoreSession.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRefresh = resolve;
        }),
    );
    const { Wrapper } = makeWrapper();
    const { unmount } = renderHook(() => usePriceStream([9]), { wrapper: Wrapper });

    const first = FakeEventSource.instances[0];
    act(() => first.fail());
    unmount();

    finishRefresh(authResponse);
    await act(async () => {
      await Promise.resolve();
    });
    act(() => vi.advanceTimersByTime(3000));

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not reconnect when session restoration reports privacy mode", async () => {
    restoreSession.mockResolvedValueOnce({
      ...authResponse,
      user: { ...authResponse.user, privacy_mode: true },
    });
    const { Wrapper } = makeWrapper();
    const { result } = renderHook(() => usePriceStream([9]), { wrapper: Wrapper });

    act(() => FakeEventSource.instances[0].fail());
    await act(async () => {
      await Promise.resolve();
    });
    act(() => vi.advanceTimersByTime(3000));

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(result.current.status).toBe("disconnected");
  });

  it("ignores late callbacks from a superseded EventSource", () => {
    const { Wrapper } = makeWrapper();
    const { rerender, result } = renderHook(
      ({ ids }: { ids: number[] }) => usePriceStream(ids),
      { wrapper: Wrapper, initialProps: { ids: [1] } },
    );
    const stale = FakeEventSource.instances[0];

    rerender({ ids: [2] });
    const current = FakeEventSource.instances[1];
    act(() => stale.fail());
    act(() => stale.emit([{
      security_id: 1,
      symbol: "OLD",
      price: "1",
      date: "2026-09-23",
      source: "manual",
      age_days: 0,
      stale: false,
    }]));

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(restoreSession).not.toHaveBeenCalled();
    expect(result.current.lastEventAt).toBeNull();
    expect(current).toBeDefined();
  });

  it("does not create a stream in privacy mode", () => {
    const { Wrapper } = makeWrapper();
    const { result } = renderHook(
      () => usePriceStream([9], { privacyMode: true }),
      { wrapper: Wrapper },
    );

    expect(FakeEventSource.instances).toHaveLength(0);
    expect(restoreSession).not.toHaveBeenCalled();
    expect(result.current.status).toBe("disconnected");
  });

  it("does not mutate the caller's securityIds array when building the memo key", () => {
    const securityIds = [3, 1, 2];
    const { Wrapper } = makeWrapper();
    renderHook(() => usePriceStream(securityIds), { wrapper: Wrapper });
    // The previous implementation used `securityIds.sort()`, which mutated the
    // prop in place (turning [3,1,2] into [1,2,3]). The fix copies first.
    expect(securityIds).toEqual([3, 1, 2]);
  });
});
