// tenantStorage (F-6, security review of the M185 Ledger candidate) — the ONE
// registry for browser-storage keys that are scopeable per tenant.
//
// Every `localStorage` / `sessionStorage` key in the app either (a) holds
// ANOTHER tenant's identifiers/UI state, (b) is global by design (theme,
// device id, the mask toggle's own session flag), or (c) is already
// per-user. This module owns the (a)→per-user conversion: keys that carry
// row identifiers (`account_id`/`security_id`/dates) or that key a UI state
// off an account id must resolve differently for two tenants sharing a
// browser, or tenant A's state silently applies to tenant B (the F-0 class
// without F-0's payload).
//
// The scope token is the user's id (stable, server-owned), falling back to
// the email and finally to a literal "signed-out" bucket for the window in
// which no user is resolved — a signed-out reader must never collide with a
// signed-in one. Contrast the already-scoped keys this mirrors:
// `Settings.tsx` `last_export_user_${userId}` and `tourState.ts`
// `${prefix}${account.id}` (docs/09 §Tour).
//
// Keys that hold NO tenant data at all stay global and are recorded as
// intentional where they live (theme `STORAGE_KEY`, `fintrack-device-id`,
// `fintrack.mask`, `fin_tracker.tour.session-started`, and
// `fintrack.exposure-expanded.v1`, whose payload is the dimension VOCABULARY
// — sector/geography/asset_class/company — never a row id; see docs/09
// §Exposure visuals).

/** Anything shaped like the auth user — the real `User` or a test double. */
export interface ScopeIdentity {
  id?: string | number | null;
  email?: string | null;
}

/**
 * The tenant-scope token for a storage key. Order matters: `id` is the
 * server's tenant identity, the email is the human-readable fallback for
 * test doubles that carry only an address, and "signed-out" keeps an
 * unresolved session out of every tenant's namespace.
 */
export function tenantScope(user: ScopeIdentity | null | undefined): string {
  if (user?.id !== undefined && user.id !== null && user.id !== "") return String(user.id);
  if (user?.email !== undefined && user.email !== null && user.email !== "")
    return user.email;
  return "signed-out";
}

/** `<base>.<scope>` — the one shape every scopeable key uses. */
export function scopedKey(base: string, user: ScopeIdentity | null | undefined): string {
  return `${base}.${tenantScope(user)}`;
}
