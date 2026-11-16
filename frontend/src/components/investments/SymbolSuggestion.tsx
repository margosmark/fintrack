import type { SecurityLookupCandidate } from "../../api/securities";
import { listingLabel } from "../../api/securities";
import { Button } from "../ui/Button";

// M142 (docs/09 §New security suggestion chip): the lookup resolved while the
// user was typing and the answer is NOT the string they typed. Two shapes, one
// rule — a guess is never stored silently (docs/07 §Symbol search fallback):
//
//   SuggestionChip   exactly one listing resolved -> "Did you mean …?", one
//                    click to accept, or keep typing and it goes away
//   CandidatePicker  several listings matched     -> a pick-one list; picking
//                    re-resolves the canonical symbol through the endpoint
//
// Both are presentational: the caller owns the state and the fetch.

interface SuggestionChipProps {
  /** What the user typed — shown as the thing being corrected. */
  typed: string;
  /** The canonical ticker the search resolved to (`APTX.TO`). */
  symbol: string;
  /** "APOTEX HEALTH CORP (Toronto, CAD)" — see `listingLabel`. */
  label: string;
  onAccept: () => void;
  onDismiss: () => void;
}

export function SuggestionChip({
  typed,
  symbol,
  label,
  onAccept,
  onDismiss,
}: SuggestionChipProps) {
  return (
    <div
      role="status"
      data-testid="symbol-suggestion"
      className="flex items-start justify-between gap-2 rounded-lg border border-accent/30 bg-accent/[0.06] px-3 py-2 text-xs"
    >
      <p className="min-w-0 text-text">
        Did you mean <span className="font-mono font-medium text-accent">{symbol}</span> — {label}
        ? <span className="text-muted">(you typed {typed})</span>
      </p>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          type="button"
          variant="accent-soft"
          size="sm"
          onClick={onAccept}
          data-testid="accept-symbol-suggestion"
        >
          Use {symbol}
        </Button>
        <button
          type="button"
          aria-label="Dismiss symbol suggestion"
          data-testid="dismiss-symbol-suggestion"
          onClick={onDismiss}
          className="leading-none text-muted transition-colors hover:text-text"
        >
          ✕
        </button>
      </div>
    </div>
  );
}

interface CandidatePickerProps {
  /** What the user typed — the chooser names it so the choice is legible. */
  typed: string;
  candidates: SecurityLookupCandidate[];
  /** True while the pick is being applied (kept for the search-panel caller). */
  pending: boolean;
  onPick: (candidate: SecurityLookupCandidate) => void;
  onDismiss: () => void;
}

/**
 * M143 (docs/09 §Candidate picker): a collision row shows CURRENCY, not just
 * a venue — `VGRO (NYSEArca, USD)` vs `VGRO.TO (Toronto, CAD)` is the entire
 * question, and a bare ticker's venue code alone does not answer it. The
 * picker is presentational: `onPick` hands the full listing straight back
 * (docs/07 §Candidate resolution), so picking never re-asks the resolver.
 */
export function CandidatePicker({
  typed,
  candidates,
  pending,
  onPick,
  onDismiss,
}: CandidatePickerProps) {
  return (
    <div
      data-testid="symbol-candidates"
      className="rounded-lg border border-control bg-surface-raised px-2 py-1.5"
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-[11px] uppercase tracking-widest text-muted">
          Several listings match “{typed}” — pick one
        </p>
        <button
          type="button"
          aria-label="Dismiss symbol candidates"
          data-testid="dismiss-symbol-candidates"
          onClick={onDismiss}
          className="shrink-0 leading-none text-muted transition-colors hover:text-text"
        >
          ✕
        </button>
      </div>
      <ul className="mt-1">
        {candidates.map((candidate) => (
          <li key={candidate.symbol}>
            <button
              type="button"
              disabled={pending}
              data-testid={`symbol-candidate-${candidate.symbol}`}
              onClick={() => onPick(candidate)}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm outline-none transition-colors hover:bg-accent/10 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
            >
              <span className="font-mono font-medium">{candidate.symbol}</span>
              <span className="min-w-0 flex-1 truncate text-muted">
                {listingLabel(candidate, candidate.listing_currency)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
