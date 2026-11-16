// BillDetail — `/bills/:billId` (M116 B2, docs/25 §M116).
//
// Everything on this page is SERVER-computed from one `GET /bills/{id}/detail`
// call: the matched rows, both rolling windows and the period series. The page
// never re-derives a total from the visible page — that is how a paginated list
// and a chart disagree (the M91 anti-fabrication rule, applied to the client).
//
// The rule editor is INLINE and honest about what it does (D3): the matched set
// is the rule re-applied over the ledger on every read, so saving here changes
// which transactions the bill holds immediately — there is no stored linkage to
// refresh, and the copy says so.
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, History, Pencil, Search, Wallet } from "lucide-react";
import {
  BILL_MATCH_MODES,
  BILL_MATCH_MODE_LABELS,
  composeTerms,
  describeTerms,
  findPastPayments,
  getBillDetail,
  readLogic,
  readTerms,
  updateBillRule,
  type BillFindPast,
  type BillMatchLogic,
  type BillMatchMode,
} from "../api/bills";
import { MatchTermsField } from "../components/bills/MatchTermsField";
import { RuleTestPanel } from "../components/bills/RuleTestPanel";
import { useTermSuggestions } from "../components/bills/useTermSuggestions";
import { listAccounts } from "../api/accounts";
import { listCategories } from "../api/categories";
import { updateTransaction, type Transaction, type TransactionPatch } from "../api/transactions";
import { BillHistoryBarChart } from "../components/charts/BillHistoryBarChart";
import { ChartRangeControl } from "../components/ui/ChartRangeControl";
import { rangeWindow, type ChartRange } from "../lib/chartRange";
import { TransactionEditor } from "../components/transactions/TransactionEditor";
import { apiErrorMessage, formatMoney } from "../components/transactions/format";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import {
  DemoLock,
  DemoReason,
  DemoRefusal,
  demoLocks,
  useDemoRefusal,
} from "../components/ui/DemoLock";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { TableScroller } from "../components/ui/TableScroller";
import { useOptionalUser } from "../auth/authUser";
import { useToast } from "../components/ui/Toast";
import { useMasked } from "../lib/mask";

const PAGE_SIZE = 50;

/**
 * `aria-describedby` target of BOTH write padlocks in the rule editor —
 * `Save rule` (`PUT /bills/{id}/rule`) and `Test`
 * (`POST /bills/{id}/find-past`). One VISIBLE reason element serves them
 * (an `aria-describedby` IDREF may be shared) — a reason per control would
 * print the same sentence twice in one form.
 */
const BILL_RULE_REASON_ID = "bill-rule-write-demo-reason";

const FREQUENCY_LABELS: Record<string, string> = {
  monthly: "Monthly",
  quarterly: "Quarterly",
  yearly: "Yearly",
  one_time: "One time",
};

function money(value: string | number | null | undefined): string {
  return formatMoney(String(value ?? ""), "CAD");
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className="text-lg font-semibold tabular text-text">{value}</div>
      {hint && <div className="text-[11px] text-muted">{hint}</div>}
    </div>
  );
}

