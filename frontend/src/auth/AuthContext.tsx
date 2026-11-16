import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import * as client from "../api/client";
import { getPasskey } from "../lib/webauthn";
import type { PasskeyRequestOptions } from "../lib/webauthn";
import {
  applyDemoMaskDefault,
  initMaskForRestoredSession,
  initMaskForSession,
} from "../lib/mask";
import { scopeCandidatesTo } from "../lib/recurringSession";
import { isReadOnlySession } from "./readOnlySession";
import type { User } from "../types";
import { AuthUserScopeContext } from "./authUser";

/**
 * Result of the password step (docs/38 "Login is a challenge, not a fork"):
 * `ok` means a session exists; `totp` means the password was accepted and the
 * second factor is now waiting — there is deliberately NO session yet.
 */
export type LoginResult = { status: "ok" } | { status: "totp"; challengeToken: string };

interface AuthState {
  /** null until the session probe resolves; false = definitely logged out. */
  ready: boolean;
  user: User | null;
  login: (email: string, password: string) => Promise<LoginResult>;
  /** Second factor: exchange the challenge token for the session. */
  completeTotpLogin: (challengeToken: string, code: string) => Promise<void>;
  /** Same, with a single-use recovery code. */
  completeRecoveryLogin: (challengeToken: string, code: string) => Promise<void>;
  /**
   * Passwordless sign-in: options for the typed email → WebAuthn assertion →
   * session. Resolves `false` when the user dismissed the authenticator prompt
   * (no error to show, no session to enter); rejects on a real failure.
   */
  passkeyLogin: (email: string) => Promise<boolean>;
  /** Finish the OIDC redirect handshake (POST /auth/oidc/confirm). */
  oidcConfirm: () => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  /** One-click demo login (w8 contract): seeds + logs into the demo account. */
  demoLogin: () => Promise<void>;
  logout: () => Promise<void>;
  /** Push an updated user object into context (e.g. after a /users/me PATCH). */
  setUser: (u: User) => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [ready, setReady] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  // Identifies the tenant the cache currently belongs to. null until the
  // session probe resolves, matching `user`'s initial value.
  const cacheOwner = useRef<string | number | null>(null);

  /**
   * Drop every cached query on any identity change.
   *
   * The React Query cache is per-browser, not per-user: each entry was fetched
   * with the access token of whoever was signed in. Anything that survives an
   * identity change is therefore served to the next user from the wrong
   * tenant (AGENTS.md §2: `user_id` on every query). This is the single seam
   * every EXPLICIT auth transition funnels through, so clearing here covers
   * login, register, demo login and logout without touching pages. Session
   * RESTORE deliberately does not: it seeds `cacheOwner` directly to keep the
   * cache the reload is repopulating (see the effect below) — which is why the
   * mask contract is its own helper instead of lines in here.
   *
   * The session-storage half is DIFFERENT and belongs to this branch: unlike a
   * query cache, `fintrack.recurring-candidates.v1` is written by the tab, not
   * fetched under a token, so an entry that outlives its identity would be
   * RENDERED to the next tenant (B2 — `lib/recurringSession.ts` stamps every
   * entry and `scopeCandidatesTo` re-binds the tab here, purging rows that are
   * not the new identity's before anything can mount).
   */
  /**
   * The MASK half of an identity transition (M139, docs/09 § Display masking).
   *
   * Split out of `applyUser` so the RESTORE path runs it too (t_2d8ffedb P1).
   * What it owns is a BOOT contract: `initMaskForSession()` clears any stale
   * sessionStorage key and applies the caller's default (restore-on-load is
   * OFF by PM ruling — the user re-masks deliberately), then a read-only demo
   * session takes the storefront default. A restored tab IS a boot, so
   * skipping this left the stale key in place AND skipped the demo default —
   * the chip said "Unmasked" while the stored key said masked.
   *
   * The cache eviction stays OUT of this helper on purpose: a restore must not
   * drop the cache it is repopulating.
   */
  /** The mask a session STARTS with — one definition, so an identity change
   *  and a restore cannot drift apart. */
  const applyInitialMask = useCallback((next: User | null) => {
    if (isReadOnlySession(next)) applyDemoMaskDefault();
  }, []);

  /** The mask half of an EXPLICIT identity change (login/register/logout): the
   *  mask dies with the identity it belonged to, so nothing is preserved. */
  const resetMaskFor = useCallback(
    (next: User | null) => {
      initMaskForSession();
      applyInitialMask(next);
    },
    [applyInitialMask],
  );

  /** The mask half of a RESTORE: the same boot contract, except that a choice
   *  the user already made this session wins (see initMaskForRestoredSession). */
  const bootRestoredMaskFor = useCallback(
    (next: User | null) => {
      initMaskForRestoredSession();
      applyInitialMask(next);
    },
    [applyInitialMask],
  );

  /**
   * The other end of the same boot: a probe that found NO session. Logged out,
   * so the stale access token goes and there is no tenant to take a default
   * from — the boot default is unmasked.
   *
   * A FAILED boot is still a boot (docs/46 D1: "a boot clears any stale key and
   * starts from the tenant default"). Without this the stale key outlived the
   * session it described until the next successful transition.
   */
  const failedBoot = useCallback(() => {
    client.tokens.clear();
    initMaskForRestoredSession();
    // No session, no tenant: the tab may not keep a session-scoped cache of
    // candidate rows it can no longer attribute to anyone (B2).
    scopeCandidatesTo(null);
  }, []);

