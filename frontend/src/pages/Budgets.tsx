import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Wallet, Plus, Pencil, Repeat, Trash2 } from "lucide-react";
import { listBudgets, createBudget, updateBudget, deleteBudget, getRecurringSummary } from "../api/budgets";
import type { BudgetWithSpent } from "../api/budgets";
import { provenance, readPrefill, type SearchCriteria } from "../lib/createFromSearch";

import { listCategories } from "../api/categories";
import { Button } from "../components/ui/Button";
import { HelpHint } from "../components/ui/HelpHint";
import { Card } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { budgetView, hasCarry, CARRY_HINT, CARRY_TOGGLE_HINT, LIMIT_HINT } from "../lib/budgetCarry";
import { useToast } from "../components/ui/Toast";
import { apiErrorMessage } from "../components/transactions/format";
import { useMasked } from "../lib/mask";
import { formatCad } from "../lib/format";

/** Whole-dollar beside a progress bar (docs/09 money displays). */
const WHOLE = { fractionDigits: 0 } as const;

export function Budgets() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const [showForm, setShowForm] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [catId, setCatId] = useState("");
  const [amount, setAmount] = useState("");
  const [period, setPeriod] = useState("monthly");
  const [searchParams, setSearchParams] = useSearchParams();
  // ── M123 C4 (docs/09 §Create from search) ─────────────────────────────────
  /** Read once per mount — a later param change must not clobber a live draft. */
  const prefillRead = useRef(false);
  const [prefill, setPrefill] = useState<SearchCriteria | null>(null);
  useEffect(() => {
    if (prefillRead.current) return;
    prefillRead.current = true;
    const criteria = readPrefill(searchParams);
    if (criteria === null) return;
    // A budget is a per-category amount, so `uncategorized` cannot be one —
    // `provenance` below says so instead of quietly selecting nothing.
    if (criteria.category !== undefined && criteria.category !== "uncategorized") {
      setCatId(criteria.category);
    }
    // "The current month as period": a MONTHLY budget IS the current month —
    // there is no date field to fill, so this is the whole of that sentence.
    setPeriod("monthly");
    setPrefill(criteria);
    setShowForm(true);
  }, [searchParams]);

  const budgets = useQuery({ queryKey: ["budgets"], queryFn: listBudgets });
  const categories = useQuery({ queryKey: ["categories"], queryFn: () => listCategories(200) });

  /** Computed at render so the category name is right even if it loads late. */
  const prefillProvenance =
    prefill === null
      ? null
      : provenance(prefill, "budget", (value) =>
          value === "uncategorized"
            ? "Uncategorized"
            : ((categories.data?.items ?? []).find((c) => String(c.id) === value)?.name ??
              `category ${value}`),
        );
  // M96 B2 (docs/09 §Recurring prompt): a LABEL — recurring-tagged rows
  // normalised to a month. Still a label, never part of the progress math.
  //
  // M137 P0-2 (docs/33 §Server-first computing): it used to earn that number
  // by downloading EVERY committed row through `loadCommittedTransactions`.
  // `GET /budgets/recurring-summary` answers the same `{count, monthly}` shape
  // from one indexed scan, so this page now shows one small `/budgets` request
  // and no `/transactions?all` at all. Keyed under ["budgets"] so a tag/re-tag
  // invalidation still reaches it.
  const committed = useQuery({
    queryKey: ["budgets", "recurring-committed"],
    queryFn: getRecurringSummary,
  });

  const create = useMutation({
    mutationFn: () => createBudget({ category_id: parseInt(catId), amount: parseFloat(amount), period }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["budgets"] }); toast.success("Budget created"); resetForm(); },
    onError: (e: Error) => toast.error("Error", e.message),
  });

  const update = useMutation({
    mutationFn: () => updateBudget(editId!, { amount: parseFloat(amount), period }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["budgets"] }); toast.success("Budget updated"); resetForm(); },
    onError: (e: Error) => toast.error("Error", apiErrorMessage(e)),
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteBudget(id),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["budgets"] }); toast.success("Budget deleted"); },
    onError: (e: Error) => toast.error("Error", apiErrorMessage(e)),
  });

  // M110 (docs/09 §Budget carry): one click, no wizard — the row's own
  // "carry leftover" switch. The carried VALUE is server-produced and frozen;
  // this only opts the category in (and back out).
  const toggleCarry = useMutation({
    mutationFn: (b: BudgetWithSpent) =>
      updateBudget(b.id, { carry: b.carry === "forward" ? "off" : "forward" }),
    onSuccess: (updated) => {
      queryClient.invalidateQueries({ queryKey: ["budgets"] });
      toast.success(`Carry leftover ${updated.carry === "forward" ? "on" : "off"}`);
    },
    onError: (e: Error) => toast.error("Error", apiErrorMessage(e)),
  });

  function resetForm() {
    setCatId("");
    setAmount("");
    setPeriod("monthly");
    setEditId(null);
    setPrefill(null);
    setShowForm(false);
    // Drop the deep-link params so a reload can never reopen a stale draft.
    setSearchParams({}, { replace: true });
  }

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">Budgets<HelpHint text="A budget tracks spending in one category for the month. Editing the amount never moves transactions." /></h1>
        <Button onClick={() => { resetForm(); setShowForm(!showForm); }}>
          <Plus className="size-4 mr-1" /> New budget
        </Button>
      </div>

      {committed.data !== undefined && committed.data.count > 0 && (
        <p className="-mt-3 text-sm text-muted">
          Committed monthly (tagged recurring):{" "}
          <span className="font-medium text-text tabular">
            {formatCad(String(committed.data.monthly), undefined, WHOLE)}
          </span>{" "}
          across {committed.data.count} tagged row{committed.data.count === 1 ? "" : "s"}
          <HelpHint text="Recurring-tagged transactions normalised to a monthly rate. This is a label — budget progress below still comes from your budget definitions." />
        </p>
      )}

      {showForm && (
        <Card className="p-4">
          {/* M123 (docs/09 §Create from search): what landed in this draft —
              and, for a budget, what a per-category amount cannot carry. */}
          {prefillProvenance !== null && (
            <p className="mb-3 text-xs text-muted" data-testid="prefill-provenance">
              {prefillProvenance}
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-3">
            <select aria-label="Budget category" value={catId} onChange={e => setCatId(e.target.value)} disabled={!!editId}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm">
              <option value="">Select category</option>
              {categories.data?.items?.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
            <input aria-label="Amount" placeholder="Amount" type="number" value={amount} onChange={e => setAmount(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm" />
            <select aria-label="Budget period" value={period} onChange={e => setPeriod(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm">
              <option value="monthly">Monthly</option>
              <option value="yearly">Yearly</option>
            </select>
          </div>
          <div className="flex gap-2 mt-3">
            <Button onClick={() => editId ? update.mutate() : create.mutate()} disabled={!catId || !amount}>
              {editId ? "Update" : "Create"}
            </Button>
            <Button variant="ghost" onClick={resetForm}>Cancel</Button>
          </div>
        </Card>
      )}

      {budgets.isPending ? <SkeletonRows rows={3} /> : budgets.data?.length === 0 ? (
        <EmptyState
          icon={<Wallet />}
          title="No budgets"
          hint="Set spending limits for your categories."
          preview={{
            src: "/help/budgets.png",
            alt: "The Budgets page with category budgets and their fill bars",
          }}
          action={{
            label: "New budget",
            onClick: () => {
              resetForm();
              setShowForm(true);
            },
            ariaLabel: "New budget — No budgets",
          }}
        />
      ) : (
        <motion.div variants={staggerContainer(0.05)} initial="hidden" animate="show" className="space-y-3">
          {budgets.data?.map(b => {
            // M110 (docs/09 §Budget carry): the row shows its LAYERS —
            // assigned, carried-in, spendable — never one merged number.
            const view = budgetView(b);
            const showCarry = hasCarry(view);
            const pct = b.pct_used;
            return (
            <motion.div key={b.id} variants={fadeUp}>
              <Card className="p-4">
                <div className="flex items-center justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-text">{b.category_name}</span>
                      <span className="text-xs text-muted">{b.period}</span>
                    </div>
                    <div className="mt-1 flex items-baseline gap-2">
                      <span className="text-lg font-semibold text-text">{formatCad(b.spent, undefined, WHOLE)}</span>
                      {/* The denominator is the spendable pool (assigned +
                          carried_in); with no carry it IS `b.amount`, so the
                          pre-M110 row is untouched. */}
                      <span className="text-sm text-muted">/ {formatCad(view.pool.toFixed(2), undefined, WHOLE)}</span>
                      {showCarry && <span className="text-xs text-muted">spendable</span>}
                    </div>
                    {showCarry && view.carried !== null && (
                      <p className={`mt-1 text-xs ${view.debt ? "text-danger" : "text-gain"}`}>
                        {view.debt ? "−" : "+"}{formatCad(Math.abs(view.carried).toFixed(2), undefined, WHOLE)} carried in
                        {view.debt ? " — a debt to pay down first" : " — extra room this period"}
                        <HelpHint className="ml-0.5" text={CARRY_HINT} />
                      </p>
                    )}
                    <div className="mt-2 w-full h-2 rounded-full bg-surface-raised overflow-hidden">
                      <div className={`h-full rounded-full transition-all ${pct === null || pct >= 100 ? "bg-danger" : pct >= 80 ? "bg-warn" : "bg-gain"}`}
                        style={{ width: `${Math.min(pct ?? 100, 100)}%` }} />
                    </div>
                    {pct === null ? (
                      <span className="text-xs text-danger">
                        over budget — the carried debt is bigger than this period's limit
                        <HelpHint className="ml-0.5" text={CARRY_HINT} />
                      </span>
                    ) : (
                      <span className="text-xs text-muted">
                        {Math.round(pct)}% used
                        {/* M65 honest-% (docs/09): true ratio, only the bar clamps. */}
                        <HelpHint className="ml-0.5" text={showCarry ? CARRY_HINT : LIMIT_HINT} />
                      </span>
                    )}
                    <div className="mt-2 flex items-center gap-2">
                      <button
                        onClick={() => toggleCarry.mutate(b)}
                        aria-pressed={b.carry === "forward"}
                        aria-label={`Carry leftover — ${b.category_name}`}
                        title={b.carry === "forward" ? "Carry leftover: on" : "Carry leftover: off"}
                        className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs ${b.carry === "forward" ? "border-gain bg-gain/20 text-gain" : "border-control text-muted hover:text-text"}`}>
                        <Repeat className="size-3" /> Carry leftover
                      </button>
                      <HelpHint text={CARRY_TOGGLE_HINT} />
                    </div>
                  </div>
                  <div className="flex gap-1 ml-4">
                    <button onClick={() => { setEditId(b.id); setCatId(String(b.category_id)); setAmount(String(b.amount)); setPeriod(b.period); setShowForm(true); }}
                      title="Edit" aria-label={`Edit budget — ${b.category_name}`}
                      className="p-1 rounded hover:bg-surface-raised"><Pencil className="size-4 text-muted" /></button>
                    <button onClick={() => remove.mutate(b.id)} title="Delete" aria-label={`Delete budget — ${b.category_name}`}
                      className="p-1 rounded hover:bg-surface-raised"><Trash2 className="size-4 text-danger" /></button>
                  </div>
                </div>
              </Card>
            </motion.div>
            );
          })}
        </motion.div>
      )}
    </section>
  );
}
