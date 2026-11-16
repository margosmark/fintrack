// ServerSideBlocked (M181 follow-up, docs/33 §Server-first computing rules 6
// and 7) — the ONE component a server-side-only write renders through when the
// tenant is opted into `users.encryption_required`.
//
// WHY THIS IS NOT `VaultLocked`:
//
//   `VaultLocked` gates a write the BROWSER cannot make while the vault is
//   locked, so its `blocked` variant offers the inline unlock and unlocking
//   genuinely restores the control (rules 7c / 7d).
//
//   A server-side ingestion write is the opposite case. The row is written by
//   the SERVER, into columns that hold no cipher at all, so `encryption_required`
//   makes the API refuse it no matter what this browser holds. Unlocking the
//   vault cannot enable it — which is exactly why rule 7d BANS naming unlock
//   (or the Encryption panel destination) as the remedy here, while rule 6
//   still requires the control to be DISABLED WITH REASON before submit.
//
//   So this component:
//     * gates on ENROLMENT ALONE (`users.encryption_required`), never on
//       `enrolled && !unlocked` — a lock-state gate would both LIE (unlocking
//       does not help) and hide the reason from an unlocked-but-enrolled user;
//     * renders NO unlock affordance and NO `VAULT_LOCK_DESTINATION`;
//     * carries its own capability strings, so `VaultLocked.tsx`'s
//       `VAULT_LOCK_COPY` stays at its FOUR lock-state constants (rule 7g).
//       These are not lock-state strings and do not count against that cap.
//
// The capability phrase of each string is VERBATIM the one the matching
// endpoint refuses with — `_refuse_when_encrypted(current_user, "<capability>")`
// in `backend/app/api/imports.py`, `api/trades.py` and `api/ynab.py`, and the
// inline copy in `api/sync.py` — minus ONLY that message's trailing "…so
// nothing was saved", because nothing has been submitted yet. That is what
// keeps the pre-emptive notice and the defense-in-depth 422 from ever telling
// two stories. The tail below therefore carries the endpoint's own words
// ("cannot SAFELY write encrypted data"); a paraphrase here would silently
// break the "minus only the trailing clause" claim (docs/09 §Server-side
// writes, security finding F3).

import { Lock } from "lucide-react";
import { useEncryptionRequired } from "./useUnlock";

/** The shared tail: one reason sentence, written once. */
const WHY =
  " is unavailable while client-side encryption is enabled: this path runs server-side and cannot safely write encrypted data.";

/**
 * One string per capability (docs/33 rule 7b — names the capability, never the
 * mechanism). Keys are the frontend's own capability ids; the values start
 * with the endpoint's own capability phrase.
 */
export const SERVER_SIDE_BLOCK_COPY = {
  /** `POST /imports/upload` — parse + stage a bank statement. */
  "import:statement": `Importing a statement file${WHY}`,
  /** `POST /imports/{batch_id}/commit` — make a staged statement batch live. */
  "import:statement-commit": `Committing a staged import batch${WHY}`,
  /**
   * `POST /imports/ynab/commit` — the YNAB budget migration upload (docs/38).
   *
   * This one used to be a `VaultLocked` gate keyed on `enrolled && !unlocked`
   * (G3), which offered "unlock the vault" for a write the SERVER must refuse
   * regardless of lock state. It is a server-side upload like every other key
   * here, so it lives on the enrolment-only gate now (docs/33 rule 7d).
   */
  "import:ynab": `Importing a YNAB budget file${WHY}`,
  /**
   * `POST /imports/networth/commit` — a ProjectionLab export or an
   * Account/Balance spreadsheet (docs/58 §B). Preview stays open: it is
   * read-only, and a tenant's balance file still parses without the vault.
   */
  "import:networth": `Importing a net-worth file${WHY}`,
  /** `POST /trades/import` — parse + stage a brokerage activity file. */
  "import:trades": `Importing a brokerage activity file${WHY}`,
  /** `POST /trades/import/{batch_id}/commit` — make staged trades live. */
  "import:trades-commit": `Committing a staged trade import${WHY}`,
  /** `POST /trades` — the manual trade form. */
  "write:trade": `Recording a trade${WHY}`,
  /** `POST /trades/record-dividend` — both dividend-capture surfaces. */
  "write:dividend": `Recording a dividend${WHY}`,
  /**
   * `POST /sync/agreements/{id}/pull`.
   *
   * Reserved: the SPA has no sync surface at all yet (`frontend/src` holds zero
   * `/sync` references — docs/33 § "OUTSIDE THE ENVELOPE (M79)"), so nothing
   * renders this string today. It lives here so the future sync status UI
   * cannot invent its own wording for the one sync route that is refused.
   */
  "sync:pull": `Pulling transactions from a bank provider${WHY}`,
} as const;

export type ServerSideCapability = keyof typeof SERVER_SIDE_BLOCK_COPY;

/**
 * Is a server-side write refused for this tenant?
 *
 * ENROLMENT ALONE — `users.encryption_required`, read from the session user.
 * Deliberately NOT `useVaultUnlocked()`: the server's refusal is keyed on the
 * flag alone (`backend/app/api/*.py::_refuse_when_encrypted`), so a lock-state
 * gate here would disagree with the API in both directions. `null` outside an
 * AuthProvider degrades to `false` — no session, no claim (rule 7a).
 */
export function useServerSideWriteBlocked(): boolean {
  return useEncryptionRequired();
}

export interface ServerSideBlockedProps {
  capability: ServerSideCapability;
  /** Set on the root so a disabled control can `aria-describedby` it. */
  id?: string;
  className?: string;
}

/**
 * The reason, rendered where the control that cannot run would have been
 * (docs/33 rule 6: DISABLED WITH REASON before submit, never a post-hoc 422 on
 * a save the user already performed).
 *
 * `role="status"` so a screen reader hears the refusal in the same breath
 * sighted users read it — the same contract `VaultLocked` keeps.
 */
export function ServerSideBlocked({ capability, id, className = "" }: ServerSideBlockedProps) {
  return (
    <span
      id={id}
      role="status"
      data-testid="server-side-blocked"
      data-server-side-capability={capability}
      className={`flex flex-wrap items-center gap-2 ${className}`}
    >
      <Lock className="size-3.5 shrink-0 text-muted" aria-hidden />
      <span className="text-xs text-muted" data-testid="server-side-blocked-copy">
        {SERVER_SIDE_BLOCK_COPY[capability]}
      </span>
    </span>
  );
}
