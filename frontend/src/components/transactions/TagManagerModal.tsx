// TagManagerModal — the tag management surface (M104, docs/09 §Tag UX).
//
// Opened from the filter bar's tag picker ("Manage tags…"), because tags
// accumulate forever through inline-create: a typo'd tag ("Goceries" next to
// "Groceries") used to be unfixable anywhere in the app — `PATCH` would 409
// `duplicate_tag`, so folding the two together is what `POST /tags/merge` is
// for, and the list carries the usage counts that decide what can be deleted
// (409 `tag_in_use` is the backstop, not the affordance).
//
// Dialog contract: the SHARED helpers from `dialogA11y` — focus moves in on
// open, Escape closes, focus returns to the opener. Deliberately NOT a Tab
// trap: docs/09's keyboard contract says every focusable surface must keep
// advancing with Tab and that dialogs are explicitly not focus traps, so the
// PM spec's "focus-trap" wording resolves to the house contract (AGENTS §0:
// the numbered docs win).

import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { deleteTag, listTags, mergeTags, updateTag } from "../../api/tags";
import { hasErrorCode } from "../../api/client";
import type { TagWithUsage } from "../../api/tags";
import { apiErrorMessage } from "./format";
import { Button } from "../ui/Button";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { useDialogFocus, useEscapeToClose } from "../ui/dialogA11y";
import { useToast } from "../ui/Toast";

/** "12 transactions · 1 rule" / "unused" — the delete affordance is the count. */
function usageLabel(tag: TagWithUsage): string {
  const parts: string[] = [];
  if (tag.transaction_count > 0) {
    parts.push(`${tag.transaction_count} transaction${tag.transaction_count === 1 ? "" : "s"}`);
  }
  if (tag.rule_count > 0) {
    parts.push(`${tag.rule_count} rule${tag.rule_count === 1 ? "" : "s"}`);
  }
  return parts.length === 0 ? "unused" : parts.join(" · ");
}

const inUse = (tag: TagWithUsage): boolean =>
  tag.transaction_count > 0 || tag.rule_count > 0;

interface TagManagerModalProps {
  onClose: () => void;
}

