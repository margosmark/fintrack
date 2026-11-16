// Test infra (FTestInfra): jsdom is the default environment for component/hook
// tests; per-file `// @vitest-environment node` overrides are used for pure
// modules that need Node's webcrypto globals. No overlap with vite.config.ts
// (dev proxy) — vitest reuses the same React app sources without the proxy.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    globals: false,
    // Both extensions: component/hook tests are authored as .tsx (they contain
    // JSX), so a `*.test.ts`-only glob silently skipped them — a test file can
    // sit in tests/ passing nothing and nobody notices.
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["./tests/setup.ts"],
    // Node >= 22 defines a BROKEN global `localStorage` (an accessor returning
    // undefined unless `--localstorage-file` is passed), and vitest will not
    // shadow a global the runtime already defines — so jsdom's working
    // `localStorage` is dropped and every storage test fails with
    // "Cannot read properties of undefined". Deleting Node's global before the
    // jsdom environment installs restores it. See the header of
    // tests/node-bootstrap.cjs for the full explanation. Harmless on Node 22,
    // where the property does not exist.
    poolOptions: {
      forks: {
        execArgv: ["--require", new URL("./tests/node-bootstrap.cjs", import.meta.url).pathname],
      },
    },
    // Whole-test budget. The vitest default (5000ms) is wall-clock and gets
    // eaten by CPU contention when the suite shares a box with a concurrent
    // build/lint/backend run (observed on splitTransactions' splitGrouping):
    // the test progresses, it just takes longer than 5s. A DEAD query still
    // fails fast — per-query waits are separately capped at 5s via
    // asyncUtilTimeout in tests/setup.ts.
    testTimeout: 15000,
  },
});
