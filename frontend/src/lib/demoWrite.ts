// demoWrite (M185, docs/47 §4) — the CLIENT mirror of the server's demo
// write boundary, and the one place that decides which controls may wear a
// DemoLock padlock.
//
// The artifact's 90 padlocks are a demo-TENANT fact, not a feature-availability
// signal (docs/47 §"What the mockup is, and is not"). `DemoLock` renders iff
// ALL FOUR of these hold — a live tenant renders zero padlocks in the entire
// product:
//
//   1. `isReadOnlySession(user)` (src/auth/readOnlySession.ts — the client
//      mirror of the server's boundary);
//   2. the action is a MUTATING request (POST/PUT/PATCH/DELETE) on a path not
//      in the exemption sets below;
//   3. the result cannot be produced honestly without the write (docs/09
//      §Read-only sessions #1 — a write boundary never hides a calculation);
//   4. no PM-recorded named exception applies (currently exactly one: W2,
//      "Test connection", which is disabled BEFORE dispatch and never reaches
//      this module — docs/47 §4).
//
// Reads — including every DOWNLOAD (`GET /reports/*?format=csv`,
// `GET /export/zip|full|ofx`, `GET /export/schedule`) — are demo-legal and
// NEVER lock (PM binding W1, docs/47 §4): they are reads, and `docs/11`
// already pins the server truth this mirrors.
//
// The exemption list mirrors `backend/app/api/deps.py`
// (`DEMO_MUTATION_EXEMPT_PATHS` / `DEMO_MUTATION_EXEMPT_PREFIXES`), which is
// the server's real boundary. A backend change to that file breaks loudly
// here: `tests/ledger-ui-build/ledger-folds.test.ts` READS deps.py and diffs
// the two sets (it used to assert a frozen copy, which let a backend edit
// drift under a green pin — QA finding, 2026-10-16).

/** Mutating methods — the only method class the server refuses for demo. */
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * `backend/app/api/deps.py::DEMO_MUTATION_EXEMPT_PATHS`, minus the
 * `/api/v1` prefix the frontend client paths carry. A demo session MAY
 * mutate these, so a control targeting one never wears a lock.
 */
export const DEMO_MUTATION_EXEMPT_PATHS: readonly string[] = [
  "/acb/rebuild",
  "/auth/demo/reset",
  "/auth/mcp-tokens",
  "/projections/run",
];

/**
 * `backend/app/api/deps.py::DEMO_MUTATION_EXEMPT_PREFIXES` — trailing slash
 * included, because deps.py matches the prefix with it. Dropping the slash
 * made the mirror BROADER than the server (`/auth/mcp-tokensfoo` would have
 * read exempt here and been refused there); keeping it exact is what lets the
 * pin assert set equality rather than "close enough".
 */
export const DEMO_MUTATION_EXEMPT_PREFIXES: readonly string[] = [
  "/auth/mcp-tokens/",
];

/** Strip the API prefix + trailing slash so path sets compare canonically. */
function canonicalPath(path: string): string {
  const withoutQuery = path.split("?")[0].split("#")[0];
  const withoutHost = withoutQuery.replace(/^https?:\/\/[^/]+/, "");
  return withoutHost.replace(/^\/api\/v1/, "").replace(/\/$/, "");
}

/** True when the server exempts this path from the demo write refusal. */
export function isDemoExemptPath(path: string): boolean {
  const canonical = canonicalPath(path);
  return (
    DEMO_MUTATION_EXEMPT_PATHS.includes(canonical) ||
    DEMO_MUTATION_EXEMPT_PREFIXES.some((prefix) => canonical.startsWith(prefix))
  );
}

/**
 * The ONE decision `DemoLock` needs: would the server refuse THIS request as
 * a demo write? False for every read (W1) and every exempt path.
 */
export function demoWouldRefuse(method: string, path: string): boolean {
  if (!MUTATING_METHODS.has(method.toUpperCase())) return false;
  return !isDemoExemptPath(path);
}

/**
 * Is THIS error the server's demo refusal? (403 `demo_account_restricted` —
 * `backend/app/api/deps.py::_assert_demo_mutation_allowed`.)
 *
 * The one code that must never reach an error-shaped surface. A refusal is
 * not a failure (docs/47 §4 / docs/09 §Read-only sessions #3), and because
 * `Toast` renders an error tone as `role="alert"`
 * (`components/ui/Toast.tsx`), a single unguarded `toast.error` for this code
 * is exactly the shape the security pin forbids: *a
 * `demo_account_restricted` response never renders `role="alert"`.*
 *
 * Kept here, not in `DemoLock`, because hosts that do NOT own the refusal
 * element still need it: they call this to skip their generic error path and
 * let the component that renders the refusal (the editor, the panel) own the
 * single `toast.info`.
 */
export function isDemoRefusalError(error: unknown): boolean {
  return (error as { code?: string } | null | undefined)?.code === "demo_account_restricted";
}
