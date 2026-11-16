import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Calendar, ChevronRight, History, Plus, Pencil, Trash2, Check } from "lucide-react";
import { listCategories } from "../api/categories";
import { provenance, readPrefill, type SearchCriteria } from "../lib/createFromSearch";
import {
  BILL_MATCH_MODES,
  BILL_MATCH_MODE_LABELS,
  composeTerms,
  createBill,
  deleteBill,
  findPastForDraft,
  findPastPayments,
  listBills,
  markPaid,
  readBillAutofill,
  readLogic,
  readTerms,
  updateBill,
  updateBillRule,
  type Bill,
  type BillFindPast,
  type BillMatchLogic,
  type BillMatchMode,
} from "../api/bills";
import { MatchTermsField } from "../components/bills/MatchTermsField";
import { RuleTestPanel } from "../components/bills/RuleTestPanel";
import { useTermSuggestions } from "../components/bills/useTermSuggestions";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { Badge } from "../components/ui/Badge";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { useToast } from "../components/ui/Toast";
import { apiErrorMessage, formatMoney } from "../components/transactions/format";
import { useMasked } from "../lib/mask";

function money(v: string | null) {
  if (!v) return "—";
  const n = parseFloat(v);
  return isNaN(n) ? "—" : formatMoney(String(n), "CAD");
}

function daysUntil(dateStr: string) {
  const d = new Date(dateStr);
  const now = new Date();
  return Math.ceil((d.getTime() - now.getTime()) / (1000 * 60 * 60 * 24));
}

