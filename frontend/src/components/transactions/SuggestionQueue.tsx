// Suggestion queue side panel (docs/09): pending LLM categorization proposals
// with inline accept/reject; accepts feed the learning loop (docs/04) and
// change transaction categories, so both queries are invalidated.
// Presentation pass: glass card, confidence badge, toast feedback.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Sparkles } from "lucide-react";
import {
  Suggestion,
  acceptSuggestion,
  listSuggestions,
  rejectSuggestion,
  runSuggestions,
} from "../../api/suggestions";
import { apiErrorMessage } from "./format";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { HelpHint } from "../ui/HelpHint";
import { RangePicker, rangeFor, type DateRange } from "../ui/RangePicker";
import { SegmentedControl } from "../ui/SegmentedControl";
import { SkeletonRows } from "../ui/Skeleton";
import { fadeUp, staggerContainer } from "../ui/motion";
import { useToast } from "../ui/Toast";

export function SuggestionQueue() {
  const queryClient = useQueryClient();
  const toast = useToast();

  const [mode, setMode] = useState<"all" | "range">("all");
  const [range, setRange] = useState<DateRange>(() => rangeFor("YTD"));

  const queue = useQuery({
    queryKey: ["suggestions", "suggested"],
    queryFn: () => listSuggestions("suggested", 200),
  });

  const run = useMutation({
    mutationFn: () =>
      mode === "range"
        ? runSuggestions({ from: range.from, to: range.to })
        : runSuggestions(),
    onSuccess: (data) => {
      // Layers 1-2 apply inline (rules + learned map) — transactions change too.
      void queryClient.invalidateQueries({ queryKey: ["suggestions"] });
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      toast.success(
        data.queued > 0
          ? `Categorizer run queued — ${data.queued} LLM batch${data.queued === 1 ? "" : "es"}`
          : "Categorizer run — rules and learned matches applied",
      );
    },
    onError: (err) => toast.error("Categorizer run failed", apiErrorMessage(err)),
  });

  const act = useMutation({
    mutationFn: ({ id, accept }: { id: number; accept: boolean }) =>
      accept ? acceptSuggestion(id) : rejectSuggestion(id),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["suggestions"] });
      // Acceptance re-categorizes the underlying transaction.
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      if (variables.accept) {
        toast.success("Suggestion accepted", "Transaction re-categorized.");
      } else {
        toast.info("Suggestion rejected");
      }
    },
    onError: (err) => toast.error("Suggestion update failed", apiErrorMessage(err)),
  });

  const items = queue.data?.items ?? [];

  return (
    <aside className="glass h-fit rounded-xl p-4 lg:sticky lg:top-6">
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        <Sparkles className="size-4 text-accent" aria-hidden />
        Suggestions{" "}
        <span className="font-normal text-muted tabular">({queue.data?.total ?? 0} queued)</span>
        <HelpHint text="Suggestions come from your rules first, then learned payees, then the assistant. Confirming teaches the app your preference." />
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto"
          loading={run.isPending}
          onClick={() => run.mutate()}
        >
          Run
        </Button>
      </h2>
      <div className="mt-2 flex items-center gap-2">
        <SegmentedControl
          options={[
            { value: "all", label: "All" },
            { value: "range", label: "Date range" },
          ]}
          value={mode}
          onChange={(v) => setMode(v)}
          layoutId="suggestion-queue-mode"
          size="sm"
        />
        {mode === "range" && (
          <RangePicker
            from={range.from}
            to={range.to}
            onChange={setRange}
            size="sm"
          />
        )}
      </div>
      {queue.isPending && (
        <div className="mt-3">
          <SkeletonRows rows={3} />
        </div>
      )}
      {queue.isError && (
        <div className="mt-3">
          <QueryErrorPanel query={queue} />
        </div>
      )}
      {!queue.isPending && !queue.isError && items.length === 0 && (
        <div className="mt-1">
          <EmptyState
            icon={<Sparkles />}
            title="No pending suggestions"
            hint="Press Run to propose categories for uncategorized transactions (rules, learned matches, then the LLM)."
            preview={{ none: "the demo tenant has zero pending suggestions" }}
            action={{
              label: "Run",
              onClick: () => run.mutate(),
              ariaLabel: "Run — No pending suggestions",
            }}
          />
        </div>
      )}
      <motion.ul
        variants={staggerContainer(0.04)}
        initial="hidden"
        animate="show"
        className="mt-3 space-y-2.5"
      >
        {items.map((suggestion: Suggestion) => (
          <motion.li key={suggestion.id} variants={fadeUp}>
            <div className="rounded-lg border border-veil/[0.06] bg-veil/[0.03] p-2.5 text-sm transition-colors hover:border-veil/[0.12]">
              <p className="font-medium">{suggestion.payee}</p>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-muted">
                <span>→ {suggestion.category_name}</span>
                <Badge tone={suggestion.confidence >= 0.8 ? "success" : "warn"} dot={false}>
                  {Math.round(suggestion.confidence * 100)}%
                </Badge>
                <span className="text-xs">{suggestion.model_used}</span>
              </div>
              {act.isError && act.variables?.id === suggestion.id && (
                <p className="mt-1 text-xs text-danger">{apiErrorMessage(act.error)}</p>
              )}
              <div className="mt-2 flex gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  disabled={act.isPending}
                  onClick={() => act.mutate({ id: suggestion.id, accept: true })}
                >
                  Accept
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={act.isPending}
                  onClick={() => act.mutate({ id: suggestion.id, accept: false })}
                >
                  Reject
                </Button>
              </div>
            </div>
          </motion.li>
        ))}
      </motion.ul>
    </aside>
  );
}
