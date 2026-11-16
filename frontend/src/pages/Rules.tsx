// Rules (docs/09): full-page categorization rules editor — table layout with
// priority-ordered rules, create/edit form supporting all match types
// (payee-based + amount-based), tag assignment (set/add), category picker,
// and inline prefill-from-payee. Extracted from Settings.tsx RulesEditor and
// enhanced with amount matchers, tag multi-select, and tag action.

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  FolderInput,
  Pencil,
  ScrollText,
  Tag,
  Trash2,
} from "lucide-react";
import { listRules, createRule, updateRule, deleteRule } from "../api/rules";
import type { Rule, RuleMatchType, RulePayload } from "../api/rules";
import { RuleDryRun } from "../components/rules/RuleDryRun";
import { listCategories } from "../api/categories";
import { listTags, createTag } from "../api/tags";
import { provenance, readPrefill, type SearchCriteria } from "../lib/createFromSearch";
import { apiErrorMessage } from "../components/transactions/format";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { HelpHint } from "../components/ui/HelpHint";
import { SkeletonRows } from "../components/ui/Skeleton";
import { Toggle } from "../components/ui/Toggle";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { useToast } from "../components/ui/Toast";

// ── Constants ──────────────────────────────────────────────────────────────

const ALL_MATCH_TYPES: readonly RuleMatchType[] = [
  "regex",
  "exact_payee",
  "contains",
  "amount_equals",
  "amount_gt",
  "amount_lt",
  "amount_between",
];

const PAYEE_MATCHERS: Record<RuleMatchType, boolean> = {
  regex: true,
  exact_payee: true,
  contains: true,
  amount_equals: false,
  amount_gt: false,
  amount_lt: false,
  amount_between: false,
};

const AMOUNT_MATCHERS: Record<RuleMatchType, boolean> = {
  regex: false,
  exact_payee: false,
  contains: false,
  amount_equals: true,
  amount_gt: true,
  amount_lt: true,
  amount_between: true,
};

const FIELD =
  "field-ring w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors placeholder:text-muted hover:border-accent/70";

