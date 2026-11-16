/**
 * Inline unlock + lock-state subscriptions (M136, docs/33 §Server-first
 * computing rule 7c).
 *
 * `unlockVaultSynced()` (`lib/vaultSync.ts`) is the whole Phase-4 unlock flow,
 * but until this wave it had exactly ONE call site — Settings' Encryption
 * panel — because every other surface told the user to *go there*. That is
 * what made "unlock in place" a promise the tree could not keep.
 *
 * This module lifts it into three hooks every degraded surface shares:
 *
 *   useUnlock()             live lock state + an inline `unlock(passphrase)`
 *   useVaultEnrollment()    is there a vault to unlock AT ALL? (the chip's gate)
 *   useEncryptionRequired() `users.encryption_required` — the write-guard world
 *
 * None of them touch the network on mount: the lock state is in-memory, the
 * enrollment probe runs only while the vault is locked, and the write guard
 * reads the session user the auth context already holds.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useOptionalUser } from "../../auth/authUser";
import { listEnvelopes } from "../../api/vault";
import { useQuery } from "@tanstack/react-query";
import { backupSupported } from "../../lib/privacyBackup";
import { isUnlocked, loadVault } from "../../lib/vault";
import {
  defaultSyncDeps,
  getDeviceId,
  unlockVaultSynced,
  type UnlockFailure,
  type UnlockResult,
  type VaultSyncDeps,
} from "../../lib/vaultSync";

/**
 * Why an unlock failed — the SAME four strings Settings' Encryption panel has
 * always shown, lifted here so the inline unlock cannot invent a fifth. Lock
 * STATE copy lives in `VaultLocked.tsx` and is capped at four; these are
 * failure reasons, not state claims.
 */
export const UNLOCK_FAILURE_COPY: Record<UnlockFailure, string> = {
  wrong_passphrase: "Wrong passphrase — the vault stays locked.",
  device_revoked:
    "This browser's key was revoked from another device. Clear this site's data to enroll it again.",
  sync_unreachable: "Can't reach key sync — the vault stays locked.",
  // The canonical destination string itself lives in `VaultLocked.tsx` (one
  // module owns every lock string); this sentence defers to it rather than
  // restating the path and drifting.
  no_envelope: "No vault on this account yet — enroll it first (see below).",
};

/** One place to turn a failed inline unlock into user-facing copy. */
export function unlockFailureMessage(reason: UnlockFailure): string {
  return UNLOCK_FAILURE_COPY[reason];
}

/**
 * Live lock state. Follows the `vaultchange` event `lib/vault.ts` dispatches
 * on create / unlock / lock / revoke, so every surface re-renders the moment
 * the vault moves — no polling, no "Recheck" button.
 */
export function useVaultUnlocked(): boolean {
  const [unlocked, setUnlocked] = useState(() => isUnlocked());
  useEffect(() => {
    const onChange = (): void => setUnlocked(isUnlocked());
    onChange();
    window.addEventListener("vaultchange", onChange);
    return () => window.removeEventListener("vaultchange", onChange);
  }, []);
  return unlocked;
}

export interface VaultUnlockHandle {
  /** Live lock state (the same subscription `useVaultUnlocked` gives). */
  unlocked: boolean;
  /** Web Crypto + IndexedDB — the same gate Settings uses before offering it. */
  supported: boolean;
  /** `false` while signed out: there is no account to unlock against. */
  ready: boolean;
  /**
   * Unlock in place. Resolves the Phase-4 `UnlockResult` unchanged so the
   * caller keeps Settings' own failure copy (`wrong_passphrase`,
   * `device_revoked`, `sync_unreachable`, `no_envelope`).
   */
  unlock: (passphrase: string) => Promise<UnlockResult>;
}

/** `defaultSyncDeps(userId)`, or null while signed out. */
export function useVaultDeps(): VaultSyncDeps | null {
  const userId = useOptionalUser()?.id ?? null;
  return useMemo(() => (userId === null ? null : defaultSyncDeps(String(userId))), [userId]);
}

export function useUnlock(): VaultUnlockHandle {
  const deps = useVaultDeps();
  const unlocked = useVaultUnlocked();
  const unlock = useCallback(
    async (passphrase: string): Promise<UnlockResult> => {
      if (deps === null) return { ok: false, reason: "sync_unreachable" };
      // Every success path inside `unlockVaultSynced` calls `unlockVault`,
      // which dispatches `vaultchange` — so the subscription above (and every
      // other surface) re-renders without this hook pushing state by hand.
      return unlockVaultSynced(passphrase, deps);
    },
    [deps],
  );
  return { unlocked, supported: backupSupported(), ready: deps !== null, unlock };
}

export interface VaultEnrollment {
  /** True when the ACCOUNT has a vault — enrolled here, or on another device. */
  enrolled: boolean;
  /** True while this browser holds its own wrapped copy (IndexedDB). */
  enrolledHere: boolean;
  /** The server envelope list, or null when it could not be read. */
  error: unknown;
}

/**
 * Is there a vault to unlock at all?
 *
 * The lock chip must not claim "locked" for a tenant that never enrolled —
 * a caveat on an unaffected surface is a false claim (docs/33 rule 7a). Three
 * cheap signals, in increasing cost:
 *
 *   1. `users.encryption_required` — the write guard is on, so the account
 *      has a vault story regardless of this browser;
 *   2. the local wrapped bundle in IndexedDB — this browser enrolled;
 *   3. `GET /vault/envelopes` — another device enrolled.
 *
 * (3) is the only network call and it is `enabled` only while the vault is
 * locked and neither of the first two already answered.
 */
export function useVaultEnrollment(force?: boolean): VaultEnrollment {
  const user = useOptionalUser();
  const userId = user?.id ?? null;
  const unlocked = useVaultUnlocked();
  const supported = backupSupported();
  const writeGuard = user?.encryption_required === true;

  const [enrolledHere, setEnrolledHere] = useState(false);
  useEffect(() => {
    if (userId === null || !supported) return;
    let cancelled = false;
    void loadVault(String(userId))
      .then((bundle) => {
        if (!cancelled) setEnrolledHere(bundle !== null);
      })
      .catch(() => {
        /* IndexedDB unavailable — signal 3 still speaks. */
      });
    return () => {
      cancelled = true;
    };
  }, [userId, supported]);

  const needsServerProbe = supported && !unlocked && !writeGuard && !enrolledHere;
  const envelopes = useQuery({
    queryKey: ["vaultEnvelopes", userId, "shell"],
    queryFn: () => listEnvelopes(getDeviceId()),
    enabled: needsServerProbe || force === true,
    retry: false,
    staleTime: 30_000,
  });

  const accountHasVault = (envelopes.data?.items.length ?? 0) > 0;
  return {
    enrolled: writeGuard || enrolledHere || accountHasVault,
    enrolledHere,
    error: envelopes.error,
  };
}

/**
 * `users.encryption_required` for the signed-in tenant (null when signed out).
 *
 * This is the write-side half of the E2EE contract: when it is true a write
 * that cannot produce ciphers is GUARANTEED to 422, so the three cipher
 * families (split save, bulk matched-rename, YNAB commit) gate on
 * `encryptionRequired && !isUnlocked()` BEFORE submit.
 */
export function useEncryptionRequired(): boolean {
  return useOptionalUser()?.encryption_required === true;
}
