// M58b renderer-idle contract (docs/09 Accessibility "Audit method", channel C
// companion): the keyboard walkthrough could only reach 2 of the 6 audited
// pages because a runaway re-render loop pinned the renderer — every Tab
// queued behind ~1,800 renders/second and later CDP calls timed out.
//
// The loop was an identity bug, not a rendering bug: `useSearchRows` handed
// `useResolvedTransactions` a fresh `[]` on every render while its query is
// disabled (the normal state — the query only enables when the search popover
// opens), and that hook's resolve effect keys on `items` identity and answers
// with `setResolved(new Map())`. render → effect → setState → render, forever.
//
// These tests pin the settled behaviour at the hook boundary: the hook renders
// a bounded number of times and its rows array keeps its identity across
// re-renders. With the per-render `[]` the render counter runs away instead.

import { afterEach, describe, expect, it } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useSearchRows } from "../src/components/e2ee/useSearchRows";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let renders = 0;
let lastRows: unknown;

/** Renders the hook the way AppShell does: disabled, so `query.data` is undefined. */
function Probe() {
  const { rows } = useSearchRows(false);
  renders += 1;
  lastRows = rows;
  return <div data-testid="rows">{rows.length}</div>;
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Probe />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  renders = 0;
  lastRows = undefined;
});

describe("useSearchRows (disabled query) — renderer settles", () => {
  it("stops rendering instead of looping once the resolve effect has run", async () => {
    const view = mount();
    await act(async () => {
      await sleep(300);
    });

    // Mount + the resolve effect's `setResolved(new Map())` settle well under
    // this. The defect rendered ~1,800×/s, so any regression lands far above it.
    expect(renders).toBeLessThan(6);
    expect(screen.getByTestId("rows").textContent).toBe("0");

    const before = renders;
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <Probe />
      </QueryClientProvider>,
    );
    await act(async () => {
      await sleep(120);
    });
    expect(renders - before).toBeLessThan(4);
  });

  it("keeps the rows array identical across re-renders (effect deps must be stable)", async () => {
    const view = mount();
    await act(async () => {
      await sleep(120);
    });
    const first = lastRows;
    expect(first).toBeDefined();

    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <Probe />
      </QueryClientProvider>,
    );
    await act(async () => {
      await sleep(120);
    });
    expect(lastRows).toBe(first);
  });
});
