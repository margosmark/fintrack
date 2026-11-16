// Bootstrap for the TEST RUNNER'S Node process only — never imported by the app.
//
// WHY THIS FILE EXISTS
// --------------------
// Node 22+ ships an experimental Web Storage global: `globalThis.localStorage`
// and `globalThis.sessionStorage`. Without `--localstorage-file` Node defines
// `localStorage` as an accessor that returns **undefined** (it emits
// "ExperimentalWarning: localStorage is not available because
// --localstorage-file was not provided" on first use). It is present in
// `globalThis`, just not usable.
//
// Vitest's jsdom environment copies jsdom's window onto `globalThis`, but its
// `getWindowKeys` deliberately does not shadow anything the runtime already
// defines:
//
//     if (k in global) return keysArray.includes(k);   // vitest
//
// So Node's broken `localStorage` WINS, and jsdom's working one is dropped.
// Every test that touches `localStorage` then fails with
// `TypeError: Cannot read properties of undefined (reading 'getItem')`.
//
// This is version-dependent: CI pins Node 22, where the global does not exist,
// so the suite is green there and red on any box running Node >= 22 with the
// global enabled (Node 26 does this by default). Deleting the property BEFORE
// the environment installs makes `k in global` false, so jsdom's own
// `localStorage` is populated instead — restoring exactly the behaviour the
// tests were written against.
//
// `delete` is legal because Node defines the property as `configurable: true`.
// Only the runner's process is affected: this file is loaded via
// `--require`/`execArgv` for the test workers, and no app or browser code ever
// imports it.
try {
  delete globalThis.localStorage;
} catch {
  /* non-configurable in some runtimes — nothing to do */
}
