// Accounts index — `/accounts` (M100, docs/09 §Pages + docs/14 C10).
//
// WHY THIS PAGE EXISTS: the collapsed rail's icon labelled "Accounts" pointed
// at `/settings` since M21 (the spec even pinned that verbatim), so Mark
// clicked Accounts and landed on Settings' Security card. The PM ruling on
// M100 was Option A — a real destination whose name equals its label — so this
// is that destination: the sidebar's OWN grouping (shared `TYPE_GROUPS`, so
// the rail and the page can never drift) with the same balances the rail
// shows, each row opening the existing `/accounts/:accountId` detail page.
//
// Money note (AGENTS §2): every figure is one account's `total_value` in its
// OWN currency, formatted by `formatMoney` — no cross-currency summing
// anywhere on this page, so no FX rate is needed and none is fetched.
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Wallet } from "lucide-react";
import { Link } from "react-router-dom";
import { listAccounts } from "../api/accounts";
import { apiErrorMessage } from "../components/transactions/format";
import { Card } from "../components/ui/Card";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { TYPE_GROUPS, TYPE_LABELS } from "../lib/accountGroups";
import { formatMoney } from "../lib/format";
import { useMasked } from "../lib/mask";

export function Accounts() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  // Same key as the sidebar and the Settings manager, so the three surfaces
  // share ONE request and one invalidation (`["accounts"]`).
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => listAccounts(200),
  });

  const items = accounts.data?.items ?? [];

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">Accounts</h1>
          <p className="mt-1 text-sm text-muted">
            Every account, grouped by type, with its current balance — cash plus
            positions, in the account&apos;s own currency. Select one for its
            ledger, transactions and holdings.
          </p>
        </div>
        {items.length > 0 && (
          <Link
            to="/settings"
            className="inline-flex min-h-11 shrink-0 items-center gap-2 rounded-lg border border-control px-3 text-sm font-medium text-text outline-none transition-colors hover:border-accent/70 focus-visible:ring-2 focus-visible:ring-accent"
          >
            Add account
          </Link>
        )}
      </div>

      {accounts.isPending && <SkeletonRows rows={5} />}

      {accounts.isError && <ErrorPanel message={apiErrorMessage(accounts.error)} />}

      {/* Empty — the standing three-part contract (docs/09 §Empty-state
          choreography): what lives here / a real capture of this page with
          data / the one action that fills it (Settings hosts the create form,
          docs/14 C2). */}
      {accounts.data !== undefined && items.length === 0 && (
        <EmptyState
          icon={<Wallet />}
          title="No accounts yet"
          hint="An account holds its own transactions, balances and positions. Add one to start tracking, or import a statement and its accounts come with it."
          preview={{
            src: "/help/accounts.png",
            alt: "The Accounts page with six accounts grouped into Cash, Credit, Registered and Non-registered, each row showing its current balance",
          }}
          action={{ label: "Add an account", to: "/settings" }}
        />
      )}

      {items.length > 0 && (
        <div className="space-y-4">
          {TYPE_GROUPS.map((group) => {
            const groupAccounts = items.filter((account) =>
              group.types.includes(account.type),
            );
            if (groupAccounts.length === 0) return null;
            return (
              <Card key={group.label} className="p-4">
                <div className="mb-2 flex items-baseline justify-between gap-3">
                  <h2 className="text-sm font-semibold tracking-wide text-text">
                    {group.label}
                  </h2>
                  <span className="text-xs text-muted">
                    {groupAccounts.length}{" "}
                    {groupAccounts.length === 1 ? "account" : "accounts"}
                  </span>
                </div>
                <ul className="flex flex-col gap-0.5">
                  {groupAccounts.map((account) => (
                    <li key={account.id}>
                      <Link
                        to={`/accounts/${account.id}`}
                        className="group flex items-center gap-3 rounded-lg px-2 py-2.5 text-sm outline-none transition-colors duration-200 hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        <Wallet
                          className="size-4 shrink-0 text-muted transition-colors group-hover:text-accent"
                          aria-hidden
                        />
                        <span className="min-w-0 flex-1 truncate text-text">
                          {account.name}
                        </span>
                        <span className="hidden shrink-0 text-xs text-muted sm:block">
                          {TYPE_LABELS[account.type]}
                        </span>
                        <span className="shrink-0 text-sm tabular-nums text-text">
                          {formatMoney(account.total_value, account.currency)}
                        </span>
                        <ChevronRight
                          className="size-4 shrink-0 text-muted opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
                          aria-hidden
                        />
                      </Link>
                    </li>
                  ))}
                </ul>
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}
