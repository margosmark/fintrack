// VaultLocked (M136, docs/33 §Server-first computing) — the ONE component a
// locked vault renders through.
//
// THE CONTRACT it exists to enforce (docs/33 rule 7):
//   a. gate the notice on EXCLUDED DATA, not on lock state — a surface the
//      lock does not affect carries NO lock copy at all;
//   b. name the capability, not the mechanism ("…to search encrypted payees",
//      never "resolve cipher rows" / "DEK" / "index key");
//   c. unlock IN PLACE — inline passphrase, one interaction, zero navigation;
//      until inline unlock shipped the copy pointed at the settings route, and
//      the canonical destination string still travels with `blocked`;
//   d. a remedy named in the copy must actually restore the feature at that
//      moment — "disable E2EE" stays banned until a decrypt flow exists;
//   e. scope the outage honestly — "encrypted payees are hidden", never the
//      whole surface when only part of it is down;
//   f. a lock hint REPLACES an empty state, never stacks with it;
//   g. one string per capability: FOUR lock-state constants, in this module.
//
// Three variants, one per shape of announcement:
//
//   status   a chip in the shell — state, no claim about any surface
//   partial  a real withheld count ("Showing X of Y rows — unlock to search
//            the other Z"); REPLACES an empty state
//   blocked  renders IN PLACE OF the control that cannot run (Save, Import),
//            carrying the canonical destination string
//
// Every variant that announces is `role="status"`, so a screen-reader user
// hears the withholding in the same breath sighted users read it.

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Lock } from "lucide-react";
import { Button } from "../ui/Button";
import { useUnlock, unlockFailureMessage } from "./useUnlock";

/**
 * Canonical destination, VERBATIM everywhere (M134 §0.7 / uxbot finding 4):
 * three different answers shipped before this wave ("Settings → Encryption",
 * "Settings → Security", "Encryption above") and none of them matched the
 * tree — the Security card is TOTP-only. This string is the one true path to
 * the Encryption panel.
 */
export const VAULT_LOCK_DESTINATION =
  "Settings → Privacy & data → Client-side encryption (in progress)";

/** The FOUR lock-state strings in the app. Adding a fifth is a failed review. */
export const VAULT_LOCK_COPY = {
  /** Rows are withheld from a matcher (S-1, S-2, S-3). */
  search: "Unlock the vault to search encrypted payees.",
  /** A write that cannot produce its ciphers (G1, G2). */
  write: "Unlock the vault to save — this account requires encrypted writes.",
  /** A capability that only the browser can run (M56 backup, the backfill). */
  client: "Unlock the vault to run this here — only an unlocked tab holds the key.",
  /** The shell chip: state, never a claim about a surface. */
  status: "Vault locked",
} as const;

type CopyKey = keyof typeof VAULT_LOCK_COPY;

/**
 * Every capability maps to ONE of the four strings. `reports:cashflow` is
 * listed because it is the matrix's canonical *unaffected* example: the
 * reports read the mandatory plaintext columns, so a cashflow surface never
 * renders this component at all — the row exists so the type can say so.
 */
const COPY_OF: Record<VaultCapability, CopyKey> = {
  search: "search",
  "reports:cashflow": "search",
  "backup:sched": "client",
  backfill: "client",
  "write:split": "write",
  "write:bulk-rename": "write",
};

export type VaultCapability =
  | "search"
  | "reports:cashflow"
  | "backup:sched"
  | "backfill"
  | "write:split"
  | "write:bulk-rename";

export type VaultLockedVariant = "blocked" | "partial" | "status";

export interface VaultLockedProps {
  /** Defaults to `blocked` — the pre-emptive gate in front of a control. */
  variant?: VaultLockedVariant;
  capability: VaultCapability;
  /** Rows the lock is withholding right now (`partial`). */
  withheld_count?: number;
  /** Rows the surface CAN show (`partial`): `shown + withheld`. */
  total_count?: number;
  /** Set on the root element so a disabled control can `aria-describedby` it. */
  id?: string;
  className?: string;
}

const CHIP_CLASS =
  "inline-flex items-center gap-1.5 rounded-full border border-accent/30 bg-accent/10 px-2.5 py-1 text-xs text-accent";

/**
 * The one partial sentence, built from real counts (never invented data —
 * `withheld_count` is always a number the caller measured, and a caller that
 * cannot measure it uses `blocked` instead of guessing).
 */
export function vaultPartialCopy(shown: number, total: number): string {
  const withheld = Math.max(total - shown, 0);
  if (total <= 0) return VAULT_LOCK_COPY.search;
  return `Showing ${shown} of ${total} rows — unlock to search the other ${withheld}.`;
}

