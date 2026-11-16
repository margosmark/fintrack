// AccountsSection (docs/14 C2): institution-grouped account manager for
// Settings — balances, derived taxable badges, full CRUD against /accounts.
// Exports `AccountForm` so the Imports page picker can reuse the exact same
// create form inline (docs/14: first-run users never leave the wizard).

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Landmark, Pencil, Plus, Trash2, Wallet } from "lucide-react";
import {
  createAccount,
  deleteAccount,
  listAccounts,
  updateAccount,
} from "../../api/accounts";
import type { Account, AccountCreateInput, AccountType } from "../../api/accounts";
import { TYPE_LABELS } from "../../lib/accountGroups";
import {
  createInstitution,
  listInstitutions,
} from "../../api/institutions";
import type { Institution } from "../../api/institutions";
import { listTransactions } from "../../api/transactions";
import { listTrades } from "../../api/trades";
import { listAcbPools } from "../../api/acb";
import { listImports } from "../../api/imports";
import { listDebts } from "../../api/debts";
import { listGoals } from "../../api/goals";
import { getReconciliation } from "../../api/reconciliation";
import { apiErrorMessage } from "../transactions/format";
import { deferDelete } from "../ui/deferDelete";
import { useDialogFocus, useEscapeToClose } from "../ui/dialogA11y";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card, CardTitle } from "../ui/Card";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { fadeUp } from "../ui/motion";
import { useToast } from "../ui/Toast";
import { inputClass } from "../ui/formStyles";

// Derive the section's field class from the shared base (adds full width).
const FIELD = `${inputClass} w-full`;

const TYPE_GROUPS: ReadonlyArray<{ label: string; types: readonly AccountType[] }> = [
  { label: "Cash", types: ["chequing", "savings"] },
  { label: "Credit", types: ["credit_card", "line_of_credit"] },
  {
    label: "Registered non-taxable",
    types: ["rrsp", "tfsa", "fhsa", "resp", "lira", "rdsp"],
  },
  { label: "Non-registered", types: ["brokerage_non_registered"] },
];

/** docs/14 C1: taxability derives from type alone, server-side. */
function isTaxableType(type: AccountType): boolean {
  return type === "brokerage_non_registered";
}

/**
 * Shared create/edit account form (pin 7/9). Renders fields only — parents own
 * submission, toasts, and surrounding chrome. Handles "+ New institution"
 * inline by creating the institution through the existing client first.
 */
