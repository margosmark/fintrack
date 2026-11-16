import { useEffect, useId, useRef, useState } from "react";
import type {
  Security,
  SecurityInput,
  SecurityLookupCandidate,
  SecurityLookupResult,
} from "../../api/securities";
import { candidateListing, listingLabel, resolvedListing } from "../../api/securities";
import { inputClass } from "../ui/formStyles";

// Searchable security picker (W11 pin 6): combobox over listSecurities with
// case-insensitive symbol+name substring matching; rows render
// `SYMBOL — name (currency)`. Used by the trade form and the watchlist add.
//
// M186 (docs/09 §Watchlist ticker search): the watchlist add ALSO reaches the
// market when NO stored security matches. `onSearchMarket` is the probe
// (600 ms after the query settles, once per settled value, ≥2 characters,
// local matches empty ONLY) and `onPickMarket` hands back the create-ready
// listing. Both props are optional — a picker without them (the trade form,
// deliberately an existing-securities picker) behaves exactly as before.

// Search input: shared base + full width + disabled affordance.
const FIELD = `${inputClass} w-full disabled:cursor-not-allowed disabled:opacity-40`;

// Every option row is this shape; a selected row only adds its highlight.
const OPTION =
  "block w-full truncate px-2.5 py-1.5 text-left text-sm transition-colors hover:bg-accent/10";

/** One market answer, remembered under the query it was about (M186). */
type MarketState =
  | { term: string; status: "pending" }
  | { term: string; status: "miss" }
  | { term: string; status: "found"; result: SecurityLookupResult }
  | { term: string; status: "ambiguous"; candidates: SecurityLookupCandidate[] };

interface SecurityPickerProps {
  securities: Security[];
  /** Selected security id as a string ("" = none), matching form state. */
  value: string;
  onChange: (securityId: string) => void;
  placeholder?: string;
  /**
   * M186 market fallback — called with the settled, upper-cased query only
   * when zero STORED securities matched (docs/09 §Watchlist ticker search).
   * Absent = local-only, the trade form's deliberate behaviour.
   */
  onSearchMarket?: (query: string) => Promise<SecurityLookupResult>;
  /** A market row was picked — that listing does not exist locally yet. */
  onPickMarket?: (listing: SecurityInput) => void;
}

