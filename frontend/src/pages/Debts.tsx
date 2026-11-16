import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { CreditCard, Plus, Trash2 } from "lucide-react";
import { listDebts, createDebt, deleteDebt, debtProjection } from "../api/debts";
import { listAccounts } from "../api/accounts";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { useToast } from "../components/ui/Toast";
import { apiErrorMessage } from "../components/transactions/format";
import { useMasked } from "../lib/mask";
import { formatCad } from "../lib/format";

const DEBT_TYPES = ["mortgage", "credit_card", "loan", "student_loan", "other"];

export function Debts() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [accountId, setAccountId] = useState("");
  const [debtType, setDebtType] = useState("credit_card");
  const [rate, setRate] = useState("");
  const [payment, setPayment] = useState("");

  const debts = useQuery({ queryKey: ["debts"], queryFn: listDebts });
  const projection = useQuery({ queryKey: ["debt-projection"], queryFn: debtProjection });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => listAccounts(200) });

  const create = useMutation({
    mutationFn: () => createDebt({
      name: name.trim(),
      account_id: parseInt(accountId),
      debt_type: debtType,
      interest_rate: rate ? parseFloat(rate) : undefined,
      minimum_payment: payment ? parseFloat(payment) : undefined,
      payment_frequency: "monthly",
    }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["debts"] }); queryClient.invalidateQueries({ queryKey: ["debt-projection"] }); toast.success("Debt added"); resetForm(); },
    onError: (e: Error) => toast.error("Error", e.message),
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteDebt(id),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["debts"] }); queryClient.invalidateQueries({ queryKey: ["debt-projection"] }); toast.success("Debt removed"); },
    onError: (e: Error) => toast.error("Error", apiErrorMessage(e)),
  });

  function resetForm() { setName(""); setAccountId(""); setRate(""); setPayment("");  setShowForm(false); }

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">Debts</h1>
        <Button onClick={() => { resetForm(); setShowForm(!showForm); }}>
          <Plus className="size-4 mr-1" /> Add debt
        </Button>
      </div>

      {projection.data && projection.data.debts.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-3">
          <Card className="p-4">
            <div className="text-xs text-muted uppercase">Total Debt</div>
            <div className="text-lg font-semibold text-danger">{formatCad(projection.data.total_debt)}</div>
          </Card>
          <Card className="p-4">
            <div className="text-xs text-muted uppercase">Total Interest</div>
            <div className="text-lg font-semibold text-text">{formatCad(projection.data.total_interest)}</div>
          </Card>
          <Card className="p-4">
            <div className="text-xs text-muted uppercase">Debts</div>
            <div className="text-lg font-semibold">{projection.data.debts.length}</div>
          </Card>
        </div>
      )}

      {showForm && (
        <Card className="p-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <input aria-label="Debt name" placeholder="Debt name" value={name} onChange={e => setName(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm" />
            <select aria-label="Linked account" value={accountId} onChange={e => setAccountId(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm">
              <option value="">Link to account</option>
              {accounts.data?.items?.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
            <select aria-label="Debt type" value={debtType} onChange={e => setDebtType(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm">
              {DEBT_TYPES.map(t => <option key={t} value={t}>{t.replace("_", " ")}</option>)}
            </select>
            <input aria-label="Interest rate %" placeholder="Interest rate %" type="number" step="0.01" value={rate} onChange={e => setRate(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm" />
            <input aria-label="Min payment" placeholder="Min payment" type="number" value={payment} onChange={e => setPayment(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm" />
          </div>
          <div className="flex gap-2 mt-3">
            <Button onClick={() => create.mutate()} disabled={!name || !accountId}>Create</Button>
            <Button variant="ghost" onClick={resetForm}>Cancel</Button>
          </div>
        </Card>
      )}

      {debts.isPending ? <SkeletonRows rows={3} /> : debts.data?.length === 0 ? (
        <EmptyState
          icon={<CreditCard />}
          title="No debts"
          hint="Add mortgages, loans, or credit cards to track payoff progress."
          preview={{
            src: "/help/debts.png",
            alt: "The Debts page with balances and payoff progress",
          }}
          action={{
            label: "Add debt",
            onClick: () => {
              resetForm();
              setShowForm(true);
            },
            ariaLabel: "Add debt — No debts",
          }}
        />
      ) : (
        <motion.div variants={staggerContainer(0.05)} initial="hidden" animate="show" className="space-y-2">
          {debts.data?.map(d => {
            const proj = projection.data?.debts.find(p => p.debt_id === d.id);
            return (
              <motion.div key={d.id} variants={fadeUp}>
                <Card className="flex items-center justify-between p-4">
                  <div>
                    <div className="font-medium text-text">{d.name}</div>
                    <div className="text-xs text-muted">{d.debt_type.replace("_", " ")} · {d.interest_rate ? `${d.interest_rate}%` : "No rate"}</div>
                  </div>
                  <div className="flex items-center gap-3">
                    {proj && (
                      <div className="text-right text-sm">
                        <div className="text-danger font-medium">{formatCad(proj.balance)}</div>
                        {proj.months && <div className="text-xs text-muted">{proj.months} months</div>}
                      </div>
                    )}
                    <button onClick={() => remove.mutate(d.id)} title="Delete" aria-label={`Delete debt — ${d.name}`}
                      className="p-1 rounded hover:bg-surface-raised">
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
