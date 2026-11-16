// Marks the React act environment so @testing-library/react runs hook/component
// effects inside act() without warnings (jsdom tests). Harmless for node-env
// test files.
const actEnv = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
actEnv.IS_REACT_ACT_ENVIRONMENT = true;

import { afterEach } from "vitest";
import { configure } from "@testing-library/dom";

// findBy* defaults to a 1000ms async query timeout. Under parallel suite load
// a cold first paint can exceed it (observed once on splitTransactions'
// splitGrouping while the backend suite ran concurrently; passes in isolation
// and on re-run). Raise the default — still finite, so a genuinely dead query
// fails rather than hangs.
configure({ asyncUtilTimeout: 5000 });

// Unmount every React tree at the end of each test.
//
// @testing-library/react only self-registers this cleanup when a GLOBAL
// `afterEach` exists at import time (see its dist/index.js: it checks
// `typeof afterEach === "function"`, not an import). With `globals: false` in
// vitest.config.ts no global is defined, so RTL registered NOTHING and no
// rendered tree was ever unmounted. Effects, framer-motion animation loops and
// refetch timers then outlived the test that started them, updating React state
// in a later test or after teardown — the source of the intermittent
// post-teardown errors and act() noise on otherwise-green runs. Register it
// explicitly instead of flipping on globals, which would be a much larger
// behavioural change to every test file.
afterEach(async () => {
  // node-environment files (webcrypto/indexedDB) have no document and never
  // render; importing the DOM library there would fail at module load.
  if (typeof document === "undefined") return;
  const { cleanup } = await import("@testing-library/react");
  cleanup();
});