export function BillDetail() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const { billId } = useParams();
  const queryClient = useQueryClient();
  const toast = useToast();
  const id = Number(billId);
  // docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal for every write
  // this page dispatches — a refused demo write is one local `role="status"`
  // element + one `toast.info`, never a `role="alert"` toast (Toast renders
  // an error tone AS `role="alert"`).
  const demo = useDemoRefusal();
  // The session scope is the M136 optional user (the same scope
  // TransactionEditor reads): outside an `AuthProvider` the scope is null and
  // the verdict degrades to "no session, no lock" — the honest degradation
  // `auth/authUser.ts` documents — instead of throwing.
  const user = useOptionalUser();
  // docs/47 §4 condition 2: `PUT /bills/{id}/rule` is a mutating request on
  // a path `deps.py` does not exempt.
  const ruleSaveLocked = demoLocks(user, "PUT", `/bills/${id}/rule`);
  // The dry-run really IS a mutating request (`POST /bills/{id}/find-past`),
  // so the demo boundary refuses it too — its padlock is not decoration.
  const ruleTestLocked = demoLocks(user, "POST", `/bills/${id}/find-past`);
  const [offset, setOffset] = useState(0);
  const [ruleOpen, setRuleOpen] = useState(false);
  const [mode, setMode] = useState<BillMatchMode>("contains");
  /** M121: the term CHIPS plus the text still typed (composed on save). */
  const [terms, setTerms] = useState<string[]>([]);
  const [termDraft, setTermDraft] = useState("");
  const [logic, setLogic] = useState<BillMatchLogic>("any");
  const [tolerance, setTolerance] = useState("");
  /** M122 T2: the DRAFT rule's dry-run ("Test"), never the saved rule. */
  const [testPreview, setTestPreview] = useState<BillFindPast | null>(null);
  const [editorTarget, setEditorTarget] = useState<Transaction | null>(null);
  // docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal element for the
  // whole page, rendered in exactly ONE place — inside the transaction editor
  // dialog when it is open (where the error line used to be), otherwise at
  // the top of the section. The two never stack, so a refused write is always
  // exactly one `demo-refusal`.
  const refusalNode =
    demo.refusal !== null ? (
      <DemoRefusal id={demo.refusalId} action={demo.refusal} />
    ) : null;
  const refusalPlace = editorTarget !== null ? "editor" : "page";

  // M122 T2: a preview describes the draft it was run from — editing the draft
  // invalidates it, so it can never be mistaken for the current inputs'
  // answer. (Signature is stable across a successful test, so the panel the
  // user just asked for stays put.)
  const draftSignature = JSON.stringify([mode, terms, termDraft, logic, tolerance]);
  useEffect(() => {
    setTestPreview(null);
  }, [draftSignature]);

  // M122 T1: suggestions from the user's OWN ledger, live while the editor is
  // open. `billId` makes the server exclude what the SAVED rule already
  // matches — the create form has no id and gets the plain ranking.
  const suggestions = useTermSuggestions({
    enabled: ruleOpen,
    draft: termDraft,
    billId: Number.isFinite(id) ? id : null,
    matchMode: mode,
    terms,
  });

  const detail = useQuery({
    queryKey: ["bill-detail", id, offset],
    queryFn: () => getBillDetail(id, { limit: PAGE_SIZE, offset }),
    enabled: Number.isFinite(id),
  });

  // M126 (docs/12 §Chart ranges): the period chart gets its OWN window. The
  // stats and the transactions table below are all-time figures, so the range
  // control must not ride the detail query — a second query, keyed by the
  // window, and DISABLED for `All` (the default), which is exactly the
  // payload `detail` already carries. One extra request per chosen range,
  // cached by that range.
  const [chartRange, setChartRange] = useState<ChartRange>("All");
  const chartSpan = rangeWindow(chartRange);
  const rangedDetail = useQuery({
    queryKey: ["bill-detail-chart", id, chartSpan.from ?? "", chartSpan.to],
    queryFn: () =>
      getBillDetail(id, { from: chartSpan.from ?? undefined, to: chartSpan.to }),
    enabled: chartRange !== "All" && Number.isFinite(id),
    staleTime: 60_000,
  });

  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => listAccounts(200) });
  const categories = useQuery({ queryKey: ["categories"], queryFn: () => listCategories(200) });

  const saveRule = useMutation({
    mutationFn: () =>
      updateBillRule(id, {
        match_mode: mode,
        // The typed-but-uncommitted term counts: losing it on Save would be a
        // rule the user never asked for (docs/25 §M121 D8).
        match_terms: composeTerms(terms, termDraft),
        match_logic: logic,
        amount_tolerance: tolerance.trim() === "" ? null : Number(tolerance),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["bill-detail", id] });
      void queryClient.invalidateQueries({ queryKey: ["bills"] });
      setRuleOpen(false);
      toast.success("Rule saved", "Transactions are re-matched against it on every read.");
    },
    onError: (error: Error) => {
      // docs/47 §4: a demo refusal is not a failure — one `demo-refusal` +
      // one `toast.info`, never an error toast (which renders `role="alert"`).
      if (demo.onError("Save rule", error)) return;
      toast.error("Could not save the rule", apiErrorMessage(error));
    },
  });

  // M122 T2 — "Test": the DRAFT rule over the whole committed ledger, rendered
  // inline. Same machinery as the create form's dry-run, but hung off the
  // saved bill so the editor can try terms it has not committed yet.
  const testRule = useMutation({
    mutationFn: () =>
      findPastPayments(id, {
        match_mode: mode,
        match_terms: composeTerms(terms, termDraft),
        match_logic: logic,
        amount_tolerance: tolerance.trim() === "" ? null : Number(tolerance),
      }),
    onSuccess: (result) => setTestPreview(result),
    onError: (error: Error) => {
      // `POST /bills/{id}/find-past` is a mutating request, so the demo
      // boundary really refuses it (docs/47 §4) — a refusal, never an error.
      if (demo.onError("Test rule", error)) return;
      toast.error("Could not test the rule", apiErrorMessage(error));
    },
  });

  const saveTransaction = useMutation({
    mutationFn: ({ target, payload }: { target: Transaction; payload: TransactionPatch }) =>
      updateTransaction(target.id, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["bill-detail", id] });
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      setEditorTarget(null);
      toast.success("Transaction updated");
    },
    onError: (error: Error) => {
      // docs/47 §4: the editor owns this refusal (it renders the one
      // `demo-refusal` + `toast.info`). The host only skips its own error
      // path — an error toast renders `role="alert"`.
      if (demo.onError("Save transaction", error)) return;
      toast.error("Transaction save failed", apiErrorMessage(error));
    },
  });

  const data = detail.data;

  const windowSize = useMemo(
    () => Math.min(PAGE_SIZE, data?.transactions.limit ?? PAGE_SIZE),
    [data?.transactions.limit],
  );

  function openRule() {
    if (data === undefined) return;
    setMode(bill.match_mode);
    setTerms(readTerms(bill));
    setTermDraft("");
    setLogic(readLogic(bill));
    setTolerance(bill.amount_tolerance ?? "");
    setRuleOpen(true);
  }

  if (detail.isPending) {
    return (
      <section className="space-y-4">
        <SkeletonRows rows={4} />
      </section>
    );
  }

  if (detail.isError || data === undefined) {
    return <ErrorPanel message={apiErrorMessage(detail.error)} />;
  }

  const bill = data.bill;
  const rolling = data.rolling;
  const rangedChart = chartRange !== "All";
  const chartLoading = rangedChart && rangedDetail.data === undefined;
  const chart = rangedChart ? (rangedDetail.data?.chart ?? data.chart) : data.chart;
  const stats = data.stats;
  const items = data.transactions.items;
  const total = data.transactions.total;
  const rangeStart = total === 0 ? 0 : offset + 1;
  const rangeEnd = Math.min(offset + windowSize, total);

  return (
    <section className="space-y-4">
      {/* docs/47 §4: the page's one refusal, above the card it is about (or
          inside the open transaction editor — never both). */}
      {refusalPlace === "page" && refusalNode}
      <div className="flex items-center justify-between gap-2">
        <Link
          to="/bills"
          className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-sm text-muted outline-none hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
        >
          <ArrowLeft className="size-4" aria-hidden /> Bills
        </Link>
        <Button size="sm" variant="ghost" onClick={openRule} aria-label={`Edit rule for ${bill.name}`}>
          <Pencil className="size-4 mr-1" aria-hidden /> Edit rule
        </Button>
      </div>

      <Card className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold tracking-tight text-text">{bill.name}</h1>
            <p className="mt-0.5 text-sm text-muted">
              {FREQUENCY_LABELS[bill.frequency] ?? bill.frequency} · Due {bill.next_due_date}
              {bill.amount !== null && <> · expected {money(bill.amount)}</>}
            </p>
            <p className="mt-1 text-xs text-muted">
              Matches transactions whose payee{" "}
              <span className="text-text">
                {/* M121 D9: the conjunction is said out loud — “A” or “B”
                    widens, “A” and “B” narrows, and the sentence must not be
                    able to describe one while the counts show the other. */}
                {BILL_MATCH_MODE_LABELS[bill.match_mode]}{" "}
                {describeTerms(readTerms(bill), readLogic(bill))}
              </span>
              {bill.amount !== null && (
                <>
                  {" "}
                  at {money(bill.amount)}
                  {bill.amount_tolerance !== null ? ` ± ${money(bill.amount_tolerance)}` : " exactly"}
                </>
              )}
              {bill.category_id !== null && <> · within one category</>}.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Badge tone={data.matched_count === 0 ? "neutral" : "accent"}>
              {data.matched_count === 0
                ? "0 payments yet"
                : `${data.matched_count} payment${data.matched_count === 1 ? "" : "s"}`}
            </Badge>
          </div>
        </div>

        {ruleOpen && (
          <form
            className="mt-4 space-y-3 border-t border-control pt-3"
            onSubmit={(event) => {
              event.preventDefault();
              saveRule.mutate();
            }}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-xs text-muted">
                Match
                <select
                  value={mode}
                  onChange={(event) => setMode(event.target.value as BillMatchMode)}
                  className="field-ring mt-1 w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm text-text"
                >
                  {BILL_MATCH_MODES.map((value) => (
                    <option key={value} value={value}>
                      {BILL_MATCH_MODE_LABELS[value]}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-xs text-muted">
                Amount tolerance (±, blank = exact)
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={tolerance}
                  disabled={bill.amount === null}
                  onChange={(event) => setTolerance(event.target.value)}
                  className="field-ring mt-1 w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm text-text disabled:opacity-50"
                />
              </label>
            </div>
            {/* M121 D1/D2: chips for the terms, ALL/ANY beside them.
                M122 T1: suggestions from the user's own ledger render under
                the input — quick-add chips while it is empty, a listbox
                while a draft of ≥3 chars is typed. */}
            <div className="mt-3">
              <MatchTermsField
                terms={terms}
                draft={termDraft}
                logic={logic}
                onDraftChange={setTermDraft}
                onAdd={(term) =>
                  setTerms((prev) => (prev.includes(term) ? prev : [...prev, term]))
                }
                onRemove={(index) =>
                  setTerms((prev) => prev.filter((_term, position) => position !== index))
                }
                onLogicChange={setLogic}
                placeholder="Add a term"
                fieldClassName="field-ring mt-1 w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm text-text"
                suggestions={suggestions.suggestions}
              />
            </div>
            <p className="text-[11px] leading-relaxed text-muted">
              Saving re-matches every committed transaction against this rule right away — the bill
              always holds exactly what its current rule finds, so nothing can go stale.
            </p>
            {/* docs/47 §4 (F-4): the visible reason BOTH write padlocks in
                this form bind to — same panel, non-zero box, never sr-only. */}
            {(ruleSaveLocked || ruleTestLocked) && (
              <DemoReason id={BILL_RULE_REASON_ID}>
                The demo account is read-only — rule changes and tests are not saved.
              </DemoReason>
            )}
            <div className="flex gap-2">
              <Button
                type="submit"
                size="sm"
                disabled={saveRule.isPending || composeTerms(terms, termDraft).length === 0}
                // docs/47 §4: Save's ACTION is the blocked write
                // (`PUT /bills/{id}/rule`) — padlock + the form's one visible
                // reason; it stays ENABLED so it can explain itself.
                aria-describedby={ruleSaveLocked ? BILL_RULE_REASON_ID : undefined}
              >
                {ruleSaveLocked && <DemoLock />}
                Save rule
              </Button>
              {/* M122 T2: the dry-run sits BESIDE Save — the user sees the
                  rule's effect before committing to it. */}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={testRule.isPending || composeTerms(terms, termDraft).length === 0}
                // docs/47 §4: the dry-run is `POST /bills/{id}/find-past` —
                // a mutating request the demo boundary refuses too.
                aria-describedby={ruleTestLocked ? BILL_RULE_REASON_ID : undefined}
                onClick={() => testRule.mutate()}
              >
                {ruleTestLocked && <DemoLock />}
                <History className="size-4 mr-1" aria-hidden /> Test
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setRuleOpen(false)}>
                Cancel
              </Button>
            </div>
            {testPreview !== null && <RuleTestPanel preview={testPreview} />}
          </form>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="p-4">
          {/* M122 T4: "Rolling average" becomes "Spending stats" — the two
              rolling windows keep their missed-$0.00 semantics; all-time and
              YTD count only payments that happened. All six numbers are
              SERVER-computed from the rule's matched set on every read (M91
              anti-fabrication). */}
          <CardTitle>Spending stats</CardTitle>
          <div className="mt-3 grid grid-cols-2 gap-4">
            <Stat
              label="Last 6 periods"
              value={rolling.six.mean === null ? "—" : money(rolling.six.mean)}
              hint={
                rolling.six.median === null
                  ? "no payments yet"
                  : `median ${money(rolling.six.median)} · ${rolling.six.payments} payment${
                      rolling.six.payments === 1 ? "" : "s"
                    }`
              }
            />
            <Stat
              label="Last 12 periods"
              value={rolling.twelve.mean === null ? "—" : money(rolling.twelve.mean)}
              hint={
                rolling.twelve.median === null
                  ? "no payments yet"
                  : `median ${money(rolling.twelve.median)} · ${rolling.twelve.payments} payment${
                      rolling.twelve.payments === 1 ? "" : "s"
                    }`
              }
            />
            <Stat
              label="All-time total"
              value={money(stats.all_time.total)}
              hint={
                stats.all_time.payments === 0
                  ? "no payments yet"
                  : `${stats.all_time.payments} payment${stats.all_time.payments === 1 ? "" : "s"}`
              }
            />
            <Stat
              label="Per payment (all-time)"
              value={stats.all_time.average === null ? "—" : money(stats.all_time.average)}
              hint="mean of every payment this rule found"
            />
            <Stat
              label={`This year (${stats.ytd.year})`}
              value={money(stats.ytd.total)}
              hint={
                stats.ytd.payments === 0
                  ? "nothing yet this year"
                  : `${stats.ytd.payments} payment${stats.ytd.payments === 1 ? "" : "s"} so far`
              }
            />
            <Stat
              label="Yearly average"
              value={
                stats.all_time.yearly_average === null
                  ? "—"
                  : money(stats.all_time.yearly_average)
              }
              hint={`all-time ÷ ${stats.all_time.years} year${
                stats.all_time.years === 1 ? "" : "s"
              } with payments`}
            />
          </div>
          <p className="mt-3 text-[11px] leading-relaxed text-muted">
            A period with no payment counts as $0.00 in the rolling averages above — the average is
            what the bill really cost you, missed months included. All-time and this-year totals
            count only payments that actually happened.
          </p>
        </Card>
        <Card className="p-4">
          <CardTitle>Found payments</CardTitle>
          <div className="mt-3 flex items-center gap-4">
            <Wallet className="size-5 text-accent" aria-hidden />
            <div>
              <div className="text-2xl font-bold tabular text-text">{bill.payment_count}</div>
              <div className="text-xs text-muted">
                {bill.payment_count === 0
                  ? "No payments match this rule yet"
                  : "committed payments match this rule"}
              </div>
            </div>
          </div>
        </Card>
      </div>

      <Card className="p-4">
        <CardTitle right={<ChartRangeControl value={chartRange} onChange={setChartRange} ariaLabel="Payments-by-period range" />}>
          Payments by period
        </CardTitle>
        <div className="mt-3">
          {chartLoading ? (
            <SkeletonRows rows={3} />
          ) : (
            <BillHistoryBarChart points={chart.points} granularity={chart.granularity} />
          )}
        </div>
      </Card>

      <Card className="p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <CardTitle>Transactions</CardTitle>
          <span className="text-xs text-muted tabular">
            {rangeStart}–{rangeEnd} of {total}
          </span>
        </div>
        {items.length === 0 ? (
          <EmptyState
            icon={<Search />}
            title="No payments yet"
            hint="Nothing in your committed ledger matches this rule. Try loosening it, or wait for the next payment."
            preview={{
              none: "The empty state only appears when a rule matches nothing — a capture of it would be a screenshot of this same copy.",
            }}
            action={{
              label: "Edit rule",
              onClick: openRule,
              ariaLabel: "Edit rule — No payments yet",
            }}
          />
        ) : (
          <>
            <TableScroller label="Transactions matching this bill">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-control text-left text-xs text-muted">
                    <th scope="col" className="px-3 py-2 font-medium">Date</th>
                    <th scope="col" className="px-3 py-2 font-medium">Payee</th>
                    <th scope="col" className="px-3 py-2 font-medium">Account</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((txn) => (
                    <tr
                      key={txn.id}
                      onClick={() => setEditorTarget(txn)}
                      className="cursor-pointer border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]"
                    >
                      <td className="whitespace-nowrap px-3 py-2 tabular">{txn.date}</td>
                      <td className="px-3 py-2 font-medium text-text">{txn.normalized_payee}</td>
                      <td className="px-3 py-2 text-muted">
                        {(accounts.data?.items ?? []).find((a) => a.id === txn.account_id)?.name ??
                          `#${txn.account_id}`}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-right tabular">
                        <span className={txn.amount_native.startsWith("-") ? "text-danger" : ""}>
                          {formatMoney(txn.amount_native, txn.currency)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroller>
            {total > windowSize && (
              <div className="mt-3 flex items-center justify-between gap-2">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - windowSize))}
                >
                  Previous
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={offset + windowSize >= total}
                  onClick={() => setOffset(offset + windowSize)}
                >
                  Next
                </Button>
              </div>
            )}
          </>
        )}
      </Card>

      {editorTarget !== null && (
        <TransactionEditor
          target={editorTarget}
          accounts={accounts.data?.items ?? []}
          categories={categories.data?.items ?? []}
          saving={saveTransaction.isPending}
          error={saveTransaction.error}
          demoRefusal={refusalPlace === "editor" ? refusalNode ?? undefined : undefined}
          onWriteError={(action, error) => demo.onError(action, error)}
          onSave={(payload) => saveTransaction.mutate({ target: editorTarget, payload })}
          onClose={() => setEditorTarget(null)}
          onCategoryCreated={() => {
            void queryClient.invalidateQueries({ queryKey: ["categories"] });
          }}
        />
      )}
    </section>
  );
}
