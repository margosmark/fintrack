// PendingImportBanner (M113, docs/09 §Account page pending-import banner) —
// the fix for Mark's TFSA-import confusion: he staged a batch, opened the
// account page, saw no securities and no hint that an import was waiting, and
// abandoned the flow twice. The staging model was right; the DISCOVERABILITY
// was not.
//
// Contract:
//  * renders only when `count > 0` — a committed / discarded / reverted batch
//    is not pending, so nothing about those ever appears (the API sends a
//    count and only the pending count, never batch contents — E2EE row-count
//    rule);
//  * `role="status"` + `aria-live="polite"` — announced, never a modal;
//  * the link and the dismiss control are both ≥44px touch targets
//    (docs/09 §Minimum Sizes);
//  * semantic tokens only, so both themes pass the documented contrast pairs;
//  * dismissible ONCE per account per browser session (sessionStorage — the
//    house pattern `Transactions.tsx` uses for its review hint): it stays
//    dismissed across navigation and reload, and comes back in a new tab or a
//    new session, where "there is still an import waiting" is news again.
import { useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { Upload } from "lucide-react";
import { fadeUp } from "../ui/motion";
import { useOptionalUser } from "../../auth/authUser";
import { tenantScope } from "../../lib/tenantStorage";

// F-6: the dismissal flag is keyed by ACCOUNT id, so two tenants sharing a
// browser can hold the same account id — the key therefore carries the
// tenant scope too (`lib/tenantStorage.ts`), or tenant A's dismissal hides
// tenant B's banner.
const storageKey = (scope: string, accountId: number): string =>
  `fintrack.m113.pending-import-banner.${scope}.${accountId}.dismissed`;

function readDismissed(scope: string, accountId: number): boolean {
  try {
    return sessionStorage.getItem(storageKey(scope, accountId)) === "1";
  } catch {
    return false; // storage blocked (private mode): banner just shows again
  }
}

export interface PendingImportBannerProps {
  accountId: number;
  /** `Account.pending_import_count` — pending batches only, count only. */
  count: number;
  /** Human account label (the account type, e.g. "TFSA"). */
  label: string;
}

export function PendingImportBanner({ accountId, count, label }: PendingImportBannerProps) {
  // The session's own tenant scope — one value per render (F-6).
  const scope = tenantScope(useOptionalUser());
  const [dismissed, setDismissed] = useState(() => readDismissed(scope, accountId));
  // The element can stay mounted across /accounts/1 → /accounts/2 (same
  // route, new param), so the dismissal is re-read when the account on
  // screen changes instead of being cached for the wrong account.
  const [mountedAccount, setMountedAccount] = useState(accountId);
  if (mountedAccount !== accountId) {
    setMountedAccount(accountId);
    setDismissed(readDismissed(scope, accountId));
  }

  if (count <= 0 || dismissed) {
    return null;
  }

  const heading =
    count === 1
      ? `1 uncommitted import for ${label}`
      : `${count} uncommitted imports for ${label}`;

  return (
    <motion.div
      variants={fadeUp}
      initial="hidden"
      animate="show"
      role="status"
      aria-live="polite"
      data-testid="pending-import-banner"
      className="mb-6 flex flex-wrap items-start justify-between gap-3 rounded-xl border border-accent/40 bg-accent/[0.07] p-4"
    >
      <div className="flex min-w-0 items-start gap-3">
        <Upload className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
        <div className="min-w-0">
          <p className="text-sm font-semibold text-text">{heading}</p>
          <p className="mt-0.5 text-sm text-muted">
            Review the rows and press Commit on the Imports page — until you do, nothing in it
            is live in this account.
          </p>
          {/* ≥44px touch target (docs/09): a client-side route, never a full
              page reload (M68). */}
          <Link
            to="/imports"
            className="mt-1 inline-flex min-h-[44px] items-center rounded-lg px-2 text-sm font-medium text-accent outline-none transition-colors hover:underline focus-visible:ring-2 focus-visible:ring-accent"
          >
            Go to the Imports page
          </Link>
        </div>
      </div>
      <button
        type="button"
        aria-label="Dismiss the uncommitted-import banner for this session"
        onClick={() => {
          setDismissed(true);
          try {
            sessionStorage.setItem(storageKey(scope, accountId), "1");
          } catch {
            // storage blocked: still dismissed for this mount
          }
        }}
        className="ml-auto min-h-[44px] shrink-0 rounded-lg border border-accent/40 px-3 text-sm font-medium text-accent outline-none transition-colors hover:bg-accent/10 focus-visible:ring-2 focus-visible:ring-accent"
      >
        Dismiss
      </button>
    </motion.div>
  );
}
