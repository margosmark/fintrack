// readOnlySession — the client's mirror of the server's demo write boundary.
//
// `backend/app/api/deps.py::_assert_demo_mutation_allowed` rejects every
// mutating method for `demo@fintracker.local` with 403 `demo_account_restricted`
// (reads stay open — the demo IS the front door). The UI needs the same fact
// before it fires a write it knows will bounce, so a read-only session can
// degrade to a toast or a disabled control instead of an ErrorPanel.
//
// One definition: `Settings.tsx` used to hold its own copy of the demo email
// for the same purpose (M65), so both now import this.

/** The seeded demo tenant's address (docs/09 — "Explore the demo"). */
export const DEMO_EMAIL = "demo@fintracker.local";

/** True when writes for this session can only ever come back 403. */
export function isReadOnlySession(
  user: { email: string } | null | undefined,
): boolean {
  return user?.email === DEMO_EMAIL;
}