function escapeRegex(payee: string): string {
  return payee.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// ── Pattern input helpers ──────────────────────────────────────────────────

function patternLabel(matchType: RuleMatchType): string {
  if (matchType === "amount_between") return "Min | Max";
  if (AMOUNT_MATCHERS[matchType]) return "Amount";
  return "Pattern";
}

function patternPlaceholder(matchType: RuleMatchType): string {
  switch (matchType) {
    case "amount_between":
      return "e.g. 50|500";
    case "amount_equals":
      return "e.g. 99.95";
    case "amount_gt":
    case "amount_lt":
      return "e.g. 100";
    case "exact_payee":
      return "e.g. METRO INC";
    case "contains":
      return "e.g. amazon";
    case "regex":
      return "e.g. ^AMAZON\\s";
  }
}

// ── Form data shape ────────────────────────────────────────────────────────

interface RuleDraft {
  match_type: RuleMatchType;
  pattern: string;
  priority: number;
  category_id: number;
  enabled: boolean;
  tag_ids: number[];
  tag_action: string;
  stop_on_match: boolean;
}

const EMPTY_DRAFT: RuleDraft = {
  match_type: "contains",
  pattern: "",
  priority: 100,
  category_id: 0,
  enabled: true,
  tag_ids: [],
  tag_action: "set",
  stop_on_match: true,
};

function draftToPayload(draft: RuleDraft): RulePayload {
  return {
    match_type: draft.match_type,
    pattern: draft.pattern.trim(),
    priority: draft.priority,
    category_id: draft.category_id,
    enabled: draft.enabled,
    tag_ids: draft.tag_ids.length > 0 ? draft.tag_ids : undefined,
    tag_action: draft.tag_ids.length > 0 ? draft.tag_action : undefined,
    stop_on_match: draft.stop_on_match,
  };
}

// ── Main page ──────────────────────────────────────────────────────────────

export function Rules() {
  const queryClient = useQueryClient();
  const toast = useToast();

  const rules = useQuery({ queryKey: ["rules"], queryFn: () => listRules(200) });
  // Query-key contract (docs/09 "Query keys"): one exact key = one queryFn = one
  // payload shape. `listCategories` returns a page ({items,total,limit}), which is
  // the shape every other ["categories"] consumer reads — the ["categories","all"]
  // key belongs to Settings' fetchAllCategories (a bare Category[]) and sharing it
  // here made /settings crash with "t is not iterable" after a visit to /rules.
  const categories = useQuery({ queryKey: ["categories"], queryFn: () => listCategories(200) });
  const tags = useQuery({ queryKey: ["tags"], queryFn: () => listTags(200) });

  // ── Create form state ──────────────────────────────────────────────────
  const [draft, setDraft] = useState<RuleDraft>({ ...EMPTY_DRAFT });
  const [payee, setPayee] = useState("");
  const [editingRuleId, setEditingRuleId] = useState<number | null>(null);
  const [dryRunRuleId, setDryRunRuleId] = useState<number | null>(null);
  const [showRuleTagInput, setShowRuleTagInput] = useState(false);
  const [ruleTagName, setRuleTagName] = useState("");
  // ── M123 (docs/09 §Create from search): deep-link prefill ─────────────────
  const [searchParams, setSearchParams] = useSearchParams();
  /** Read once per mount — a later param change must not clobber a live draft. */
  const prefillRead = useRef(false);
  const [prefill, setPrefill] = useState<SearchCriteria | null>(null);
  useEffect(() => {
    if (prefillRead.current) return;
    prefillRead.current = true;
    const criteria = readPrefill(searchParams);
    if (criteria === null) return;
    setDraft((previous) => ({
      ...previous,
      // A rule's match term is a `contains` pattern — pin it so the form says
      // the same thing the deep link claimed. `uncategorized` has no id to
      // assign, so it is deliberately NOT applied (see `provenance`).
      ...(criteria.q !== undefined ? { pattern: criteria.q, match_type: "contains" as const } : {}),
      ...(criteria.category !== undefined && criteria.category !== "uncategorized"
        ? { category_id: Number(criteria.category) }
        : {}),
    }));
    setPrefill(criteria);
  }, [searchParams]);

  /** Computed at render so the category name is right even if it loads late. */
  const prefillProvenance =
    prefill === null
      ? null
      : provenance(prefill, "rule", (value) =>
          value === "uncategorized"
            ? "Uncategorized"
            : ((categories.data?.items ?? []).find((category) => String(category.id) === value)
                ?.name ?? `category ${value}`),
        );

  /** The deep link stays in the URL until the draft resolves, exactly like M55's
   *  `autofill` on /bills — so a reload brings the draft back, and a successful
   *  create stops it coming back with a rule that already exists. */
  function clearPrefill(): void {
    if (!prefillRead.current) return;
    const next = new URLSearchParams(searchParams);
    for (const key of ["prefill", "q", "category", "account", "tag"]) next.delete(key);
    setSearchParams(next, { replace: true });
    setPrefill(null);
  }

  const createTagState = useMutation({
    mutationFn: () => createTag({ name: ruleTagName.trim() }),
    onSuccess: (tag) => {
      void queryClient.invalidateQueries({ queryKey: ["tags"] });
      setDraft((d) => ({ ...d, tag_ids: [...d.tag_ids, tag.id] }));
      setRuleTagName("");
      setShowRuleTagInput(false);
    },
  });

  async function createRuleTag(): Promise<void> {
    if (ruleTagName.trim() === "") return;
    try {
      await createTagState.mutateAsync();
    } catch {
      // Surfaced via createTagState.isError below.
    }
  }

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["rules"] });
  };

  const categoryName = useMemo(
    () => new Map<number, string>((categories.data?.items ?? []).map((c) => [c.id, c.name])),
    [categories.data],
  );

  // ── Mutations ──────────────────────────────────────────────────────────

  const create = useMutation({
    mutationFn: (payload: RulePayload) => createRule(payload),
    onSuccess: () => {
      invalidate();
      setDraft({ ...EMPTY_DRAFT });
      setPayee("");
      setEditingRuleId(null);
      clearPrefill();
      toast.success("Rule added");
    },
    onError: (err) => {
      toast.error("Could not add rule", err.message);
    },
  });

  const save = useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: Partial<RulePayload> }) =>
      updateRule(id, patch),
    onSuccess: () => {
      invalidate();
      setEditingRuleId(null);
      setDraft({ ...EMPTY_DRAFT });
      toast.success("Rule saved");
    },
    onError: (err) => {
      toast.error("Rule save failed", err.message);
    },
  });

  const remove = useMutation({
    mutationFn: (ruleId: number) => deleteRule(ruleId),
    onSuccess: () => {
      invalidate();
      toast.success("Rule deleted");
    },
    onError: (err) => {
      toast.error("Rule delete failed", err.message);
    },
  });

  // ── Prefill from payee ────────────────────────────────────────────────

  const applyPayee = () => {
    if (payee.trim() === "") return;
    setDraft((d) => ({
      ...d,
      pattern: d.match_type === "regex" ? escapeRegex(payee.trim()) : payee.trim(),
    }));
  };

  // ── Edit handlers ─────────────────────────────────────────────────────

  const startEdit = (rule: Rule) => {
    setEditingRuleId(rule.id);
    setDraft({
      match_type: rule.match_type,
      pattern: rule.pattern,
      priority: rule.priority,
      category_id: rule.category_id,
      enabled: rule.enabled,
      tag_ids: rule.tags.map((t) => t.id),
      tag_action: rule.tag_action || "set",
      stop_on_match: rule.stop_on_match ?? true,
    });
    setPayee("");
  };

  const cancelEdit = () => {
    setEditingRuleId(null);
    setDraft({ ...EMPTY_DRAFT });
  };

  const submitForm = () => {
    const payload = draftToPayload(draft);
    if (editingRuleId !== null) {
      save.mutate({ id: editingRuleId, patch: payload });
    } else {
      create.mutate(payload);
    }
  };

  // ── Tag toggle helper ─────────────────────────────────────────────────

  const toggleTag = (tagId: number) => {
    setDraft((d) => ({
      ...d,
      tag_ids: d.tag_ids.includes(tagId)
        ? d.tag_ids.filter((id) => id !== tagId)
        : [...d.tag_ids, tagId],
    }));
  };

  // ── Loading / error states ────────────────────────────────────────────

  if (rules.isPending || categories.isPending || tags.isPending) {
    return (
      <Card className="p-3 sm:p-4">
        <CardTitle>Categorization rules</CardTitle>
        <SkeletonRows rows={6} />
      </Card>
    );
  }
  if (rules.isError) {
    return (
      <Card className="p-3 sm:p-4">
        <CardTitle>Categorization rules</CardTitle>
        <ErrorPanel message={rules.error.message} />
      </Card>
    );
  }
  if (categories.isError) {
    return (
      <Card className="p-3 sm:p-4">
        <CardTitle>Categorization rules</CardTitle>
        <ErrorPanel message={categories.error.message} />
      </Card>
    );
  }

  const sorted = [...rules.data.items].sort((a, b) => a.priority - b.priority || a.id - b.id);
  const dryRunRule = sorted.find((r) => r.id === dryRunRuleId);
  const parsedPriority = draft.priority;
  const priorityValid =
    !Number.isNaN(parsedPriority) && parsedPriority >= 0 && parsedPriority <= 9999;
  const patternValid =
    AMOUNT_MATCHERS[draft.match_type] ||
    draft.pattern.trim() !== "";
  const ready = patternValid && draft.category_id > 0 && priorityValid;

  return (
    <motion.div variants={staggerContainer(0.08)}>
      <Card className="p-3 sm:p-4">
        <CardTitle
          right={
            <span className="text-xs text-muted">
              {sorted.length} rule{sorted.length !== 1 ? "s" : ""}
            </span>
          }
        >
          <span className="flex items-center gap-2">
            <ScrollText className="size-4 text-accent" aria-hidden />
            Categorization rules
          </span>
        </CardTitle>
        <p className="-mt-2 mb-4 text-xs text-muted">
          Ordered by priority ascending; first match wins unless a rule lets processing continue.
        </p>

        {/* ── Create / Edit form ────────────────────────────────────── */}
        {/* Padding is rem-based, so it scales with the user's font: at
            Android's 200% scale (root 32px) card p-4 + form p-3 + main's
            padding left a 320px viewport with a 124px form column — narrower
            than the label's own longest word ("transaction", 142.7px) and
            narrower than the submit row (140.6px). One step down below `sm`
            gives the column 156px, which holds every child at 200% while
            `sm:` keeps >=640 exactly as it was (M82-F2 200% residual). */}
        <form
          className="mb-5 space-y-3 rounded-lg border border-veil/[0.06] bg-surface p-2 text-sm sm:p-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (ready) submitForm();
          }}
        >
          {/* M123 (docs/09 §Create from search): where this draft came from,
              and — honestly — which parts of the search a rule cannot express. */}
          {prefillProvenance !== null && (
            <p className="text-xs text-muted" data-testid="prefill-provenance">
              {prefillProvenance}
            </p>
          )}
          {/* Row 1: match type, pattern, priority, enabled.
              Below md a 12-col track is ~45px wide at a 412px viewport while
              the `Enabled` toggle alone needs ~98px, so the row used to push
              the toggle (and the Priority `?`) past the right edge where
              main#main-content's `overflow-x: clip` hides it for good. The
              fields therefore stack full-width on mobile and the 12-col
              desktop row is untouched from md up. */}
          <div className="grid grid-cols-1 items-end gap-2 md:grid-cols-12">
            <label className="md:col-span-3">
              <span className="mb-1 block text-xs text-muted">Match type</span>
              <select
                value={draft.match_type}
                onChange={(e) => {
                  const mt = e.target.value as RuleMatchType;
                  setDraft((d) => ({
                    ...d,
                    match_type: mt,
                    pattern: AMOUNT_MATCHERS[mt] && !AMOUNT_MATCHERS[d.match_type]
                      ? ""
                      : PAYEE_MATCHERS[mt] && AMOUNT_MATCHERS[d.match_type]
                        ? ""
                        : d.pattern,
                  }));
                }}
                className={FIELD}
              >
                {ALL_MATCH_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </label>
            <label className="md:col-span-5">
              <span className="mb-1 block text-xs text-muted">
                {patternLabel(draft.match_type)}
                {editingRuleId !== null && draft.pattern !== rules.data.items.find((r) => r.id === editingRuleId)?.pattern && (
                  <span className="ml-1 text-accent">*</span>
                )}
              </span>
              <input
                id="rule-pattern"
                value={draft.pattern}
                onChange={(e) => setDraft((d) => ({ ...d, pattern: e.target.value }))}
                placeholder={patternPlaceholder(draft.match_type)}
                className={FIELD}
              />
            </label>
            <label className="md:col-span-2">
              {/* The hint gives up half its default gap below `lg`: the
                  Priority cell is only 2 of the 12 tracks, and gap-2 is 16px
                  per gutter at root 32, so the track is 36.7px at 768/200% —
                  `ml-1` + the 32px hint stood 3.3px outside this span. 4px
                  still separates the "?" from the word, and `lg:ml-1` keeps
                  1024/1280 byte-identical. */}
              <span className="mb-1 block text-xs text-muted">Priority<HelpHint className="ml-0.5 lg:ml-1" text="Rules run in priority order, lowest number first. Lower numbers run before higher numbers." /></span>
              <input
                value={String(draft.priority)}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, priority: parseInt(e.target.value, 10) || 0 }))
                }
                inputMode="numeric"
                pattern="[0-9]*"
                className={FIELD}
              />
            </label>
            {/* `Enabled` takes a whole row from md up (M82-F2 residual): line 1
                is exactly 12 tracks (3 + 5 + 2 + 2), so a 2-col track is
                55.4px at 768/130% root font while the pill is 128px wide
                (40px switch + gap + the word "Enabled"). The pill needs 5
                tracks (154px) and only 2 are free; taking 3 more would leave
                Match type / Pattern / Priority 88px-wide inputs. So the
                toggle gets its own full-width row — the same shape the
                `Stop here on match` toggle below already uses. One line
                taller from md up; no other cell in the row moves. */}
            <div className="md:col-span-12 pb-0.5">
              <Toggle
                checked={draft.enabled}
                onChange={(checked) => setDraft((d) => ({ ...d, enabled: checked }))}
                ariaLabel="Rule enabled"
                label={<span className="text-xs text-muted">Enabled</span>}
              />
            </div>
            <div className="md:col-span-12 pb-0.5">
              <Toggle
                checked={draft.stop_on_match}
                onChange={(checked) => setDraft((d) => ({ ...d, stop_on_match: checked }))}
                ariaLabel="Stop at this rule on match"
                label={
                  <span className="text-xs text-muted" title="When on, later rules are skipped once this rule matches. When off, processing continues to lower-priority rules.">
                    Stop here on match
                    <HelpHint text="On: the first matching rule files the transaction and later rules are skipped. Off: processing continues to lower-priority rules; the last matching rule's category wins and tags add up." />
                  </span>
                }
              />
            </div>
          </div>

          {/* Row 2: category, tag action, tags */}
          <div className="grid grid-cols-1 items-start gap-2 md:grid-cols-12">
            <label className="md:col-span-3">
              <span className="mb-1 block text-xs text-muted">
                Category{draft.category_id > 0 ? "" : " *"}
              </span>
              <select
                value={draft.category_id || ""}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, category_id: Number(e.target.value) || 0 }))
                }
                className={FIELD}
              >
                <option value="">Select…</option>
                {[...categoryName.entries()].map(([id, name]) => (
                  <option key={id} value={id}>
                    {name}
                  </option>
                ))}
              </select>
            </label>

            <div className="md:col-span-9">
              <span className="mb-1 block text-xs text-muted">Tags</span>
              <div className="flex flex-wrap items-center gap-1.5">
                {(tags.data?.items ?? []).map((tag) => {
                  const selected = draft.tag_ids.includes(tag.id);
                  return (
                    <button
                      key={tag.id}
                      type="button"
                      onClick={() => toggleTag(tag.id)}
                      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors ${
                        selected
                          ? "border-accent/40 bg-accent/15 text-accent"
                          : "border-control bg-veil/[0.03] text-muted hover:border-accent/70 hover:text-text"
                      }`}
                    >
                      <Tag className="size-3" aria-hidden />
                      {tag.name}
                    </button>
                  );
                })}
                {(tags.data?.items ?? []).length === 0 && (
                  <span className="text-xs text-muted">No tags created yet</span>
                )}
                {!showRuleTagInput ? (
                  <button
                    key="__new_tag__"
                    type="button"
                    onClick={() => setShowRuleTagInput(true)}
                    className="inline-flex items-center gap-1 rounded-full border border-dashed border-control px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/70 hover:text-text"
                  >
                    + New…
                  </button>
                ) : (
                  <span key="__new_tag_input__" className="inline-flex items-center gap-1">
                    <input
                      autoFocus
                      value={ruleTagName}
                      onChange={(e) => setRuleTagName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          void createRuleTag();
                        } else if (e.key === "Escape") {
                          setShowRuleTagInput(false);
                          setRuleTagName("");
                        }
                      }}
                      placeholder="Tag name"
                      aria-label="New rule tag name"
                      className="field-ring w-28 rounded-full border border-control bg-surface px-2 py-0.5 text-[11px] outline-none"
                    />
                    <button
                      type="button"
                      disabled={ruleTagName.trim() === "" || createTagState.isPending}
                      onClick={() => void createRuleTag()}
                      className="rounded-full bg-accent/15 px-2 py-0.5 text-[11px] font-medium text-accent outline-none transition-colors hover:bg-accent/25 focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
                    >
                      Add
                    </button>
                  </span>
                )}
              </div>
              {createTagState.isError && (
                <p role="alert" className="mt-1 text-xs text-danger">
                  {apiErrorMessage(createTagState.error)}
                </p>
              )}
            </div>
          </div>

          {/* Row 3: tag action radio + payee prefill + submit */}
          <div className="flex flex-wrap items-end gap-4 border-t border-veil/[0.06] pt-3">
            {/* Tag action — only shown when tags are selected */}
            {draft.tag_ids.length > 0 && (
              <fieldset className="flex items-center gap-3">
                <span className="text-xs text-muted">Tag action</span>
                <label className="flex items-center gap-1.5 text-xs">
                  <input
                    type="radio"
                    name="tag_action"
                    value="set"
                    checked={draft.tag_action === "set"}
                    onChange={() => setDraft((d) => ({ ...d, tag_action: "set" }))}
                    className="accent-accent"
                  />
                  Set (replace)
                </label>
                <label className="flex items-center gap-1.5 text-xs">
                  <input
                    type="radio"
                    name="tag_action"
                    value="add"
                    checked={draft.tag_action === "add"}
                    onChange={() => setDraft((d) => ({ ...d, tag_action: "add" }))}
                    className="accent-accent"
                  />
                  Add (append)
                </label>
              </fieldset>
            )}

            {/* Prefill from payee.
                flex-wrap is the whole fix (M82-F2 residual): `flex-1` is
                `flex: 1 1 0%`, so the row's line breaker sees base size 0 and
                clamps this box at its min-content — label + gap + the
                nowrap "Use as pattern" button (~245px at 130% root font) —
                which is wider than the 191px row at 320, so the box itself
                spilled 53.9px past the row's right edge. With `flex-wrap`
                the min-content becomes the widest single child (the button,
                ~142px), which fits every width, and when label + button no
                longer share a line the button drops below the input instead
                of pushing the box out of the row. Desktop geometry is
                unchanged: at >=768 both children still fit on one line, so
                the label keeps growing to exactly the width it has today. */}
            <div className="flex flex-1 flex-wrap items-end gap-2">
              <label className="flex-1">
                <span className="mb-1 block text-xs text-muted">
                  Prefill from payee (normalized transaction payee)
                </span>
                <input
                  value={payee}
                  onChange={(e) => setPayee(e.target.value)}
                  placeholder="e.g. METRO INC"
                  className={FIELD}
                />
              </label>
            {/* `wrap` (M82-F2 200% residual): the label is nowrap, so the
                button's min-content is label + padding = 217.5px at root 32
                while its own line in the box is 156px at 320 — the button was
                the floor that pushed the whole prefill box 93.5px out of row
                3. Wrapping drops the floor to the longest word and lets the
                button grow to a second line; at >=640 there is room for the
                single line, so it renders exactly as before. */}
              <Button
                type="button"
                variant="outline"
                size="sm"
                wrap
                disabled={payee.trim() === ""}
                onClick={applyPayee}
              >
                Use as pattern{draft.match_type === "regex" ? " (escaped)" : ""}
              </Button>
            </div>

            {/* Submit / Cancel */}
            <div className="flex gap-2">
              {editingRuleId !== null && (
                <Button type="button" variant="ghost" size="sm" onClick={cancelEdit}>
                  Cancel
                </Button>
              )}
              <Button
                type="submit"
                variant="primary"
                size="sm"
                loading={create.isPending || save.isPending}
                disabled={!ready}
              >
                {editingRuleId !== null ? "Save rule" : "Add rule"}
              </Button>
            </div>
          </div>
        </form>

        {/* ── Rules table ──────────────────────────────────────────── */}
        {sorted.length === 0 ? (
          <EmptyState
            icon={<FolderInput />}
            title="No rules yet."
            hint="Rules auto-categorize incoming transactions before suggestions are generated."
            preview={{
              src: "/help/rules.png",
              alt: "The Rules page listing automation rules",
            }}
            action={{
              // The create form sits ABOVE this panel and is always mounted, so
              // the action lands the user on its pattern field (and never
              // duplicates the form's own "Add rule" submit label — docs/09 M92).
              label: "Create a rule",
              onClick: () => {
                const field = document.getElementById("rule-pattern");
                field?.scrollIntoView({ block: "center" });
                field?.focus();
              },
            }}
          />
        ) : (
          <div role="region" aria-label="Categorization rules table" tabIndex={0} className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-veil/[0.06] text-left text-xs text-muted">
                  <th className="pb-2 pr-3 font-medium">Priority</th>
                  <th className="pb-2 pr-3 font-medium">Match type</th>
                  <th className="pb-2 pr-3 font-medium">Pattern</th>
                  <th className="pb-2 pr-3 font-medium">Category</th>
                  <th className="pb-2 pr-3 font-medium">Tags</th>
                  <th className="pb-2 pr-3 text-right font-medium">Enabled</th>
                  <th className="pb-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <motion.tbody variants={staggerContainer(0.03)}>
                {sorted.map((rule) => (
                  <RuleRow
                    key={rule.id}
                    rule={rule}
                    categoryName={categoryName}
                    isEditing={editingRuleId === rule.id}
                    onStartEdit={() => startEdit(rule)}
                    onRemove={() => remove.mutate(rule.id)}
                    isRemoving={remove.isPending}
                    onPreview={() =>
                      setDryRunRuleId((id) => (id === rule.id ? null : rule.id))
                    }
                    previewOpen={dryRunRuleId === rule.id}
                  />
                ))}
              </motion.tbody>
            </table>
          </div>
        )}
        {dryRunRule !== undefined && (
          <RuleDryRun rule={dryRunRule} onClose={() => setDryRunRuleId(null)} />
        )}
      </Card>
    </motion.div>
  );
}

// ── Rule table row ─────────────────────────────────────────────────────────

function RuleRow({
  rule,
  categoryName,
  isEditing,
  onStartEdit,
  onRemove,
  isRemoving,
  onPreview,
  previewOpen,
}: {
  rule: Rule;
  categoryName: Map<number, string>;
  isEditing: boolean;
  onStartEdit: () => void;
  onRemove: () => void;
  isRemoving: boolean;
  onPreview: () => void;
  previewOpen: boolean;
}) {
  const isPayee = PAYEE_MATCHERS[rule.match_type];
  const isAmount = AMOUNT_MATCHERS[rule.match_type];

  return (
    <motion.tr
      variants={fadeUp}
      className={`border-b border-veil/[0.04] transition-colors hover:bg-veil/[0.02] ${
        isEditing ? "bg-accent/[0.04]" : ""
      }`}
    >
      <td className="py-2 pr-3 font-mono text-xs text-muted">{rule.priority}</td>
      <td className="py-2 pr-3">
        <Badge tone={isAmount ? "warn" : isPayee ? "accent" : "neutral"}>
          {rule.match_type.replace(/_/g, " ")}
        </Badge>
      </td>
      <td className="max-w-[200px] truncate py-2 pr-3 font-mono text-xs">
        {rule.pattern}
      </td>
      <td className="py-2 pr-3 text-xs">
        {categoryName.get(rule.category_id) ?? (
          <span className="text-danger">#{rule.category_id}</span>
        )}
      </td>
      <td className="py-2 pr-3">
        <div className="flex flex-wrap gap-1">
          {rule.tags.map((tag) => (
            <Badge key={tag.id} tone="success" dot={false}>
              {tag.name}
            </Badge>
          ))}
        </div>
      </td>
      <td className="py-2 pr-3 text-right">
        <Badge tone={rule.enabled ? "success" : "neutral"} dot={false}>
          {rule.enabled ? "On" : "Off"}
        </Badge>
        {!rule.stop_on_match && (
          <div className="mt-1" title="Processing continues to later rules after this match">
            <Badge tone="accent" dot={false}>
              continues
            </Badge>
          </div>
        )}
      </td>
      <td className="py-2 text-right">
        <div className="inline-flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={onPreview}
            aria-label={`Test run rule ${rule.pattern}`}
            title="Dry-run: see what this rule would match"
            className={previewOpen ? "text-accent" : undefined}
          >
            <ScrollText className="size-3.5" />
          </Button>
          <HelpHint text="Dry-run only shows what would match. It never changes your data." />
          <Button variant="ghost" size="sm" onClick={onStartEdit} aria-label={`Edit rule ${rule.pattern}`}>
            <Pencil className="size-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onRemove}
            loading={isRemoving}
            aria-label={`Delete rule ${rule.pattern}`}
            className="text-danger hover:bg-danger/10"
          >
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </td>
    </motion.tr>
  );
}