export function SecurityPicker({
  securities,
  value,
  onChange,
  placeholder = "Search symbol or name…",
  onSearchMarket,
  onPickMarket,
}: SecurityPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [market, setMarket] = useState<MarketState | null>(null);
  const probedRef = useRef<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  // M186 (docs/09 §Watchlist ticker search — combobox contract): the expanded
  // combobox must name the node it controls (axe `aria-required-attr`, the
  // same shape TagPicker ships), and that node must EXIST — so with no
  // options the popup is a plain list carrying the id, not a listbox with
  // nothing in it (axe `aria-required-children`).
  const listId = useId();

  const selected = securities.find((s) => String(s.id) === value) ?? null;

  const q = query.trim().toLowerCase();
  const matches =
    q === ""
      ? securities
      : securities.filter(
          (s) => s.symbol.toLowerCase().includes(q) || s.name.toLowerCase().includes(q),
        );

  // M186: one debounced probe per settled value — the M142 add-form cadence,
  // but ONLY when the stored list could not answer (a local hit is the
  // answer) and never for a single character. `probedRef` keeps a re-typed
  // value from becoming a second request while the same string is in flight.
  useEffect(() => {
    if (onSearchMarket === undefined) return;
    const term = query.trim().toLowerCase();
    if (term.length < 2 || matches.length > 0) return;
    if (probedRef.current === term) return;
    const timer = window.setTimeout(() => {
      probedRef.current = term;
      setMarket({ term, status: "pending" });
      onSearchMarket(term.toUpperCase())
        .then((result) => {
          // A LATER probe owns the field: drop the answer that lost the race.
          if (probedRef.current !== term) return;
          setMarket(
            result.match === "ambiguous"
              ? { term, status: "ambiguous", candidates: result.candidates }
              : { term, status: "found", result },
          );
        })
        .catch(() => {
          if (probedRef.current !== term) return;
          // docs/07: a miss is a STATE (404 lookup_unavailable, 429
          // rate_limited, offline), never an error banner in a half-typed
          // field — the no-match row keeps its honest copy below.
          setMarket({ term, status: "miss" });
        });
    }, 600);
    return () => window.clearTimeout(timer);
  }, [query, matches.length, onSearchMarket]);

  const close = () => {
    setOpen(false);
    setQuery("");
  };

  // The fallback renders only while it is still the answer for the CURRENT
  // query and the stored list still matches nothing (M186): an answer for a
  // string the user has edited away is never shown.
  const marketActive =
    market !== null &&
    market.term === q &&
    q.length >= 2 &&
    matches.length === 0 &&
    onPickMarket !== undefined
      ? market
      : null;
  const marketListing =
    marketActive !== null && marketActive.status === "found"
      ? resolvedListing(marketActive.result)
      : null;
  /** How many MARKET rows the fallback currently offers (0 = nothing new). */
  const marketOptionCount =
    marketActive === null
      ? 0
      : marketActive.status === "ambiguous"
        ? marketActive.candidates.length
        : marketActive.status === "found" && marketListing !== null
          ? 1
          : 0;
  // "no matching securities" is withheld exactly while the market rows or the
  // pending line show.
  const marketRowsShown = marketOptionCount > 0 || marketActive?.status === "pending";
  /** A `role="listbox"` must only exist when it can own an option (axe). */
  const hasOptions = matches.length + marketOptionCount > 0;

  const pickMarket = (listing: SecurityInput) => {
    onPickMarket?.(listing);
    close();
  };

  return (
    <div ref={containerRef} className="relative" data-testid="security-picker">
      <input
        role="combobox"
        aria-expanded={open}
        // M186 combobox contract (docs/09): an EXPANDED combobox names the node
        // it controls, and that node always exists (the pending/no-match popup
        // carries the id instead of a listbox that holds no option).
        aria-controls={open ? listId : undefined}
        aria-autocomplete="list"
        aria-label="Security"
        autoComplete="off"
        value={open ? query : selected === null ? "" : `${selected.symbol} — ${selected.name} (${selected.listing_currency})`}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Escape") close();
        }}
        onBlur={(e) => {
          if (!containerRef.current?.contains(e.relatedTarget as Node | null)) close();
        }}
        placeholder={placeholder}
        className={FIELD}
      />
      {open && (
        <ul
          id={listId}
          role={hasOptions ? "listbox" : undefined}
          aria-label={hasOptions ? "Securities" : undefined}
          className="absolute z-20 mt-1 max-h-56 w-full overflow-y-auto rounded-lg border border-control bg-surface-raised py-1 shadow-card"
          onMouseDown={(e) => e.preventDefault()}
        >
          {matches.map((s) => (
            <li key={s.id} role="presentation">
              <button
                type="button"
                role="option"
                aria-selected={String(s.id) === value}
                data-testid={`security-option-${s.symbol}`}
                onClick={() => {
                  onChange(String(s.id));
                  close();
                }}
                className={`${OPTION} ${String(s.id) === value ? "bg-accent/10 text-accent" : ""}`}
              >
                <span className="font-medium">{s.symbol}</span>
                <span className="text-muted"> — {s.name} ({s.listing_currency})</span>
              </button>
            </li>
          ))}
          {marketActive !== null && marketActive.status === "pending" && (
            <li
              className="px-2.5 py-1.5 text-xs text-muted"
              data-testid="security-market-searching"
            >
              Searching the market…
            </li>
          )}
          {marketActive !== null && marketActive.status === "found" && marketListing !== null && (
            <li role="presentation">
              <button
                type="button"
                role="option"
                aria-selected={false}
                data-testid={`security-market-option-${marketActive.result.symbol}`}
                onClick={() => pickMarket(marketListing)}
                className={OPTION}
              >
                <span className="font-medium">{marketActive.result.symbol}</span>
                <span className="text-muted">
                  {" — "}
                  {listingLabel(marketActive.result, marketActive.result.listing_currency)}
                </span>
                <span className="text-xs text-muted"> · from market</span>
              </button>
            </li>
          )}
          {marketActive !== null &&
            marketActive.status === "ambiguous" &&
            marketActive.candidates.map((candidate) => (
              <li key={candidate.symbol} role="presentation">
                <button
                  type="button"
                  role="option"
                  aria-selected={false}
                  data-testid={`security-market-candidate-${candidate.symbol}`}
                  onClick={() => pickMarket(candidateListing(candidate))}
                  className={OPTION}
                >
                  <span className="font-medium">{candidate.symbol}</span>
                  <span className="text-muted">
                    {" — "}
                    {listingLabel(candidate, candidate.listing_currency)}
                  </span>
                  <span className="text-xs text-muted"> · from market</span>
                </button>
              </li>
            ))}
          {matches.length === 0 && !marketRowsShown && (
            <li className="px-2.5 py-1.5 text-xs text-muted" data-testid="security-no-match">
              No matching securities.
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
