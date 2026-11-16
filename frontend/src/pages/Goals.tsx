import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Target, Plus, Pencil, Trash2 } from "lucide-react";
import { listGoals, createGoal, updateGoal, deleteGoal } from "../api/goals";
import type { GoalWithProgress } from "../api/goals";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { useToast } from "../components/ui/Toast";
import { apiErrorMessage } from "../components/transactions/format";
import { useMasked } from "../lib/mask";
import { formatCad } from "../lib/format";

/** Whole-dollar beside a progress bar (docs/09 money displays). */
const WHOLE = { fractionDigits: 0 } as const;

const STATUS_COLORS: Record<string, string> = {
  on_track: "text-gain",
  ahead: "text-gain",
  behind: "text-danger",
  completed: "text-accent",
};

export function Goals() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const [showForm, setShowForm] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [date, setDate] = useState("");
  const [override, setOverride] = useState("");

  const goals = useQuery({ queryKey: ["goals"], queryFn: listGoals });

  const create = useMutation({
    mutationFn: () => createGoal({
      name: name.trim(),
      target_amount: parseFloat(target),
      target_date: date || undefined,
      current_amount_override: override ? parseFloat(override) : undefined,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      toast.success("Goal created", "Your savings goal has been added.");
      resetForm();
    },
    onError: (e: Error) => toast.error("Could not create goal", e.message),
  });

  const update = useMutation({
    mutationFn: () => updateGoal(editId!, {
      name: name.trim(),
      target_amount: parseFloat(target),
      target_date: date || undefined,
      current_amount_override: override ? parseFloat(override) : undefined,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      toast.success("Goal updated", "Your savings goal has been updated.");
      resetForm();
    },
    onError: (e: Error) => toast.error("Could not update goal", e.message),
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteGoal(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      toast.success("Goal deleted", "The savings goal has been removed.");
    },
    onError: (e: Error) => toast.error("Could not delete goal", apiErrorMessage(e)),
  });

  function resetForm() {
    setName(""); setTarget(""); setDate(""); setOverride("");
    setEditId(null); setShowForm(false);
  }

  function startEdit(g: GoalWithProgress) {
    setEditId(g.id); setName(g.name); setTarget(String(g.target_amount));
    setDate(g.target_date || ""); setOverride(g.current_amount_override ? String(g.current_amount_override) : "");
    setShowForm(true);
  }

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">Goals</h1>
        <Button onClick={() => { resetForm(); setShowForm(!showForm); }}>
          <Plus className="size-4 mr-1" /> New goal
        </Button>
      </div>

      {showForm && (
        <Card className="p-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <input aria-label="Goal name" placeholder="Goal name" value={name} onChange={e => setName(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm" />
            <input aria-label="Target amount" placeholder="Target amount" type="number" value={target} onChange={e => setTarget(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm" />
            <input aria-label="Target date (optional)" placeholder="Target date (optional)" type="date" value={date} onChange={e => setDate(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm" />
            <input aria-label="Current amount (optional)" placeholder="Current amount (optional)" type="number" value={override} onChange={e => setOverride(e.target.value)}
              className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm" />
          </div>
          <div className="flex gap-2 mt-3">
            <Button onClick={() => editId ? update.mutate() : create.mutate()} disabled={!name || !target}>
              {editId ? "Update" : "Create"}
            </Button>
            <Button variant="ghost" onClick={resetForm}>Cancel</Button>
          </div>
        </Card>
      )}

      {goals.isPending ? <SkeletonRows rows={3} /> : goals.data?.length === 0 ? (
        <EmptyState
          icon={<Target />}
          title="No goals yet"
          hint="Create a savings goal to track your progress."
          preview={{
            src: "/help/goals.png",
            alt: "The Goals page with savings goals and progress bars",
          }}
          action={{
            label: "New goal",
            onClick: () => {
              resetForm();
              setShowForm(true);
            },
            ariaLabel: "New goal — No goals yet",
          }}
        />
      ) : (
        <motion.div variants={staggerContainer(0.05)} initial="hidden" animate="show" className="space-y-3">
          {goals.data?.map(g => (
            <motion.div key={g.id} variants={fadeUp}>
              <Card className="p-4">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-text">{g.name}</span>
                      <span className={`text-xs font-medium ${STATUS_COLORS[g.status] || "text-muted"}`}>
                        {g.status.replace("_", " ")}
                      </span>
                    </div>
                    <div className="mt-2 flex items-baseline gap-2">
                      <span className="text-lg font-semibold text-text">{formatCad(g.current_amount, undefined, WHOLE)}</span>
                      <span className="text-sm text-muted">/ {formatCad(g.target_amount, undefined, WHOLE)}</span>
                    </div>
                    <div className="mt-2 w-full h-2 rounded-full bg-surface-raised overflow-hidden">
                      <div className="h-full rounded-full bg-gain transition-all" style={{ width: `${Math.min(g.progress_pct, 100)}%` }} />
                    </div>
                    <div className="flex justify-between mt-1 text-xs text-muted">
                      <span>{g.progress_pct}%</span>
                      {g.monthly_needed && <span>{formatCad(g.monthly_needed, undefined, WHOLE)}/mo needed</span>}
                      {g.target_date && <span>Target: {g.target_date}</span>}
                    </div>
                  </div>
                  <div className="flex gap-1 ml-4">
                    <button onClick={() => startEdit(g)} title="Edit" aria-label={`Edit goal — ${g.name}`}
                      className="p-1 rounded hover:bg-surface-raised"><Pencil className="size-4 text-muted" /></button>
                    <button onClick={() => remove.mutate(g.id)} title="Delete" aria-label={`Delete goal — ${g.name}`}
                      className="p-1 rounded hover:bg-surface-raised"><Trash2 className="size-4 text-danger" /></button>
                  </div>
                </div>
              </Card>
            </motion.div>
          ))}
        </motion.div>
      )}
    </section>
  );
}
