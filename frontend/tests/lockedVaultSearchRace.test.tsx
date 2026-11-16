import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  unlocked: true,
  blindIndexTokenForQuery: vi.fn(),
  searchGlobal: vi.fn(),
}));

vi.mock("../src/lib/e2ee", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/e2ee")>()),
  blindIndexTokenForQuery: mocks.blindIndexTokenForQuery,
}));
vi.mock("../src/api/search", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/search")>()),
  searchGlobal: mocks.searchGlobal,
}));
vi.mock("../src/components/e2ee/useUnlock", () => ({
  useVaultUnlocked: () => mocks.unlocked,
}));

import { CommandPalette } from "../src/components/layout/CommandPalette";
import { lockVault } from "../src/lib/vault";

const sensitiveResults = {
  transactions: [{ id: 871, normalized_payee: "SECRET ENCRYPTED PAYEE", category_name: null }],
  categories: [],
  accounts: [],
  securities: [],
  tags: [],
  withheld: 0,
};
const emptyResults = {
  transactions: [],
  categories: [],
  accounts: [],
  securities: [],
  tags: [],
  withheld: 0,
};

function client() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: 0 } },
  });
}

function palette(queryClient: QueryClient) {
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CommandPalette onClose={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function searchForMerchant() {
  act(() => {
    fireEvent.change(screen.getByTestId("command-palette-input"), {
      target: { value: "merchant" },
    });
  });
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  });
}

async function lockAndRerender(view: ReturnType<typeof render>, queryClient: QueryClient) {
  mocks.unlocked = false;
  act(() => {
    lockVault();
    view.rerender(palette(queryClient));
  });
}

describe("locked-vault command-palette search races", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.unlocked = true;
    lockVault();
    mocks.blindIndexTokenForQuery.mockResolvedValue("blind-index-token");
  });

  it("does not issue a stale token request when the vault locks during token creation", async () => {
    let resolveToken!: (token: string) => void;
    mocks.blindIndexTokenForQuery.mockReturnValue(
      new Promise<string>((resolve) => {
        resolveToken = resolve;
      }),
    );
    mocks.searchGlobal.mockResolvedValue(emptyResults);

    const queryClient = client();
    const view = render(palette(queryClient));
    await searchForMerchant();
    await waitFor(() => expect(mocks.blindIndexTokenForQuery).toHaveBeenCalledWith("merchant"));

    await lockAndRerender(view, queryClient);
    await waitFor(() => expect(mocks.searchGlobal).toHaveBeenCalled());
    await act(async () => {
      resolveToken("blind-index-token");
      await Promise.resolve();
    });

    expect(mocks.searchGlobal.mock.calls.length).toBeGreaterThan(0);
    expect(mocks.blindIndexTokenForQuery).toHaveBeenCalledTimes(1);
    expect(mocks.searchGlobal.mock.calls.every((call) => call[2] === true && call[1] === undefined)).toBe(true);
    expect(screen.queryByText("SECRET ENCRYPTED PAYEE")).toBeNull();
    expect(queryClient.getQueryData(["search", "merchant", true])).toBeUndefined();
    expect(queryClient.getQueryData(["search", "merchant", false])).toEqual(emptyResults);
  });

  it("never renders or caches an in-flight unlocked response that arrives after locking", async () => {
    let resolveUnlockedSearch!: (result: typeof sensitiveResults) => void;
    mocks.searchGlobal.mockImplementation((_query: string, _token: string | undefined, hide: boolean) => {
      if (hide) return Promise.resolve(emptyResults);
      return new Promise<typeof sensitiveResults>((resolve) => {
        resolveUnlockedSearch = resolve;
      });
    });

    const queryClient = client();
    const view = render(palette(queryClient));
    await searchForMerchant();
    await waitFor(() => expect(mocks.searchGlobal).toHaveBeenCalledTimes(1));
    expect(mocks.searchGlobal).toHaveBeenLastCalledWith("merchant", "blind-index-token", false, 10);

    await lockAndRerender(view, queryClient);
    await waitFor(() => expect(mocks.searchGlobal).toHaveBeenCalledTimes(2));
    expect(mocks.searchGlobal).toHaveBeenLastCalledWith("merchant", undefined, true, 10);

    await act(async () => {
      resolveUnlockedSearch(sensitiveResults);
      await Promise.resolve();
    });

    expect(screen.queryByText("SECRET ENCRYPTED PAYEE")).toBeNull();
    expect(queryClient.getQueryData(["search", "merchant", true])).toBeUndefined();
    expect(queryClient.getQueryData(["search", "merchant", false])).toEqual(emptyResults);
  });

  it("does not restore sensitive results after navigation/remount or a fresh reload client", async () => {
    mocks.searchGlobal.mockResolvedValueOnce(sensitiveResults).mockResolvedValue(emptyResults);

    const queryClient = client();
    const firstView = render(palette(queryClient));
    await searchForMerchant();
    expect(await screen.findByText("SECRET ENCRYPTED PAYEE")).toBeTruthy();
    expect(queryClient.getQueryData(["search", "merchant", true])).toEqual(sensitiveResults);

    await lockAndRerender(firstView, queryClient);
    await waitFor(() => expect(mocks.searchGlobal).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("SECRET ENCRYPTED PAYEE")).toBeNull();
    expect(queryClient.getQueryData(["search", "merchant", true])).toBeUndefined();
    expect(mocks.searchGlobal).toHaveBeenLastCalledWith("merchant", undefined, true, 10);

    firstView.unmount();
    const navigationView = render(palette(queryClient));
    await searchForMerchant();
    await waitFor(() => expect(mocks.searchGlobal).toHaveBeenCalledTimes(3));
    expect(mocks.searchGlobal).toHaveBeenLastCalledWith("merchant", undefined, true, 10);
    expect(screen.queryByText("SECRET ENCRYPTED PAYEE")).toBeNull();

    navigationView.unmount();
    const reloadClient = client();
    expect(reloadClient.getQueryCache().getAll()).toHaveLength(0);
    const reloadView = render(palette(reloadClient));
    await searchForMerchant();
    await waitFor(() => expect(mocks.searchGlobal).toHaveBeenCalledTimes(4));
    expect(mocks.searchGlobal).toHaveBeenLastCalledWith("merchant", undefined, true, 10);
    expect(screen.queryByText("SECRET ENCRYPTED PAYEE")).toBeNull();
    expect(reloadClient.getQueryData(["search", "merchant", true])).toBeUndefined();
    reloadView.unmount();
  });
});
