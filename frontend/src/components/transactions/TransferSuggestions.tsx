// TransferSuggestions: reusable panel showing unpaired transaction pairs
// with accept/dismiss actions. Used by AccountDetail page.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeftRight, Check, X } from "lucide-react";
import { acceptTransferSuggestion } from "../../api/transactions";
import type { TransferSuggestion } from "../../api/transactions";
import { formatMoney } from "./format";
import { useToast } from "../ui/Toast";
import { useMasked } from "../../lib/mask";

export function TransferSuggestions({
  suggestions,
  accountCurrency,
}: {
  suggestions: TransferSuggestion[];
  accountCurrency: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const accept = useMutation({
    mutationFn: (s: TransferSuggestion) => acceptTransferSuggestion(s.source_id, s.candidate_id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transfer-suggestions"] });
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      toast.success("Transfer linked");
    },
    onError: (err) => toast.error("Link failed", err instanceof Error ? err.message : "Unknown error"),
  });

  if (suggestions.length === 0) {
    return <p className="text-sm text-muted">No transfer suggestions found</p>;
  }

  return (
    <div className="space-y-3">
      {suggestions.map((s) => (
        <div
          key={`${s.source_id}-${s.candidate_id}`}
          className="flex items-center gap-3 rounded-lg border border-border p-3"
        >
          <ArrowLeftRight className="size-4 shrink-0 text-accent" aria-hidden />
          {/* ONE grid holds BOTH rows, on one shared track (grid-cols-1), so
              every amount / date / account / payee line of the pair starts at
              the same x — that shared track is what killed the staircase the
              two self-contained inline flex rows used to print.

              Why one column instead of amount|date|account side by side: this
              card's own probe measured the content box at 92 / 128 / 158 / 188
              px (320 / 360 / 390 / 420 — 160 px in the desktop sidebar)
              against 77 + 82 + 119 px of amount, date and a REALISTIC account
              label plus gaps: 294 px. Side-by-side columns fit at no
              supported width, which is exactly why the old markup wrapped
              "(CIBC" / "Chequing)" mid-label, stacked the date "2026-" /
              "09-12" and pushed the card 14 px past the 360 px viewport.
              Stacked on one track, all four lines fit (or ellipsize) at every
              width: `truncate` is overflow:hidden, so a pathological value
              clips instead of shoving the card wide or colliding with the
              buttons, which stay a shrink-0 sibling of the grid. */}
          <div className="grid min-w-0 flex-1 grid-cols-1 gap-y-1 text-sm">
            <SuggestionSide
              side="source"
              amount={s.source_amount}
              date={s.source_date}
              account={s.source_account_name}
              payee={s.source_payee}
              currency={accountCurrency}
            />
            <SuggestionSide
              side="candidate"
              amount={s.candidate_amount}
              date={s.candidate_date}
              account={s.candidate_account_name}
              payee={s.candidate_payee}
              currency={accountCurrency}
              topGap
            />
          </div>
          <div className="flex shrink-0 gap-1">
            <button
              type="button"
              disabled={accept.isPending}
              onClick={() => accept.mutate(s)}
              className="rounded-lg bg-accent/10 p-1.5 text-accent transition-colors hover:bg-accent/20 disabled:opacity-40"
              title="Accept transfer suggestion"
            >
              <Check className="size-4" aria-hidden />
            </button>
            <button
              type="button"
              onClick={() => {/* client-side dismiss — no persistence per plan */}}
              className="rounded-lg p-1.5 text-muted transition-colors hover:bg-veil/[0.06] hover:text-text"
              title="Dismiss"
            >
              <X className="size-4" aria-hidden />
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

/** One side of the pair: amount, date, account, description — always in that
 * order, always on the pair's single shared track. `data-side`/`data-cell`
 * are what the regression test and the browser probe key on. */
function SuggestionSide({
  side,
  amount,
  date,
  account,
  payee,
  currency,
  topGap = false,
}: {
  side: "source" | "candidate";
  amount: string;
  date: string;
  account: string | null;
  payee: string;
  currency: string;
  topGap?: boolean;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const description = payee.trim();
  return (
    <>
      <span
        data-side={side}
        data-cell="amount"
        className={`truncate font-medium tabular-nums${topGap ? " mt-1.5" : ""}`}
      >
        {formatMoney(amount, currency)}
      </span>
      <span data-side={side} data-cell="date" className="truncate text-muted">
        {date}
      </span>
      {account && (
        <span data-side={side} data-cell="account" className="truncate text-xs text-muted">
          ({account})
        </span>
      )}
      {/* A payee of "-" (or blank) is a real value from the data: it means
          "no description". Printing it rendered a stray truncated dash line
          above the amount, so the line is omitted instead. */}
      {description !== "" && description !== "-" && (
        <span data-side={side} data-cell="payee" className="truncate text-xs text-muted">
          {payee}
        </span>
      )}
    </>
  );
}
