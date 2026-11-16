// postAuthRedirect — the ONE place that decides where a session lands after a
// successful sign-in (M68, docs/09 "App shell & auth flow").
//
// `RequireAuth` (App.tsx) stashes the path the visitor originally asked for on
// the `/login` navigation state; the password form AND the "Explore the demo"
// button both resolve it through this helper, so a deep link requested while
// logged out survives the trip through the login screen instead of being
// discarded for `/dashboard`.

/** Shape `RequireAuth` puts on `Navigate state` when it bails to `/login`. */
export interface PostAuthRedirectState {
  /** `pathname + search` of the requested page, same-document only. */
  from?: string;
}

/** Where to go once a session exists (docs/09). */
export const DEFAULT_POST_AUTH_PATH = "/dashboard";

/**
 * Resolve the post-auth destination from the login route's navigation state.
 *
 * The value is produced by our own `<Navigate state>`, but it is still
 * re-validated here rather than trusted: only same-document paths are
 * accepted (leading `/`, not protocol-relative `//host`, no URL scheme), so a
 * hand-crafted state can never bounce a fresh session to another origin.
 * Anything else falls back to the dashboard — the pre-M68 behaviour.
 */
export function postAuthRedirect(state: unknown): string {
  const from = (state as PostAuthRedirectState | null | undefined)?.from;
  if (
    typeof from === "string" &&
    from.startsWith("/") &&
    !from.startsWith("//") &&
    !/^[a-z][a-z0-9+.-]*:/i.test(from)
  ) {
    return from;
  }
  return DEFAULT_POST_AUTH_PATH;
}