export function AccountForm({
  institutions,
  initial = null,
  pending = false,
  error = null,
  submitLabel,
  onSubmit,
}: {
  institutions: Institution[];
  /** Present → edit mode prefill; absent/null → creation. */
  initial?: Account | null;
  pending?: boolean;
  error?: string | null;
  submitLabel?: string;
  onSubmit: (input: AccountCreateInput) => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [type, setType] = useState<AccountType>(initial?.type ?? "chequing");
  const [currency, setCurrency] = useState(initial?.currency ?? "CAD");
  const [institutionId, setInstitutionId] = useState(
    initial?.institution_id === null || initial === null
      ? ""
      : String(initial.institution_id),
  );
  const [newInstitutionName, setNewInstitutionName] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  const creatingInstitution = useMutation({
    mutationFn: (institutionName: string) => createInstitution({ name: institutionName }),
    onError: (err) => setLocalError(apiErrorMessage(err)),
  });

  // Reset transient state whenever the parent swaps edit targets.
  useEffect(() => {
    setName(initial?.name ?? "");
    setType(initial?.type ?? "chequing");
    setCurrency(initial?.currency ?? "CAD");
    setInstitutionId(initial?.institution_id == null ? "" : String(initial.institution_id));
    setNewInstitutionName("");
    setLocalError(null);
  }, [initial]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedName = name.trim();
    if (trimmedName === "") {
      setLocalError("Name is required");
      return;
    }
    if (!/^[A-Za-z]{3}$/.test(currency)) {
      setLocalError("Currency must be a 3-letter ISO code");
      return;
    }

    let resolvedInstitutionId: number | null =
      institutionId === "" || institutionId === "__new__"
        ? null
        : Number(institutionId);
    if (resolvedInstitutionId === null && newInstitutionName.trim() !== "") {
      try {
        const created = await creatingInstitution.mutateAsync(newInstitutionName.trim());
        resolvedInstitutionId = created.id;
      } catch {
        return; // mutation's onError already surfaced the detail
      }
    }
    setLocalError(null);
    onSubmit({
      name: trimmedName,
      type,
      currency: currency.toUpperCase(),
      institution_id: resolvedInstitutionId,
    });
  }

  const busy = pending || creatingInstitution.isPending;

  return (
    <form onSubmit={(e) => void handleSubmit(e)} className="space-y-3">
      <label className="block text-xs text-muted">
        Name
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Everyday chequing"
          className={`${FIELD} mt-1`}
          autoFocus
        />
      </label>

      <label className="block text-xs text-muted">
        Institution
        <select
          value={institutionId}
          onChange={(e) => {
            setInstitutionId(e.target.value);
            if (e.target.value !== "") setNewInstitutionName("");
          }}
          className={`${FIELD} mt-1`}
        >
          <option value="">No institution</option>
          {institutions.map((institution) => (
            <option key={institution.id} value={String(institution.id)}>
              {institution.name}
            </option>
          ))}
          <option value="__new__">+ New institution…</option>
        </select>
      </label>
      {institutionId === "__new__" && (
        <input
          value={newInstitutionName}
          onChange={(e) => setNewInstitutionName(e.target.value)}
          placeholder="New institution name"
          className={FIELD}
        />
      )}

      <label className="block text-xs text-muted">
        Type
        <select
          value={type}
          onChange={(e) => setType(e.target.value as AccountType)}
          className={`${FIELD} mt-1`}
        >
          {TYPE_GROUPS.map((group) => (
            <optgroup key={group.label} label={group.label}>
              {group.types.map((accountType) => (
                <option key={accountType} value={accountType}>
                  {TYPE_LABELS[accountType]}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>

      <div className="flex items-end gap-3">
        <label className="block w-28 text-xs text-muted">
          Currency
          <input
            value={currency}
            onChange={(e) => setCurrency(e.target.value.toUpperCase())}
            maxLength={3}
            placeholder="CAD"
            className={`${FIELD} mt-1 uppercase`}
          />
        </label>
        {/* Derived badge (docs/14 C1): read-only mirror of the server rule. */}
        <Badge tone={isTaxableType(type) ? "warn" : "success"} dot={false}>
          {isTaxableType(type) ? "Taxable" : "Registered"}
        </Badge>
      </div>

      {(error ?? localError) !== null && (
        <p className="text-sm text-danger">{error ?? localError}</p>
      )}

      <Button type="submit" variant="primary" loading={busy}>
        {submitLabel ?? (initial === null ? "Create account" : "Save changes")}
      </Button>
    </form>
  );
}

// --- Section ------------------------------------------------------------------

/**
 * M95 P3 — the seven tables `DELETE /accounts/{id}` pre-checks before it will
 * remove anything (docs/29 F3, docs/17): transactions, trades, ACB pools
 * (tax lots), import batches, debts, goals, reconciliation items. Budgets are
 * deliberately NOT here — they hang off categories, never off an account
 * (docs/02), so the "N transactions, N lots, M budgets" shorthand this card
 * asked to verify was wrong about budgets and wrong about everything missing
 * from this list.
 */
const BLAST_RADIUS = [
  { key: "transactions", label: "transactions" },
  { key: "trades", label: "trades" },
  { key: "lots", label: "tax lots" },
  { key: "batches", label: "import batches" },
  { key: "debts", label: "debts" },
  { key: "goals", label: "goals" },
  { key: "reconciliation", label: "reconciliation items" },
] as const;

type BlastKey = (typeof BLAST_RADIUS)[number]["key"];
type BlastCounts = Record<BlastKey, number>;

/** Live counts for the confirm dialog — same population the server checks. */
async function fetchBlastRadius(accountId: number): Promise<BlastCounts> {
  const [transactions, trades, lots, batches, debts, goals, reconciliation] =
    await Promise.all([
      listTransactions({ accountId }, { limit: 1, offset: 0 }),
      listTrades({ account_id: accountId, limit: 1, offset: 0 }),
      listAcbPools({ account_id: accountId, limit: 1, offset: 0 }),
      listImports({ limit: 200, offset: 0, account_id: accountId }),
      listDebts(),
      listGoals(),
      getReconciliation(accountId),
    ]);
  return {
    transactions: transactions.total,
    trades: trades.total,
    lots: lots.total,
    batches: batches.total,
    debts: debts.filter((debt) => debt.account_id === accountId).length,
    goals: goals.filter((goal) => goal.account_id === accountId).length,
    reconciliation: reconciliation.summary.total,
  };
}

function emptyBlastRadius(): BlastCounts {
  return {
    transactions: 0,
    trades: 0,
    lots: 0,
    batches: 0,
    debts: 0,
    goals: 0,
    reconciliation: 0,
  };
}

function countTotal(counts: BlastCounts): number {
  return BLAST_RADIUS.reduce((sum, row) => sum + counts[row.key], 0);
}

interface InstitutionGroup {
  institution: Institution | null;
  accounts: Account[];
}

export function AccountsSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const institutions = useQuery({
    queryKey: ["institutions"],
    queryFn: () => listInstitutions(200),
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => listAccounts(200),
  });

  /** undefined = closed; null = creating; Account = editing. */
  const [editorTarget, setEditorTarget] = useState<Account | null | undefined>(undefined);
  const [formError, setFormError] = useState<string | null>(null);

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    void queryClient.invalidateQueries({ queryKey: ["net-worth"] });
  }

  const save = useMutation({
    mutationFn: ({ target, input }: { target: Account | null; input: AccountCreateInput }) =>
      target === null
        ? createAccount(input)
        : updateAccount(target.id, {
            name: input.name,
            type: input.type,
            currency: input.currency,
            institution_id: input.institution_id,
          }),
    onSuccess: (saved, variables) => {
      invalidate();
      setEditorTarget(undefined);
      setFormError(null);
      toast.success(
        variables.target === null ? "Account created" : "Account updated",
        saved.name,
      );
    },
    onError: (err) => setFormError(apiErrorMessage(err)),
  });

  // M95 P3 (docs/09 §Destructive actions): the delete is two-stage. Stage 1
  // hides the row and offers a 5s Undo in a snackbar — nothing has been sent.
  // Stage 2 (window closed / Dismiss) sends the real DELETE; a 409
  // `account_in_use` comes back verbatim and the row is restored.
  const [pendingDeletes, setPendingDeletes] = useState<Set<number>>(
    () => new Set(),
  );
  /** The account whose confirm dialog is open. */
  const [confirmTarget, setConfirmTarget] = useState<Account | null>(null);
  /** Live blast-radius counts; null = still checking. */
  const [deps, setDeps] = useState<{ counts: BlastCounts; unknown: boolean } | null>(
    null,
  );

  // Fetch the blast radius fresh every time the dialog opens.
  useEffect(() => {
    if (confirmTarget === null) return;
    let cancelled = false;
    setDeps(null);
    fetchBlastRadius(confirmTarget.id)
      .then((counts) => {
        if (!cancelled) setDeps({ counts, unknown: false });
      })
      .catch(() => {
        // Unverifiable references must not BLOCK the user — the server is the
        // arbiter (it still refuses with 409 if anything is attached).
        if (!cancelled) setDeps({ counts: emptyBlastRadius(), unknown: true });
      });
    return () => {
      cancelled = true;
    };
  }, [confirmTarget]);

  function confirmDelete(): void {
    const target = confirmTarget;
    if (target === null) return;
    setConfirmTarget(null);
    deferDelete(toast, {
      title: "Deleted account",
      detail: target.name,
      hide: () =>
        setPendingDeletes((prev) => {
          const next = new Set(prev);
          next.add(target.id);
          return next;
        }),
      restore: () =>
        setPendingDeletes((prev) => {
          const next = new Set(prev);
          next.delete(target.id);
          return next;
        }),
      commit: () => deleteAccount(target.id),
      onCommitted: invalidate,
      onError: (message) => toast.error("Delete failed", message),
    });
  }

  // Join client-side; ungrouped accounts sort last under "No institution".
  // Rows with a PENDING delete are already gone from the UI (stage 1).
  const visibleAccounts = (accounts.data?.items ?? []).filter(
    (account) => !pendingDeletes.has(account.id),
  );
  const groups: InstitutionGroup[] = [];
  const byInstitution = new Map<number, Account[]>();
  for (const account of visibleAccounts) {
    if (account.institution_id === null) continue;
    const bucket = byInstitution.get(account.institution_id);
    if (bucket === undefined) {
      byInstitution.set(account.institution_id, [account]);
    } else {
      bucket.push(account);
    }
  }
  for (const institution of institutions.data?.items ?? []) {
    const bucket = byInstitution.get(institution.id);
    if (bucket !== undefined) {
      groups.push({ institution, accounts: bucket });
      byInstitution.delete(institution.id);
    }
  }
  const ungrouped = visibleAccounts.filter(
    (account) => account.institution_id === null,
  );
  if (ungrouped.length > 0) groups.push({ institution: null, accounts: ungrouped });

  // Close on Escape like every other surface-level dialog.
  useEffect(() => {
    if (editorTarget === undefined) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setEditorTarget(undefined);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editorTarget]);

  // Focus moves into the dialog on open and back to "New account" on close
  // (docs/09 Keyboard contract).
  const editorDialogRef = useRef<HTMLDivElement | null>(null);
  useDialogFocus(editorDialogRef, editorTarget !== undefined);

  // Same dialog contract for the delete confirm: focus in on open, back to the
  // trash button on close, Escape closes it (registered once per `enabled`).
  const confirmDialogRef = useRef<HTMLDivElement | null>(null);
  useDialogFocus(confirmDialogRef, confirmTarget !== null);
  useEscapeToClose(() => setConfirmTarget(null), confirmTarget !== null);
  const blocked =
    deps !== null && !deps.unknown && countTotal(deps.counts) > 0;

  return (
    <Card hover className="p-4">
      <CardTitle
        right={
          <Button size="sm" variant="primary" onClick={() => setEditorTarget(null)}>
            <Plus className="size-4" aria-hidden />
            New account
          </Button>
        }
      >
        Accounts
      </CardTitle>
      <p className="-mt-1 mb-3 text-sm text-muted">
        Grouped by institution · cash balances are committed transactions in each account&apos;s
        currency.
      </p>

      {accounts.isPending && <SkeletonRows rows={3} />}
      {accounts.isError && <QueryErrorPanel query={accounts} />}

      {!accounts.isPending && !accounts.isError && groups.length === 0 && (
        <EmptyState
          icon={<Wallet />}
          title="No accounts yet"
          hint="Create your first account here, or add one inline from the Imports picker."
          preview={{
            src: "/help/settings-accounts.png",
            alt: "Settings with the accounts and institutions manager",
          }}
          action={{
            label: "New account",
            onClick: () => setEditorTarget(null),
            ariaLabel: "New account — No accounts yet",
          }}
        />
      )}

      <div className="space-y-4">
        {groups.map(({ institution, accounts: rows }) => (
          <motion.div key={institution?.id ?? "none"} variants={fadeUp}>
            <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-widest text-muted">
              {institution === null ? (
                <>
                  <Landmark className="size-3.5" aria-hidden />
                  No institution
                </>
              ) : (
                institution.name
              )}
            </h3>
            <ul className="space-y-2">
              {rows.map((account) => (
                <li
                  key={account.id}
                  className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 rounded-lg border border-veil/[0.06] px-3 py-2 text-sm transition-colors hover:border-veil/[0.12]"
                >
                  <span className="flex min-w-0 flex-wrap items-center gap-2">
                    <Link
                      to={`/accounts/${account.id}`}
                      className="truncate font-medium hover:text-accent transition-colors"
                    >
                      {account.name}
                    </Link>
                    <Badge tone="neutral" dot={false}>
                      {TYPE_LABELS[account.type]}
                    </Badge>
                    <Badge
                      tone={isTaxableType(account.type) ? "warn" : "success"}
                      dot={false}
                      pulse={false}
                    >
                      {isTaxableType(account.type) ? "Taxable" : "Registered"}
                    </Badge>
                    <span className="text-xs uppercase tracking-wide text-muted">
                      {account.currency}
                    </span>
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="mr-2 tabular font-medium">
                      {/* M145: null = no honest figure — a dash, never "null". */}
                      {account.cash_balance ?? "—"}
                    </span>
                    {/* M68: reconciliation was reachable only from Account
                        detail; the accounts list carries it too. */}
                    <Link
                      to={`/reconciliation/${account.id}`}
                      aria-label={`Reconcile ${account.name}`}
                      className="mr-1 rounded-md px-2 py-1 text-xs text-accent transition-colors hover:bg-veil/[0.06] hover:underline focus-visible:ring-2 focus-visible:ring-accent outline-none"
                    >
                      Reconcile
                    </Link>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Edit ${account.name}`}
                      onClick={() => {
                        setFormError(null);
                        setEditorTarget(account);
                      }}
                    >
                      <Pencil className="size-3.5" aria-hidden />
                      Edit
                    </Button>
                    <Button
                      variant="danger"
                      size="sm"
                      aria-label={`Delete ${account.name}`}
                      onClick={() => {
                        setFormError(null);
                        setConfirmTarget(account);
                      }}
                    >
                      <Trash2 className="size-3.5" aria-hidden />
                    </Button>
                  </span>
                </li>
              ))}
            </ul>
          </motion.div>
        ))}
      </div>

      {/* Create/edit dialog.
          NOTE: never wrap `createPortal(...)` in `<AnimatePresence>` — framer's
          onlyElements() keeps only React.isValidElement() children, and portals
          are NOT elements, so the dialog silently never rendered (found by M95
          P3; live-verified against the running stack). A plain conditional +
          motion initial/animate gives the open transition; exit transitions
          need a portal-returning component (the LinkModal idiom) instead. */}
      {editorTarget !== undefined &&
          createPortal(
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="fixed inset-0 z-[90] flex items-center justify-center bg-scrim p-4 backdrop-blur-sm"
              onClick={() => setEditorTarget(undefined)}
            >
              <motion.div
                ref={editorDialogRef}
                initial={{ scale: 0.96, y: 8 }}
                animate={{ scale: 1, y: 0 }}
                exit={{ scale: 0.96, y: 8 }}
                transition={{ type: "spring", stiffness: 320, damping: 26 }}
                className="w-full max-w-md outline-none"
                role="dialog"
                aria-modal="true"
                aria-labelledby="account-editor-title"
                tabIndex={-1}
                onClick={(event) => event.stopPropagation()}
              >
                <Card hover={false} className="p-4">
                  <CardTitle>
                    <span id="account-editor-title">
                      {editorTarget === null ? "New account" : "Edit account"}
                    </span>
                  </CardTitle>
                  <AccountForm
                    institutions={institutions.data?.items ?? []}
                    initial={editorTarget}
                    pending={save.isPending}
                    error={formError}
                    onSubmit={(input) => save.mutate({ target: editorTarget, input })}
                  />
                </Card>
              </motion.div>
            </motion.div>,
            document.body,
          )}

      {/* M95 P3 — the blast radius is spelled out BEFORE anything is sent
          (docs/09 §Destructive actions). Every table the server pre-checks is
          listed with its live count; a single attached row disables the button
          and quotes the server's refusal verbatim, because that is exactly
          what `DELETE /accounts/{id}` would answer. */}
      {confirmTarget !== null &&
          createPortal(
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="fixed inset-0 z-[90] flex items-center justify-center bg-scrim p-4 backdrop-blur-sm"
              onClick={() => setConfirmTarget(null)}
            >
              <motion.div
                ref={confirmDialogRef}
                initial={{ scale: 0.96, y: 8 }}
                animate={{ scale: 1, y: 0 }}
                exit={{ scale: 0.96, y: 8 }}
                transition={{ type: "spring", stiffness: 320, damping: 26 }}
                className="w-full max-w-md outline-none"
                role="dialog"
                aria-modal="true"
                aria-labelledby="delete-account-title"
                tabIndex={-1}
                onClick={(event) => event.stopPropagation()}
              >
                <Card hover={false} className="p-4">
                  <CardTitle>
                    <span id="delete-account-title">
                      Delete {confirmTarget.name}?
                    </span>
                  </CardTitle>
                  <p className="-mt-1 mb-2 text-sm text-muted">
                    Everything attached to this account goes with it:
                  </p>
                  {deps === null && (
                    <p
                      className="text-sm text-muted"
                      data-testid="blast-loading"
                      role="status"
                    >
                      Checking references…
                    </p>
                  )}
                  {deps !== null && (
                    <ul
                      className="space-y-1 text-sm"
                      data-testid="delete-account-blast"
                    >
                      {BLAST_RADIUS.map((row) => (
                        <li
                          key={row.key}
                          data-testid={`blast-${row.key}`}
                          className="flex items-baseline justify-between gap-3"
                        >
                          <span className="text-muted">{row.label}</span>
                          <span className="tabular font-medium">
                            {deps.unknown ? "?" : deps.counts[row.key]}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                  {deps?.unknown === true && (
                    <p className="mt-2 text-xs text-muted">
                      Couldn’t check references here — the server will decide.
                    </p>
                  )}
                  {blocked && (
                    <p
                      className="mt-2 text-sm font-medium text-danger"
                      data-testid="blast-blocked"
                    >
                      Account is in use; reassign references first
                    </p>
                  )}
                  <div className="mt-4 flex justify-end gap-2">
                    <Button variant="ghost" onClick={() => setConfirmTarget(null)}>
                      Cancel
                    </Button>
                    <Button
                      variant="danger"
                      data-testid="confirm-delete-account"
                      disabled={deps === null || blocked}
                      onClick={confirmDelete}
                    >
                      Delete account
                    </Button>
                  </div>
                </Card>
              </motion.div>
            </motion.div>,
            document.body,
          )}
    </Card>
  );
}