export function Bills() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [showForm, setShowForm] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [frequency, setFrequency] = useState("monthly");
  const [dueDate, setDueDate] = useState("");
  /** M116 D1 / M121 D1: the match rule, edited in the SAME form as the
   *  reminder. `matchTerms` are the committed chips; `termDraft` is what is
   *  still typed (composed into the payload, so it is never lost). */
  const [matchMode, setMatchMode] = useState<BillMatchMode>("contains");
  const [matchTerms, setMatchTerms] = useState<string[]>([]);
  const [termDraft, setTermDraft] = useState("");
  const [matchLogic, setMatchLogic] = useState<BillMatchLogic>("any");
  const [tolerance, setTolerance] = useState("");
  /** M55 (docs/25): subscription that seeded this draft — create-only. */
  const [fromSubscriptionId, setFromSubscriptionId] = useState<string | null>(null);
  /** M116 B1: the read-only "find past payments" preview. */
  const [preview, setPreview] = useState<BillFindPast | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  /** M123 (docs/25): the matcher's CATEGORY SCOPE, as ids for the select. */
  const [categoryId, setCategoryId] = useState("");
  // ── M123 (docs/09 §Create from search): deep-link prefill ─────────────────
  /** Read once per mount — a later param change must not clobber a live draft. */
  const prefillRead = useRef(false);
  const [prefill, setPrefill] = useState<SearchCriteria | null>(null);

  // M122 T1: suggestions from the user's OWN ledger while the form is open —
  // no bill id in create mode; in edit mode the server excludes what the
  // SAVED rule already matches.
  const suggestions = useTermSuggestions({
    enabled: showForm,
    draft: termDraft,
    billId: editId,
    matchMode,
    terms: matchTerms,
  });

  const fieldClass =
    "field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm";

  // The terms this form is ABOUT right now: the chips plus whatever is still
  // typed, falling back to the bill's name exactly as the server would
  // (docs/25 §M116 D1 — a rule always exists, and the name is its default).
  const currentTerms = (): string[] => {
    const composed = composeTerms(matchTerms, termDraft);
    if (composed.length > 0) return composed;
    return name.trim() ? [name.trim()] : [];
  };

  // Hand-off from the subscriptions / recurring surfaces:
  // `/bills?autofill=…` prefills the create form. It only ever opens or
  // overwrites the form — nothing is created until the user submits it
  // (docs/25 M55 decision 6). M116: the payee also becomes the MATCH TERM,
  // which is the "create a reminder with match_term X" adoption the milestone
  // asks the M96 group prompt for.
  useEffect(() => {
    const draft = readBillAutofill(searchParams);
    if (!draft) return;
    setName(draft.name);
    setAmount(draft.amount);
    setFrequency(draft.frequency);
    setDueDate(draft.dueDate);
    setMatchTerms(draft.matchTerm ? [draft.matchTerm] : []);
    setTermDraft("");
    setMatchLogic("any");
    setFromSubscriptionId(draft.fromSubscriptionId);
    setPreview(null);
    setPreviewOpen(false);
    setShowForm(true);
  }, [searchParams]);

  // M123 (docs/09): the SAME deep link, read once. It only ever OPENS the
  // form — nothing is created until the user submits, exactly like M55's
  // autofill above. `resetForm` drops the params, so a reload cannot reopen a
  // draft that has already been saved.
  useEffect(() => {
    if (prefillRead.current) return;
    prefillRead.current = true;
    const criteria = readPrefill(searchParams);
    if (criteria === null) return;
    setMatchMode("contains");
    setMatchTerms(criteria.q !== undefined ? [criteria.q] : []);
    setTermDraft("");
    // `amount→None`: amounts are not part of the four search params, and
    // guessing one from a filter the search never had would be worse than none.
    setAmount("");
    // `uncategorized` has no id to scope by — leave the scope blank rather
    // than pretend (`provenance` says so out loud below).
    setCategoryId(
      criteria.category !== undefined && criteria.category !== "uncategorized"
        ? criteria.category
        : "",
    );
    setPreview(null);
    setPreviewOpen(false);
    setShowForm(true);
    setPrefill(criteria);
  }, [searchParams]);

  const categories = useQuery({ queryKey: ["categories"], queryFn: () => listCategories(200) });

  /** Computed at render so the category name is right even if it loads late. */
  const prefillProvenance =
    prefill === null
      ? null
      : provenance(prefill, "bill", (value) =>
          value === "uncategorized"
            ? "Uncategorized"
            : ((categories.data?.items ?? []).find((category) => String(category.id) === value)
                ?.name ?? `category ${value}`),
        );

  const bills = useQuery({ queryKey: ["bills"], queryFn: () => listBills() });

  const create = useMutation({
    mutationFn: () =>
      createBill({
        name: name.trim(),
        amount: amount ? parseFloat(amount) : undefined,
        // M123 (docs/25): the matcher's category SCOPE. Only present when a
        // category was chosen, so an untouched form posts exactly what it did
        // before this field existed.
        ...(categoryId !== "" ? { category_id: Number(categoryId) } : {}),
        frequency,
        next_due_date: dueDate,
        from_subscription_id: fromSubscriptionId ?? undefined,
        match_mode: matchMode,
        // No terms typed at all = "derive from the name" (docs/25 §M116 D1) —
        // the chips start empty, so an untouched field must not 422 on submit.
        match_terms: currentTerms().length > 0 ? currentTerms() : undefined,
        match_logic: matchLogic,
        amount_tolerance: tolerance ? parseFloat(tolerance) : undefined,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bills"] });
      toast.success("Bill created");
      resetForm();
    },
    onError: (e: Error) => toast.error("Error", apiErrorMessage(e)),
  });

  const update = useMutation({
    mutationFn: () =>
      updateBill(editId!, {
        name: name.trim(),
        amount: amount ? parseFloat(amount) : undefined,
        // Explicit `null` clears the scope — `exclude_unset` would otherwise
        // keep the OLD category while the select showed blank.
        category_id: categoryId === "" ? null : Number(categoryId),
        frequency,
        next_due_date: dueDate,
      }).then(() =>
        updateBillRule(editId!, {
          match_mode: matchMode,
          match_terms: currentTerms(),
          match_logic: matchLogic,
          amount_tolerance: tolerance ? parseFloat(tolerance) : null,
        }),
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bills"] });
      toast.success("Bill updated");
      resetForm();
    },
    onError: (e: Error) => toast.error("Error", apiErrorMessage(e)),
  });

  const findPast = useMutation({
    mutationFn: () =>
      editId === null
        ? findPastForDraft({
            match_mode: matchMode,
            match_terms: currentTerms(),
            match_logic: matchLogic,
            amount_tolerance: tolerance ? parseFloat(tolerance) : null,
            amount: amount ? parseFloat(amount) : null,
            frequency,
            // M123: test the SCOPE the form shows, not a stale one.
            category_id: categoryId === "" ? null : Number(categoryId),
          })
        : findPastPayments(editId, {
            match_mode: matchMode,
            match_terms: currentTerms(),
            match_logic: matchLogic,
            amount_tolerance: tolerance ? parseFloat(tolerance) : null,
            category_id: categoryId === "" ? null : Number(categoryId),
          }),
    onSuccess: (result) => {
      setPreview(result);
      setPreviewOpen(true);
    },
    onError: (e: Error) => toast.error("Could not find past payments", apiErrorMessage(e)),
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteBill(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bills"] });
      toast.success("Bill deleted");
    },
    onError: (e: Error) => toast.error("Error", apiErrorMessage(e)),
  });

  const paid = useMutation({
    mutationFn: (id: number) => markPaid(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bills"] });
      toast.success("Marked as paid");
    },
    onError: (e: Error) => toast.error("Error", apiErrorMessage(e)),
  });

  function resetForm() {
    setName("");
    setAmount("");
    setFrequency("monthly");
    setDueDate("");
    setMatchMode("contains");
    setMatchTerms([]);
    setTermDraft("");
    setMatchLogic("any");
    setTolerance("");
    setEditId(null);
    setFromSubscriptionId(null);
    setPreview(null);
    setPreviewOpen(false);
    setCategoryId("");
    setPrefill(null);
    setShowForm(false);
    // Drop the hand-off params so a reload can never reopen a stale draft.
    setSearchParams({}, { replace: true });
  }

  const upcomingBills =
    bills.data?.filter((b) => daysUntil(b.next_due_date) <= 7 && daysUntil(b.next_due_date) >= 0) ||
    [];
  const totalUpcoming = upcomingBills.reduce(
    (sum, b) => sum + (b.amount ? parseFloat(String(b.amount)) : 0),
    0,
  );

  const canFindPast = useMemo(
    () => (matchTerms.length > 0 || termDraft.trim() !== "" || name.trim() !== "") && !findPast.isPending,
    [matchTerms, termDraft, name, findPast.isPending],
  );

  function startEdit(bill: Bill) {
    setEditId(bill.id);
    setName(bill.name);
    setAmount(bill.amount ? String(bill.amount) : "");
    setCategoryId(bill.category_id != null ? String(bill.category_id) : "");
    setFrequency(bill.frequency);
    setDueDate(bill.next_due_date);
    setMatchMode(bill.match_mode);
    setMatchTerms(readTerms(bill));
    setTermDraft("");
    setMatchLogic(readLogic(bill));
    setTolerance(bill.amount_tolerance ? String(bill.amount_tolerance) : "");
    setFromSubscriptionId(null);
    setPreview(null);
    setPreviewOpen(false);
    setPrefill(null);
    setSearchParams({}, { replace: true });
    setShowForm(true);
  }

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">Bills</h1>
        <Button
          onClick={() => {
            resetForm();
            setShowForm(!showForm);
          }}
        >
          <Plus className="size-4 mr-1" /> New bill
        </Button>
      </div>

      {upcomingBills.length > 0 && (
        <Card className="p-4 border-l-4 border-l-warn">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-text">Upcoming this week</div>
              <div className="text-2xl font-bold text-text">
                {money(String(totalUpcoming))}
              </div>
            </div>
            <Badge tone="warn">{upcomingBills.length} bills</Badge>
          </div>
        </Card>
      )}

      {showForm && (
        <Card className="p-4">
          {/* M123 (docs/09 §Create from search): this draft is a DRAFT — the
              prefill is shown, never applied behind the user's back. */}
          {prefillProvenance !== null && (
            <p className="mb-3 text-xs text-muted" data-testid="prefill-provenance">
              {prefillProvenance}
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            <input
              aria-label="Bill name"
              placeholder="Bill name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={fieldClass}
            />
            <input
              aria-label="Amount (optional)"
              placeholder="Amount (optional)"
              type="number"
              step="0.01"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              className={fieldClass}
            />
            <select
              aria-label="Frequency"
              value={frequency}
              onChange={(e) => setFrequency(e.target.value)}
              className={fieldClass}
            >
              <option value="monthly">Monthly</option>
              <option value="quarterly">Quarterly</option>
              <option value="yearly">Yearly</option>
              <option value="one_time">One time</option>
            </select>
            <input
              aria-label="Due date"
              type="date"
              value={dueDate}
              onChange={(e) => setDueDate(e.target.value)}
              className={fieldClass}
            />
          </div>

          <fieldset className="mt-3 rounded-lg border border-control p-3">
            <legend className="px-1 text-xs font-medium text-muted">
              Match transactions (M116 · M121)
            </legend>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-xs text-muted">
                Payee
                <select
                  aria-label="Match mode"
                  value={matchMode}
                  onChange={(e) => setMatchMode(e.target.value as BillMatchMode)}
                  className={`${fieldClass} mt-1`}
                >
                  {BILL_MATCH_MODES.map((mode) => (
                    <option key={mode} value={mode}>
                      {BILL_MATCH_MODE_LABELS[mode]}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-xs text-muted">
                Amount tolerance (±, blank = exact)
                <input
                  aria-label="Amount tolerance"
                  type="number"
                  min="0"
                  step="0.01"
                  value={tolerance}
                  disabled={amount === ""}
                  onChange={(e) => setTolerance(e.target.value)}
                  className={`${fieldClass} mt-1 disabled:opacity-50`}
                />
              </label>
              {/* M123 (docs/25): the matcher's CATEGORY SCOPE — which
                  transactions this rule may match. No "uncategorized" option:
                  a scope is a category id, and a bill scoped to "no category"
                  is not a thing the schema can say. */}
              <label className="block text-xs text-muted">
                Category (blank = any)
                <select
                  aria-label="Match category"
                  value={categoryId}
                  onChange={(e) => setCategoryId(e.target.value)}
                  className={`${fieldClass} mt-1`}
                >
                  <option value="">Any category</option>
                  {(categories.data?.items ?? []).map((category) => (
                    <option key={category.id} value={String(category.id)}>
                      {category.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {/* M121: one chip per term, and the ANY/ALL switch BESIDE the field
                — the term list and its AND/OR mode are one decision (docs/25
                §M121 D1/D2). */}
            <div className="mt-3">
              <MatchTermsField
                terms={matchTerms}
                draft={termDraft}
                logic={matchLogic}
                onDraftChange={setTermDraft}
                onAdd={(term) =>
                  setMatchTerms((prev) => (prev.includes(term) ? prev : [...prev, term]))
                }
                onRemove={(index) =>
                  setMatchTerms((prev) => prev.filter((_term, position) => position !== index))
                }
                onLogicChange={setMatchLogic}
                placeholder={name || "e.g. NETFLIX"}
                fieldClassName={`${fieldClass} mt-1`}
                suggestions={suggestions.suggestions}
              />
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-muted">
              Left blank, the bill&rsquo;s name is the only term. ANY matches a transaction when one
              term hits; ALL needs every term to hit. Normalisation (case, trailing numbers) happens
              the same way the recurring detector does it.
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {/* M122 T2: the dry-run is the SAME "Test" action the bill
                  editor has — one word for one behavior across both forms. */}
              <Button
                size="sm"
                variant="ghost"
                disabled={!canFindPast}
                onClick={() => findPast.mutate()}
              >
                <History className="size-4 mr-1" />
                Test rule
              </Button>
            </div>
            {previewOpen && preview !== null && <RuleTestPanel preview={preview} />}
          </fieldset>

          <div className="flex gap-2 mt-3">
            <Button
              onClick={() => (editId ? update.mutate() : create.mutate())}
              disabled={
                !name ||
                !dueDate ||
                (matchTerms.length === 0 && termDraft.trim() === "" && name.trim() === "") ||
                update.isPending ||
                create.isPending
              }
            >
              {editId ? "Update" : "Create"}
            </Button>
            <Button variant="ghost" onClick={resetForm}>
              Cancel
            </Button>
          </div>
        </Card>
      )}

      {bills.isPending ? (
        <SkeletonRows rows={3} />
      ) : bills.data?.length === 0 ? (
        <EmptyState
          icon={<Calendar />}
          title="No bills"
          hint="Add your recurring bills to track upcoming payments."
          preview={{
            src: "/help/bills.png",
            alt: "The Bills page with recurring bills and their due dates",
          }}
          action={{
            label: "New bill",
            onClick: () => {
              resetForm();
              setShowForm(true);
            },
            ariaLabel: "New bill — No bills",
          }}
        />
      ) : (
        <motion.div variants={staggerContainer(0.05)} initial="hidden" animate="show" className="space-y-2">
          {bills.data?.map((b) => {
            const days = daysUntil(b.next_due_date);
            const payments = b.payment_count ?? 0;
            return (
              <motion.div key={b.id} variants={fadeUp}>
                {/* flex-wrap: at 390px the name+badge and the amount+actions
                    blocks cannot share one line once the row actions are 44px
                    touch targets (M72) — the second block drops below instead
                    of being clipped by the shell's overflow-x-clip. Desktop
                    fits both on one line, so nothing changes there. */}
                <Card className="flex flex-wrap items-center justify-between gap-2 p-4">
                  <div className="flex min-w-0 items-center gap-3">
                    <div className="min-w-0">
                      <Link
                        to={`/bills/${b.id}`}
                        className="flex items-center gap-1 font-medium text-text outline-none hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
                        aria-label={`Open ${b.name}`}
                      >
                        <span className="truncate">{b.name}</span>
                        <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
                      </Link>
                      <div className="text-xs text-muted">
                        {b.frequency} · Due {b.next_due_date} ·{" "}
                        {payments === 0 ? "0 payments yet" : `${payments} payment${payments === 1 ? "" : "s"}`}
                        {b.rolling_average != null && (
                          <> · avg {money(String(b.rolling_average))}</>
                        )}
                        {/* M122 T4: the compact row carries the calendar-YTD
                            figure the server computed from the same rule. */}
                        {b.ytd_total !== undefined && <> · YTD {money(b.ytd_total)}</>}
                      </div>
                    </div>
                    <Badge tone={days <= 0 ? "danger" : days <= 3 ? "warn" : "neutral"}>
                      {days <= 0 ? "Overdue" : `${days}d`}
                    </Badge>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-text">
                      {money(b.amount ? String(b.amount) : null)}
                    </span>
                    <button
                      onClick={() => paid.mutate(b.id)}
                      className="p-1 rounded hover:bg-surface-raised"
                      title="Mark paid"
                      aria-label={`Mark ${b.name} paid`}
                    >
                      <Check className="size-4 text-gain" />
                    </button>
                    <button
                      onClick={() => startEdit(b)}
                      className="p-1 rounded hover:bg-surface-raised"
                      title="Edit"
                      aria-label={`Edit ${b.name}`}
                    >
                      <Pencil className="size-4 text-muted" />
                    </button>
                    <button
                      onClick={() => remove.mutate(b.id)}
                      className="p-1 rounded hover:bg-surface-raised"
                      title="Delete"
                      aria-label={`Delete ${b.name}`}
                    >
                      <Trash2 className="size-4 text-danger" />
                    </button>
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
