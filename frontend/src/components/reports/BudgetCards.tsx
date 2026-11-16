import { useQuery } from "@tanstack/react-query";
import { listBudgets } from "../../api/budgets";
import { Card } from "../ui/Card";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { HelpHint } from "../ui/HelpHint";
import { budgetView, hasCarry, CARRY_HINT, LIMIT_HINT } from "../../lib/budgetCarry";
import { motion } from "framer-motion";
import { fadeUp } from "../ui/motion";
import { Wallet } from "lucide-react";
import { useMasked } from "../../lib/mask";
import { formatCad } from "../../lib/format";

/** Whole-dollar beside a progress bar (docs/09 money displays). */
const WHOLE = { fractionDigits: 0 } as const;

function pctColor(pct: number | null) {
  // `null` = a carried debt swallowed the pool (docs/09) — the most over
  // state there is, so it takes the danger tone like a 100%+ ratio does.
  if (pct === null || pct >= 100) return "bg-danger";
  if (pct >= 80) return "bg-warn";
  // docs/09 line 22: green (<80%) — `gain` is the semantic green (AA both themes).
  return "bg-gain";
}

export function BudgetCards() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["budgets"],
    queryFn: () => listBudgets(),
  });

  if (isLoading) return <SkeletonRows rows={3} />;
  if (error) return <QueryErrorPanel query={{ error, isFetching, refetch }} />;
  if (!data || data.length === 0) {
    return (
      <EmptyState
        icon={<Wallet />}
        title="No budgets set"
        hint="Set monthly spending limits on the Budgets page to track your budget."
        preview={{ src: "/help/budgets.png", alt: "The Budgets page with category budgets and their fill bars" }}
        action={{ label: "Add a budget", to: "/budgets" }}
      />
    );
  }

  // AGENTS §6 (code trap): the budget cards must NOT be written as
  // `<motion.div {...fadeUp}>` — that spreads `hidden`/`show` as DOM
  // attributes, and `hidden` is the global HTML attribute, so every card
  // renders `display:none` and the whole Budget progress section vanishes.
  // Variants travel via `variants` + initial/animate.
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {data.map((b) => {
        // M110 (docs/09 §Budget carry): assigned / carried-in / spendable stay
        // separate lines — a debt is never folded into the limit it came from.
        const view = budgetView(b);
        const showCarry = hasCarry(view);
        const pct = b.pct_used;
        return (
        <motion.div key={b.id} variants={fadeUp} initial="hidden" animate="show">
          <Card className="p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium text-text">{b.category_name}</span>
              <span className="text-xs text-muted">{b.period}</span>
            </div>
            <div className="flex items-baseline gap-2 mb-1">
              <span className="text-lg font-semibold text-text">{formatCad(b.spent, undefined, WHOLE)}</span>
              <span className="text-sm text-muted">/ {formatCad(view.pool.toFixed(2), undefined, WHOLE)}</span>
              {showCarry && <span className="text-xs text-muted">spendable</span>}
            </div>
            {showCarry && view.carried !== null && (
              <p className={`mb-1 text-xs ${view.debt ? "text-danger" : "text-gain"}`}>
                {view.debt ? "−" : "+"}{formatCad(Math.abs(view.carried).toFixed(2), undefined, WHOLE)} carried in
                {view.debt ? " — debt to pay down" : ""}
              </p>
            )}
            <div className="w-full h-2 rounded-full bg-surface-raised overflow-hidden">
              <div
                className={`h-full rounded-full transition-all ${pctColor(pct)}`}
                style={{ width: `${Math.min(pct ?? 100, 100)}%` }}
              />
            </div>
            <div className="flex justify-between mt-1">
              <span className="text-xs text-muted">
                {pct === null ? (
                  <span className="text-danger">over budget</span>
                ) : (
                  <>
                    {Math.round(pct)}% used
                    {/* M65 honest-%: the label is the TRUE ratio (201.94% prints as
                        202%); only the bar clamps — which is exactly what the raw
                        percentage alone cannot tell you. */}
                    <HelpHint text={showCarry ? CARRY_HINT : LIMIT_HINT} />
                  </>
                )}
              </span>
              <span className="text-xs text-muted">{formatCad(b.remaining, undefined, WHOLE)} left</span>
            </div>
          </Card>
        </motion.div>
        );
      })}
    </div>
  );
}
