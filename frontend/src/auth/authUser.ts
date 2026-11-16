/**
 * The session-user scope (M136).
 *
 * A tiny context OWNED OUTSIDE `AuthContext` on purpose. Shared leaf
 * components — `components/e2ee/useUnlock` and everything that renders
 * `VaultLocked` — need exactly one fact from the session: who is signed in
 * (so they can key a vault bundle) and whether the tenant turns on the
 * `encryption_required` write guard. If they read that from `AuthContext`
 * directly, every test that mocks the auth module has to re-declare the new
 * export or the component tree throws — eighteen files for one field.
 *
 * `AuthProvider` renders this scope directly outside its own provider, so in
 * the app the value is always present. Outside a provider (the tests that
 * mock auth wholesale, and any leaf rendered in isolation) the scope is null
 * and callers degrade honestly: no session, no unlock, no write guard.
 */

import { createContext, useContext } from "react";
import type { User } from "../types";

export interface AuthUserScope {
  user: User | null;
}

export const AuthUserScopeContext = createContext<AuthUserScope | null>(null);

/** The signed-in user, or null while signed out / outside an AuthProvider. */
export function useOptionalUser(): User | null {
  return useContext(AuthUserScopeContext)?.user ?? null;
}
