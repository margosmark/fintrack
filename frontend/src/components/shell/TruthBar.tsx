// TruthBar (M185, docs/47 §1.3) — the persistent trust line directly under
// the TopBar (mobile: the single truncated line under the appbar).
//
// BOUNDED BY THE PAYLOAD: every segment renders only when a real field backs
// it, and the bar never prints a vendor name (the market-data vendor renders
// as "Market data" via sourceLabel) or a "last updated" time derived from the
// wall clock. Forbidden here and everywhere: `cache yfinance 1d`, `BoC`/vendor
// names not present in the payload, and any number the payload does not carry
// (docs/47 §1.3).
//
//   Active range   the page's own from/to state (PageMeta) — money pages only
//   FX             GET /fx/status via FxStalenessChip — only when pairs exist
//   Prices         the page's payload: newest price_date + price_source
//   Privacy        the mask store — always ("Amounts masked"/"visible")
//   Read-only demo isReadOnlySession(user) — `demo-banner`, role=status
//
// Semantics (docs/47 §6.3): plain text, NOT a landmark; the demo banner inside
// it is `role="status"`. Below 768px one truncated line (range + privacy +
// demo) with the full text in a Details popover — the ruling on PROPOSAL
// open question 2.

import { useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import { isReadOnlySession } from "../../auth/readOnlySession";
import { useMasked } from "../../lib/mask";
import { usePageMeta } from "../../lib/pageMeta";
import { sourceLabel } from "../charts/format";
import { FxStalenessChip } from "../ui/FxStalenessChip";

/** "Oct 1 – Oct 13, 2026" from two payload dates. No clock anywhere. */
function fmtRange(from: string, to: string): string {
  const fmt = (iso: string, withYear: boolean): string => {
    const date = new Date(`${iso}T00:00:00`);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleDateString("en-CA", {
      month: "short",
      day: "numeric",
      ...(withYear ? { year: "numeric" } : {}),
    });
  };
  const sameYear = from.slice(0, 4) === to.slice(0, 4);
  return `${fmt(from, !sameYear)} – ${fmt(to, true)}`;
}

function Sep() {
  return (
    <span aria-hidden className="text-muted">
      ·
    </span>
  );
}

export function TruthBar() {
  const { user } = useAuth();
  const masked = useMasked();
  const { range, prices } = usePageMeta();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const demo = isReadOnlySession(user);

  const privacy = masked ? "Amounts masked" : "Amounts visible";
  const priceLine =
    prices?.price_date !== null && prices?.price_date !== undefined
      ? `Prices as of ${prices.price_date}${
          prices.price_source !== null && prices.price_source !== ""
            ? ` · ${sourceLabel(prices.price_source)}`
            : ""
        }`
      : null;
  const rangeLine = range !== null ? fmtRange(range.from, range.to) : null;
  const fxLine = <FxStalenessChip />;

  const fullText = [rangeLine, priceLine, privacy].filter(Boolean).join(" · ");

  return (
    <div
      data-testid="truth-bar"
      className="h-8 border-b border-rule bg-surface px-4 text-xs text-muted md:h-9 md:px-6"
    >
      <div className="mx-auto flex h-full max-w-[1344px] items-center gap-2.5 overflow-hidden">
        {rangeLine !== null && (
          <span className="truncate font-medium text-text">{rangeLine}</span>
        )}
        {priceLine !== null && (
          <>
            <Sep />
            <span className="hidden truncate md:inline">{priceLine}</span>
          </>
        )}
        <span className="hidden md:inline">{fxLine}</span>
        <Sep />
        <span className="truncate">{privacy}</span>
        {demo && (
          <>
            <Sep />
            <span
              role="status"
              data-testid="demo-banner"
              className="truncate font-medium text-warn"
            >
              Read-only demo — changes are not saved
            </span>
          </>
        )}
        {/* Mobile: the full line lives behind one Details control (docs/47
            §1.3) — the visible bar stays a single truncated row. */}
        <span className="ml-auto shrink-0 md:hidden">
          <button
            type="button"
            data-testid="truth-details"
            aria-expanded={detailsOpen}
            aria-controls="truth-details-panel"
            onClick={() => setDetailsOpen((open) => !open)}
            className="h-6 rounded border border-control px-2 text-[11px] text-text outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Details
          </button>
          {detailsOpen && (
            <div
              id="truth-details-panel"
              data-testid="truth-details-panel"
              className="absolute left-4 right-4 top-[calc(3.5rem_+_2rem)] z-50 rounded-lg border border-rule bg-surface-raised p-3 text-xs shadow-glow-sm"
            >
              <p className="font-medium text-text">Truth bar details</p>
              <p className="mt-1.5">{fullText}</p>
              <p className="mt-1">{fxLine}</p>
              <p className="mt-1 text-muted">
                Every figure is a server-computed value rendered verbatim;
                nothing here is derived from your device clock.
              </p>
            </div>
          )}
        </span>
      </div>
    </div>
  );
}
