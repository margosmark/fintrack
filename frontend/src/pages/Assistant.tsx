import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History } from "lucide-react";
import {
  askAssistant,
  applyProposal,
  dismissProposal,
  listHistory,
  undoProposal,
} from "../api/assistant";
import type { Proposal } from "../api/assistant";
import { listCategories } from "../api/categories";
import { ProposedChangeCard } from "../components/assistant/ProposedChangeCard";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { useToast } from "../components/ui/Toast";
import { apiErrorMessage } from "../components/transactions/format";
import { useMasked } from "../lib/mask";

// M188 (docs/57 §6): ask in plain English → the answer → a Proposed Change
// card you Apply or Dismiss → History with Undo. The server owns every write;
// this page only renders what it returns and posts the four transitions.

export function Assistant() {
  // Re-render on a mask flip — the card masks money fields at call time.
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [undoingId, setUndoingId] = useState<number | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const history = useQuery({
    queryKey: ["assistant", "history"],
    queryFn: () => listHistory(),
  });
  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: () => listCategories(200),
  });

  const categoryName = (id: number): string | undefined =>
    categories.data?.items.find((c) => c.id === id)?.name;

  const historyItems = history.data?.items ?? [];

  const ask = useMutation({
    mutationKey: ["assistant", "ask"],
    mutationFn: (q: string) => askAssistant(q),
    onSuccess: (res) => {
      setAnswer(res.answer);
      setProposal(res.proposal);
      setRefusal(null);
    },
    onError: (err: Error) => {
      // A refusal is an honest outcome (docs/57 §5): the envelope detail,
      // "nothing was applied", and NO card — never a silent partial write.
      setAnswer(null);
      setProposal(null);
      setRefusal(apiErrorMessage(err));
    },
  });

  const apply = useMutation({
    mutationFn: (id: number) => applyProposal(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["assistant", "history"] });
      setProposal(null);
      toast.success("Change applied", "It's in History — undo any time.");
    },
    onError: (err: Error) => toast.error("Could not apply the change", apiErrorMessage(err)),
  });

  const dismiss = useMutation({
    mutationFn: (id: number) => dismissProposal(id),
    onSuccess: () => {
      setProposal(null);
      toast.info("Proposal dismissed", "Nothing was changed.");
    },
    onError: (err: Error) => toast.error("Could not dismiss the proposal", apiErrorMessage(err)),
  });

  const undo = useMutation({
    mutationFn: (id: number) => undoProposal(id),
    onMutate: (id: number) => setUndoingId(id),
    onSettled: () => setUndoingId(null),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["assistant", "history"] });
      toast.success("Change undone", "The previous state is restored.");
    },
    onError: (err: Error) => toast.error("Could not undo the change", apiErrorMessage(err)),
  });

  function submit() {
    const q = question.trim();
    if (!q || ask.isPending) return;
    ask.mutate(q);
  }

  return (
    <section className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">Assistant</h1>

      <Card className="space-y-3">
        <label htmlFor="assistant-question" className="text-sm font-medium">
          Ask in plain English
        </label>
        <textarea
          id="assistant-question"
          ref={inputRef}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={2}
          maxLength={2000}
          placeholder="How much did I spend on groceries last month?"
          className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm"
          data-testid="assistant-question"
        />
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted">Enter to ask · Shift+Enter for a new line</p>
          <Button
            variant="primary"
            onClick={submit}
            loading={ask.isPending}
            data-testid="assistant-ask"
          >
            Ask
          </Button>
        </div>
      </Card>

      {refusal !== null && (
        <div
          role="status"
          data-testid="assistant-refusal"
          className="rounded-lg border border-border bg-veil/[0.04] px-4 py-3 text-sm"
        >
          <p className="font-medium">Nothing was applied.</p>
          <p className="text-muted">{refusal}</p>
        </div>
      )}

      {answer !== null && refusal === null && (
        <Card data-testid="assistant-answer" className="whitespace-pre-wrap text-sm">
          {answer}
        </Card>
      )}

      {proposal !== null && refusal === null && (
        <ProposedChangeCard
          proposal={proposal}
          categoryName={categoryName}
          onApply={() => apply.mutate(proposal.id)}
          onDismiss={() => dismiss.mutate(proposal.id)}
          busy={apply.isPending || dismiss.isPending}
        />
      )}

      <Card>
        <CardTitle right={<span className="text-xs text-muted">Newest first</span>}>
          History
        </CardTitle>
        {history.isPending ? (
          <SkeletonRows rows={3} />
        ) : historyItems.length === 0 ? (
          <EmptyState
            icon={<History />}
            title="No changes yet"
            preview={{ none: "Applied changes land here with an Undo button." }}
            action={{ label: "Ask a question", onClick: () => inputRef.current?.focus() }}
          />
        ) : (
          <ul className="divide-y divide-border" data-testid="assistant-history">
            {historyItems.map((item) => (
              <li
                key={item.id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
                data-testid="assistant-history-row"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{item.target_label}</p>
                  <p className="text-xs text-muted">{item.summary}</p>
                  <p className="text-xs text-muted">
                    {item.status === "applied" ? "Applied" : "Undone"}
                    {item.applied_at ? ` · ${new Date(item.applied_at).toLocaleString()}` : ""}
                  </p>
                </div>
                {item.status === "applied" ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => undo.mutate(item.id)}
                    loading={undo.isPending && undoingId === item.id}
                    data-testid="assistant-undo"
                  >
                    Undo
                  </Button>
                ) : (
                  <span className="text-xs text-muted" data-testid="assistant-undone">
                    Undone
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </section>
  );
}