  const applyUser = useCallback(
    (next: User | null) => {
      const nextOwner = next?.id ?? null;
      if (cacheOwner.current !== nextOwner) {
        // removeQueries (not clear) so mounted observers refetch under the new
        // token; clear() would leave mounted components showing the old
        // tenant's data with no pending fetch to correct it.
        queryClient.removeQueries();
        cacheOwner.current = nextOwner;
        resetMaskFor(next);
        // B2: the recurring-candidate cache is tab-scoped storage, not a query
        // — re-bind it to the new identity (null = logout) so the previous
        // tenant's rows are gone before any page can render them.
        scopeCandidatesTo(nextOwner);
      }
      setUser(next);
    },
    [queryClient, resetMaskFor],
  );

  useEffect(() => {
    // Session probe: try to restore from the httpOnly refresh cookie.
    // If the cookie is valid, we get a fresh access token + user; otherwise
    // the user is logged out and sees the login page.
    let cancelled = false;
    client
      .restoreSession()
      .then((pair) => {
        // A failed probe RESOLVES `null` — `postRefreshToken` returns null on a
        // non-ok status and on a thrown fetch, and never rejects — so this is
        // where a failed boot actually lands. Leaving the contract to the
        // `.catch` alone made it dead code in production (t_5900dd66): the
        // stale key survived the failed reload it was supposed to be cleared
        // by. Both homes now call the same helper.
        if (cancelled) return;
        if (!pair) {
          failedBoot();
          return;
        }
        // The session restored on a fresh page load is the FIRST tenant for
        // this cache: there is no previous owner to evict, and clearing here
        // would discard the cache the app is still populating. Seeding the ref
        // without a clear is what keeps a reload from throwing away work.
        //
        // The CACHE half is skipped here; the MASK half is not (t_2d8ffedb P1).
        // A restore is still a boot, so the session contract has to run: this
        // path used to seed `cacheOwner` and nothing else, which left a stale
        // `fintrack.mask` key outliving the session it described and skipped
        // the read-only demo default entirely.
        cacheOwner.current = pair.user.id;
        bootRestoredMaskFor(pair.user);
        // A restore is the seam a reload — and a refresh cookie that changed
        // in ANOTHER tab — arrives on, so the stored candidates are re-bound
        // here too: the restored identity keeps their own entry (the tab still
        // does not re-run the scan) and anybody else's is dropped.
        scopeCandidatesTo(pair.user.id);
        setUser(pair.user);
      })
      .catch(() => {
        // A probe that THROWS is the same failed boot (the resolved-null shape
        // above is what the real client emits today).
        if (!cancelled) failedBoot();
      })
      .finally(() => {
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [bootRestoredMaskFor, failedBoot]);

  const login = useCallback(
    async (email: string, password: string): Promise<LoginResult> => {
      const result = await client.login(email, password);
      // A challenge is NOT a session: nothing to store, no identity to apply
      // (docs/38 decision 4 — no session exists until the second factor lands).
      if ("challenge" in result) {
        return { status: "totp", challengeToken: result.challenge_token };
      }
      applyUser(result.user);
      return { status: "ok" };
    },
    [applyUser],
  );

  const completeTotpLogin = useCallback(
    async (challengeToken: string, code: string) => {
      const result = await client.loginWithTotp(challengeToken, code);
      applyUser(result.user);
    },
    [applyUser],
  );

  const completeRecoveryLogin = useCallback(
    async (challengeToken: string, code: string) => {
      const result = await client.loginWithRecovery(challengeToken, code);
      applyUser(result.user);
    },
    [applyUser],
  );

  const passkeyLoginFn = useCallback(
    async (email: string): Promise<boolean> => {
      // Blank email = the discoverable ceremony (docs/38): the server binds
      // the challenge to no account and resolves one from the assertion, so
      // sign-in needs no address. A typed email keeps the email-first path
      // that also serves non-discoverable credentials.
      const trimmed = email.trim();
      const { challenge_token, options } = await client.passkeyLoginOptions(
        trimmed.length > 0 ? trimmed : undefined,
      );
      const credential = await getPasskey(options as unknown as PasskeyRequestOptions);
      // User dismissed the authenticator prompt: not a failure, just no sign-in.
      if (credential === null) return false;
      const result = await client.passkeyLogin(challenge_token, credential);
      applyUser(result.user);
      return true;
    },
    [applyUser],
  );

  const oidcConfirmFn = useCallback(async () => {
    const result = await client.oidcConfirm();
    applyUser(result.user);
  }, [applyUser]);

  const register = useCallback(
    async (email: string, password: string) => {
      // M87 (docs/29 F15): register no longer opens a session — it answers
      // the same token-free body for a free and a taken address, so the
      // caller is sent to the login form next instead of to the Dashboard.
      await client.register(email, password);
    },
    [],
  );

  const demoLogin = useCallback(async () => {
    const result = await client.demoLogin();
    applyUser(result.user);
  }, [applyUser]);

  const logout = useCallback(async () => {
    await client.logout();
    applyUser(null);
  }, [applyUser]);

  const value = useMemo<AuthState>(
    () => ({
      ready,
      user,
      login,
      completeTotpLogin,
      completeRecoveryLogin,
      passkeyLogin: passkeyLoginFn,
      oidcConfirm: oidcConfirmFn,
      register,
      demoLogin,
      logout,
      setUser: (u) => applyUser(u),
    }),
    [
      ready,
      user,
      login,
      completeTotpLogin,
      completeRecoveryLogin,
      passkeyLoginFn,
      oidcConfirmFn,
      register,
      demoLogin,
      logout,
      applyUser,
    ],
  );

  // M136: the session-user scope rides OUTSIDE the auth context so shared
  // leaf components (the E2EE vault hooks) can read who is signed in without
  // importing a module eighteen test files mock wholesale.
  return (
    <AuthUserScopeContext.Provider value={{ user }}>
      <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
    </AuthUserScopeContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return ctx;
}

