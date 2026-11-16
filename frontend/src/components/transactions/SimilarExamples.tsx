// SimilarExamples (docs/04): fixed-position popover listing server-ranked
// past entries matching a transaction's payee. Clicking a row applies its
// category via PATCH /transactions/{id}. Deterministic display aid for
// manual categorization — never auto-applies. Rendered with
// `position: fixed` so it escapes the table's overflow-x-auto clipping.
//
// M137 (docs/33 §Server-first computing): ONE query to the re-mounted
// `GET /transactions/similar` replaces the full-ledger download this popover
// used to rank in the browser (the 285-line `lib/searchMatch` port). The
// database owns the matcher — pg_trgm at 0.3, `score desc, [categorized
// first,] id desc` — so the popover answers from the same predicate
// `services/categorization` scores with. `lib/searchMatch.ts` stays as the
// E2EE/offline fallback (P2-5 retires it, not this card).
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, LoaderCircle, X } from "lucide-react";
import { getSimilarTransactions, updateTransaction } from "../../api/transactions";
import { VaultLocked } from "../e2ee/VaultLocked";
import { useVaultUnlocked } from "../e2ee/useUnlock";
import { apiErrorMessage, formatMoney } from "./format";
import { useToast } from "../ui/Toast";
import { useDialogFocus } from "../ui/dialogA11y";
import { useMasked } from "../../lib/mask";

const PANEL_WIDTH = 288; // px — 18rem

export interface SimilarAnchor {
  /** Transaction whose payee seeds the query; excluded from its own results. */
  id: number;
  payee: string;
  /** Viewport coords of the triggering button (fixed positioning anchor). */
  left: number;
  top: number;
}

interface SimilarExamplesProps {
  anchor: SimilarAnchor;
  onClose: () => void;
}

export function SimilarExamples({ anchor, onClose }: SimilarExamplesProps) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const panelRef = useRef<HTMLDivElement>(null);
  useDialogFocus(panelRef);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();

  // M136 rule 7c: the lock state is IN the query key — unlocking inline
  // re-runs the server query without a reload, and a locked answer (withheld
  // rows) is never reused after the unlock.
  const vaultUnlocked = useVaultUnlocked();
  const similar = useQuery({
    queryKey: ["transactions", "similar", anchor.id, anchor.payee, vaultUnlocked],
    queryFn: () =>
      getSimilarTransactions({
        q: anchor.payee,
        excludeId: anchor.id,
        // M119: a categorized sibling ranks ahead of an EQUAL-SCORING
        // uncategorized one (server tiebreak), and `categorizedOnly` stays
        // OFF — nine identical payees with one category is not "no examples".
        categorizedFirst: true,
        hideEncrypted: !vaultUnlocked,
        limit: 10,
      }),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const items = similar.data?.items ?? [];

  // M119 honesty fix: a LOCKED vault withholds cipher payees (the server
  // reports the count in `withheld`), so an empty result may mean "hidden",
  // not "none". M136 routes that claim through the shared component so the
  // popover can no longer invent its own wording (docs/33 rule 7f). Non-E2EE
  // tenants have no cipher rows → withheld is 0 → no lock copy (rule 7a).
  const lockedOut = (similar.data?.withheld ?? 0) > 0;

  const apply = useMutation({
    mutationFn: ({ id, category_id }: { id: number; category_id: number }) =>
      updateTransaction(id, { category_id }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["suggestions"] });
      toast.success("Categorized from past example");
      onClose();
    },
    onError: (err) => toast.error("Category update failed", apiErrorMessage(err)),
  });

  // Close on Escape and outside pointer-down like every other surface dialog.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    function onPointerDown(event: MouseEvent) {
      if (!panelRef.current?.contains(event.target as Node | null)) onClose();
    }
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onPointerDown);
    };
  }, [onClose]);

  // Keep the panel inside the viewport when the row sits at the right edge.
  const left = Math.min(anchor.left, window.innerWidth - PANEL_WIDTH - 12);

  return createPortal(
    <div
      ref={panelRef}
      role="dialog"
      aria-label="Similar past entries"
      tabIndex={-1}
      className="glass fixed z-50 rounded-xl border border-border p-2 shadow-glow-sm outline-none"
      style={{ left, top: anchor.top + 6, width: PANEL_WIDTH }}
    >
      <div className="flex items-center gap-2 px-2 pb-1.5 pt-1">
        <History className="size-3.5 text-accent" aria-hidden />
        <span className="text-[11px] font-medium uppercase tracking-widest text-muted">
          Similar past entries
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="ml-auto rounded-md p-0.5 text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </div>

      {similar.isPending && (
        <p className="flex items-center gap-2 px-2 py-3 text-xs text-muted">
          <LoaderCircle className="size-3.5 animate-spin" aria-hidden /> Searching…
        </p>
      )}
      {similar.isError && (
        <p className="px-2 py-3 text-xs text-danger">{apiErrorMessage(similar.error)}</p>
      )}
      {/* docs/33 rule 7f: the lock hint REPLACES the empty state, it never
          stacks with it — and an unlocked vault says the true thing. */}
      {!similar.isPending && !similar.isError && items.length === 0 && lockedOut && (
        <VaultLocked variant="blocked" capability="search" className="px-2 py-3" />
      )}
      {!similar.isPending && !similar.isError && items.length === 0 && !lockedOut && (
        <p className="px-2 py-3 text-xs text-muted">No similar past entries</p>
      )}

      {!similar.isPending && !similar.isError && items.length > 0 && (
        <>
        <ul className="flex flex-col">
          {items.map((item) => {
            // Captured: TS drops property narrowing inside the onClick
            // closure, and the apply path must take a `number`, never null.
            const categoryId = item.category_id;
            const category = item.category_name;
            const body = (
              <>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">
                    {item.normalized_payee}
                  </span>
                  <span className="block text-xs text-muted tabular">{item.date}</span>
                </span>
                <span className="shrink-0 text-xs tabular">
                  {formatMoney(item.amount_native, item.currency)}
                </span>
                <span className="max-w-20 shrink-0 truncate rounded-md bg-accent/15 px-1.5 py-0.5 text-[11px] text-accent">
                  {category ?? "Uncategorized"}
                </span>
              </>
            );
            // M119: an uncategorized sibling has no category to apply, so it
            // is an inert row — never a button that swallows the click. The
            // one-click apply path below is unchanged for the rows that have
            // one (the only kind this popover used to render).
            if (categoryId === null) {
              return (
                <li key={item.id}>
                  <div
                    title="No category to apply yet"
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5"
                  >
                    {body}
                  </div>
                </li>
              );
            }
            return (
              <li key={item.id}>
                <button
                  type="button"
                  disabled={apply.isPending}
                  onClick={() => apply.mutate({ id: anchor.id, category_id: categoryId })}
                  title={`Apply “${category ?? "Uncategorized"}”`}
                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {body}
                </button>
              </li>
            );
          })}
        </ul>
        <button
          type="button"
          onClick={() => {
            onClose();
            navigate(`/transactions?q=${encodeURIComponent(anchor.payee)}`);
          }}
          className="mt-1 w-full rounded-lg border-t border-veil/[0.06] px-2 pt-2 text-left text-xs text-accent outline-none transition-colors hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
        >
          See all similar →
        </button>
        </>
      )}
    </div>,
    document.body,
  );
}