export function TagManagerModal({ onClose }: TagManagerModalProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useDialogFocus(dialogRef);
  useEscapeToClose(onClose);

  const tags = useQuery({ queryKey: ["tags"], queryFn: () => listTags(200) });
  const items = tags.data?.items ?? [];

  /** Merge selection: every checked tag is folded into the survivor, which is
   *  one of them (chosen in the merge bar as soon as two are selected). */
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [survivorId, setSurvivorId] = useState<number | null>(null);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [draftName, setDraftName] = useState("");

  function invalidate(): void {
    // Names ride on every row that carries a tag, so the ledger caches move too.
    void queryClient.invalidateQueries({ queryKey: ["tags"] });
    void queryClient.invalidateQueries({ queryKey: ["transactions"] });
    void queryClient.invalidateQueries({ queryKey: ["rules"] });
    void queryClient.invalidateQueries({ queryKey: ["search"] });
  }

  function toggle(id: number): void {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    setSurvivorId((current) => {
      if (current !== null) return current;
      return id;
    });
  }

  const rename = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) => updateTag(id, { name }),
    onSuccess: (_data, variables) => {
      setRenamingId(null);
      invalidate();
      toast.success("Tag renamed", variables.name);
    },
    onError: (err) => {
      // Branch on the envelope `code`, never on `detail` (docs/08, hasErrorCode).
      const message = hasErrorCode(err, "duplicate_tag")
        ? "A tag with that name already exists."
        : apiErrorMessage(err);
      toast.error("Rename failed", message);
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteTag(id),
    onSuccess: () => {
      invalidate();
      toast.success("Tag deleted");
    },
    onError: (err) => {
      const message = hasErrorCode(err, "tag_in_use")
        ? "That tag is in use — remove it from its transactions or rules first."
        : apiErrorMessage(err);
      toast.error("Delete blocked", message);
    },
  });

  const sourceIds = [...selected].filter((id) => id !== survivorId);
  const survivor = items.find((tag) => tag.id === survivorId) ?? null;

  const merge = useMutation({
    mutationFn: () => mergeTags({ source_ids: sourceIds, target_id: survivorId as number }),
    onSuccess: (result) => {
      setSelected(new Set());
      setSurvivorId(null);
      invalidate();
      toast.success("Tags merged", `Kept “${result.target.name}”`);
    },
    onError: (err) => toast.error("Merge failed", apiErrorMessage(err)),
  });

  const canMerge = survivorId !== null && sourceIds.length > 0 && selected.size >= 2;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay/60 p-4"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tag-manager-title"
        tabIndex={-1}
        className="glass max-h-[calc(100vh-2rem)] w-full max-w-2xl overflow-y-auto rounded-2xl p-5 shadow-2xl outline-none"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="tag-manager-title" className="text-lg font-semibold">
              Manage tags
            </h2>
            <p className="mt-1 text-xs text-muted">
              Rename a typo, fold duplicates into one tag, delete what nothing uses
            </p>
          </div>
          <button
            type="button"
            aria-label="Close tag manager"
            onClick={onClose}
            className="text-muted transition-colors hover:text-text"
          >
            ×
          </button>
        </div>

        {tags.isPending && (
          <div className="mt-4">
            <SkeletonRows rows={5} />
          </div>
        )}
        {tags.isError && (
          <div className="mt-4">
            <QueryErrorPanel query={tags} />
          </div>
        )}

        {/* No EmptyState here on purpose: the three-part contract's preview
            exceptions are a pinned set (docs/09 §Empty states), and a dialog
            that lists an empty collection only needs to say so. */}
        {!tags.isPending && !tags.isError && items.length === 0 && (
          <p className="mt-4 text-sm text-muted">
            No tags yet — close this and add one while editing a transaction.
          </p>
        )}

        {!tags.isPending && !tags.isError && items.length > 0 && (
          <div
            role="region"
            aria-label="All tags with their usage"
            tabIndex={0}
            className="mt-4 max-h-[55vh] overflow-y-auto rounded-lg border border-control"
          >
            <ul className="divide-y divide-veil/[0.06]">
              {items.map((tag) => (
                <li key={tag.id} className="flex flex-wrap items-center gap-2 px-3 py-2">
                  <input
                    type="checkbox"
                    aria-label={`Select ${tag.name} to merge`}
                    checked={selected.has(tag.id)}
                    onChange={() => toggle(tag.id)}
                    className="size-4 accent-accent"
                  />
                  {renamingId === tag.id ? (
                    <>
                      <input
                        aria-label={`New name for ${tag.name}`}
                        value={draftName}
                        autoFocus
                        onChange={(event) => setDraftName(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" && draftName.trim() !== "") {
                            rename.mutate({ id: tag.id, name: draftName.trim() });
                          }
                          if (event.key === "Escape") {
                            event.stopPropagation();
                            setRenamingId(null);
                          }
                        }}
                        className="field-ring min-w-32 flex-1 rounded-lg border border-control bg-surface px-2 py-1 text-sm outline-none"
                      />
                      <Button
                        variant="primary"
                        size="sm"
                        loading={rename.isPending}
                        disabled={draftName.trim() === ""}
                        onClick={() => rename.mutate({ id: tag.id, name: draftName.trim() })}
                      >
                        Save name
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setRenamingId(null)}>
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <>
                      <span className="min-w-24 flex-1 truncate text-sm font-medium text-text">
                        {tag.name}
                      </span>
                      <span
                        className={`text-xs ${inUse(tag) ? "text-muted" : "text-accent"}`}
                      >
                        {usageLabel(tag)}
                      </span>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setRenamingId(tag.id);
                          setDraftName(tag.name);
                        }}
                      >
                        Rename {tag.name}
                      </Button>
                      {inUse(tag) ? null : (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-danger hover:text-danger"
                          onClick={() => remove.mutate(tag.id)}
                        >
                          Delete {tag.name}
                        </Button>
                      )}
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        {selected.size >= 2 && survivor !== null && (
          <div className="mt-3 flex flex-wrap items-end gap-2 rounded-lg border border-control bg-surface p-3">
            <label className="text-xs text-muted">
              Keep as
              <select
                value={survivorId ?? ""}
                onChange={(event) => setSurvivorId(Number(event.target.value))}
                className="field-ring mt-1 block rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none"
              >
                {[...selected].map((id) => {
                  const tag = items.find((item) => item.id === id);
                  return tag ? (
                    <option key={id} value={id}>
                      {tag.name}
                    </option>
                  ) : null;
                })}
              </select>
            </label>
            <p className="text-xs text-muted">
              {sourceIds.length} tag{sourceIds.length === 1 ? "" : "s"} fold into “
              {survivor.name}”; every transaction and rule keeps its label.
            </p>
            <div className="ml-auto">
              <Button
                variant="primary"
                size="sm"
                loading={merge.isPending}
                disabled={!canMerge}
                onClick={() => merge.mutate()}
              >
                Merge {sourceIds.length} into {survivor.name}
              </Button>
            </div>
          </div>
        )}

        <div className="mt-4 flex justify-end">
          <Button variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
