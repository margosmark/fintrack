// Test-lifecycle hygiene: every React tree a test renders must be unmounted
// when that test ends.
//
// This is the precondition for a clean Vitest exit. `vitest.config.ts` runs
// with `globals: false`, and @testing-library/react only registers its
// auto-cleanup when a GLOBAL `afterEach`/`teardown` exists at import time.
// With no globals it silently registers nothing, so rendered trees, their
// effects, their framer-motion animation loops and their timers all survive
// into the next test and the post-teardown phase — which is how a green
// suite starts emitting stray state updates and hangs the process on exit.
import { describe, expect, it } from "vitest";
import { useEffect } from "react";
import { render } from "@testing-library/react";

// Incremented by the unmount cleanup of the tree rendered in the first test.
// Module scope, so the counter survives into the second test in this file.
let unmounts = 0;

function Tracked() {
  useEffect(() => {
    // A pending effect like AnimatedNumber's tween or usePriceStream's
    // EventSource: if the tree is never unmounted, this cleanup never runs.
    return () => {
      unmounts += 1;
    };
  }, []);
  return <div data-testid="tracked" />;
}

describe("test lifecycle", () => {
  it("renders a tree with a live effect", () => {
    render(<Tracked />);
    // A fresh module state, i.e. no leftover unmount from a prior test.
    expect(unmounts).toBe(0);
  });

  it("unmounts the previous test's tree before the next test starts", () => {
    // The first test's tree must have been torn down. If auto-cleanup never
    // registered, this is still 0 and the defect is back.
    expect(unmounts).toBeGreaterThan(0);
  });
});