export function VaultLocked({
  variant = "blocked",
  capability,
  withheld_count,
  total_count,
  id,
  className = "",
}: VaultLockedProps) {
  const copy = VAULT_LOCK_COPY[COPY_OF[capability]];
  const { unlocked, supported, ready, unlock } = useUnlock();
  const [revealed, setRevealed] = useState(false);
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const passphraseRef = useRef<HTMLInputElement>(null);
  const errorId = useId();

  // Tab focus management: revealing the field moves focus INTO it, so the
  // next Tab continues from the passphrase rather than from the button that
  // opened it (M136 a11y acceptance).
  useEffect(() => {
    if (revealed) passphraseRef.current?.focus();
  }, [revealed]);

  // `vaultchange` is the single source of truth; `useUnlock` subscribes to it.
  useEffect(() => {
    if (unlocked) {
      setRevealed(false);
      setPassphrase("");
      setError(null);
    }
  }, [unlocked]);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (passphrase === "" || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await unlock(passphrase);
      if (!result.ok) {
        setError(unlockFailureMessage(result.reason));
        passphraseRef.current?.focus();
        passphraseRef.current?.select();
        return;
      }
      setPassphrase("");
      setRevealed(false);
    } finally {
      setBusy(false);
    }
  }

  // (a) The chip is the shell's persistent STATE indicator. It says what is
  // true ("Vault locked"), never that a particular surface is degraded — so it
  // reads the `status` copy, NOT the capability's (a chip rendered with
  // capability="search" used to announce "Unlock the vault to search …" as if
  // the shell were a search surface; rule 7a).
  if (variant === "status") {
    return (
      <span
        id={id}
        role="status"
        data-testid="vault-locked-chip"
        className={`${CHIP_CLASS} ${className}`}
      >
        <Lock className="size-3" aria-hidden />
        {VAULT_LOCK_COPY.status}
      </span>
    );
  }

  const shown = Math.max((total_count ?? 0) - (withheld_count ?? 0), 0);
  const headline =
    variant === "partial" && withheld_count !== undefined && total_count !== undefined
      ? vaultPartialCopy(shown, total_count)
      : copy;

  const inlineUnlock = (
    <>
      {!revealed ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!supported || !ready}
          onClick={() => setRevealed(true)}
          data-testid="vault-unlock-toggle"
        >
          Unlock vault
        </Button>
      ) : (
        <form
          onSubmit={(event) => void submit(event)}
          className="flex flex-wrap items-center gap-2"
          data-testid="vault-unlock-form"
        >
          <input
            ref={passphraseRef}
            type="password"
            value={passphrase}
            aria-label="Encryption passphrase"
            aria-describedby={error === null ? undefined : errorId}
            onChange={(event) => setPassphrase(event.target.value)}
            className="field-ring w-56 rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70"
            placeholder="Encryption passphrase"
            data-testid="vault-unlock-passphrase"
          />
          <Button type="submit" size="sm" variant="primary" loading={busy} disabled={busy}>
            Unlock
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setRevealed(false);
              setError(null);
            }}
          >
            Cancel
          </Button>
        </form>
      )}
      {!supported && (
        <span className="text-xs text-muted">
          This browser has no Web Crypto or IndexedDB, so it cannot unlock the vault.
        </span>
      )}
      {error !== null && (
        <span id={errorId} role="alert" className="text-xs text-danger" data-testid="vault-unlock-error">
          {error}
        </span>
      )}
    </>
  );

  if (variant === "blocked") {
    return (
      <span
        id={id}
        role="status"
        data-testid="vault-locked"
        data-vault-variant="blocked"
        className={`flex flex-wrap items-center gap-2 ${className}`}
      >
        <Lock className="size-3.5 shrink-0 text-muted" aria-hidden />
        <span className="text-xs text-muted" data-testid="vault-locked-copy">
          {headline}{" "}
          <span className="text-muted" data-testid="vault-locked-destination">
            {VAULT_LOCK_DESTINATION}
          </span>
        </span>
        {inlineUnlock}
      </span>
    );
  }

  return (
    <div
      id={id}
      role="status"
      data-testid="vault-locked"
      data-vault-variant="partial"
      className={`flex flex-wrap items-center gap-2 rounded-lg border border-accent/30 bg-accent/10 px-3 py-2 ${className}`}
    >
      <Lock className="size-3.5 shrink-0 text-accent" aria-hidden />
      <span className="text-sm" data-testid="vault-locked-copy">
        {headline}{" "}
        <span className="text-muted" data-testid="vault-locked-destination">
          {VAULT_LOCK_DESTINATION}
        </span>
      </span>
      {inlineUnlock}
    </div>
  );
}
