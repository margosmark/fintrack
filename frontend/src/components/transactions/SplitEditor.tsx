// SplitEditor — the modal docs/44 §12 pins: split ONE statement line into
// 2…20 parts, each with its own amount, category, tags and note.
//
//   * The `Remaining:` line is EXACT Decimal-string arithmetic (the report
//     engine's BigInt fixed-point helpers — no float ever touches money,
//     AGENTS §2) and submit stays disabled until it reads `Remaining: 0.00`.
//   * The first child is prefilled with the PARENT's category and the rest are
//     empty — a client-side convenience only: the server copies identity
//     fields only (docs/44 D4) and never a category or a tag (D5).
//   * A server envelope renders its `detail` VERBATIM plus its `code` — never
//     a generic "something went wrong" (docs/44 §4.3).
//   * Escape / backdrop / focus behave exactly like TransactionEditor.

import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Category } from "../../api/categories";
import type { SplitChildInput, Transaction } from "../../api/transactions";
import type { Tag } from "../../api/tags";
import { createTag, listTags } from "../../api/tags";
import { ApiError } from "../../api/client";
import { encryptTransaction } from "../../lib/e2ee";
import { isUnlocked } from "../../lib/vault";
import { VaultLocked } from "../e2ee/VaultLocked";
import { useEncryptionRequired, useVaultUnlocked } from "../e2ee/useUnlock";
import { dadd, dcents, dec, dround, dstr, dsub, type Dec } from "../../lib/dec";
import { noteTagUsed } from "../../lib/tagRecency";
import { useDialogFocus, useEscapeToClose } from "../ui/dialogA11y";
import { Button } from "../ui/Button";
import { DemoLock, DemoReason, demoLocks } from "../ui/DemoLock";
import { useOptionalUser } from "../../auth/authUser";
import { isDemoRefusalError } from "../../lib/demoWrite";
import { CategoryPicker } from "./CategoryPicker";
import { TagPicker } from "./TagPicker";

const INPUT_CLASS =
  "field-ring mt-1 block w-full rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";

/** A leg amount the server would accept: a Decimal, nothing else. */
const AMOUNT_RE = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;

/** Raw cents of a typed leg — `null` when the text is not a Decimal yet. */
function legCents(raw: string): Dec | null {
  const text = raw.trim();
  if (!AMOUNT_RE.test(text)) return null;
  // Cents, half-up — the column is Numeric(18,2), so this is what the server
  // stores: display and payload then agree to the digit.
  return dround(dec(text), 2);
}

/**
 * `parent − Σ children`, in cents, as the wire shows it (`-10.00`, `0.00`).
 *
 * Exported because the arithmetic IS the contract: an empty/typed-over leg
 * contributes nothing, and every step happens in exact fixed-point — the float
 * answer for `0.30 − (0.10 + 0.20)` is `-0.00`, which would strand a correct
 * split behind a disabled button.
 */
export function remainingLabel(parentAmount: string, amounts: readonly string[]): string {
  let sum = dec("0");
  for (const raw of amounts) {
    const cents = legCents(raw);
    if (cents !== null) sum = dadd(sum, cents);
  }
  return dcents(dsub(dec(parentAmount), sum));
}

/** Every leg the server accepts: non-zero, sign matching the parent. */
export function legsAreValid(parentAmount: string, amounts: readonly string[]): boolean {
  const parentNegative = dec(parentAmount).v < 0n;
  return amounts.every((raw) => {
    const cents = legCents(raw);
    if (cents === null || cents.v === 0n) return false;
    return (cents.v < 0n) === parentNegative;
  });
}

interface Draft {
  key: string;
  amount: string;
  categoryId: string;
  note: string;
  tagIds: number[];
}

let draftSeq = 0;

function emptyDraft(categoryId: number | string | null): Draft {
  draftSeq += 1;
  return {
    key: `leg-${draftSeq}`,
    amount: "",
    categoryId: categoryId === null ? "" : String(categoryId),
    note: "",
    tagIds: [],
  };
}

function draftsFrom(children: readonly Transaction[]): Draft[] {
  return children.map((child) => {
    draftSeq += 1;
    return {
      key: `leg-${draftSeq}`,
      amount: child.amount_native,
      categoryId: child.category_id === null ? "" : String(child.category_id),
      note: child.note ?? "",
      tagIds: (child.tags ?? []).map((tag) => tag.id),
    };
  });
}

export interface SplitEditorProps {
  parent: Transaction;
  /** Re-split: the current children, prefilled (docs/44 §12). */
  initialChildren?: readonly Transaction[];
  categories: Category[];
  saving: boolean;
  error?: unknown;
  onSave: (children: SplitChildInput[]) => void;
  onClose: () => void;
  onCategoryCreated?: () => void;
  /**
   * docs/47 §4 — the page's ONE refusal, rendered where the server envelope
   * used to be. The host owns the `useDemoRefusal` instance (its mutation owns
   * the 403), so a refused split is one `role="status"` element + one
   * `toast.info` and NEVER the `role="alert"` line — a demo refusal is not a
   * bad sum (docs/44 §4.3 still renders every OTHER envelope verbatim).
   */
  demoRefusal?: ReactNode;
  /**
   * The HOST's demo-refusal guard (docs/47 §4) for the writes this dialog
   * dispatches itself (category create, tag create) — true means "the host
   * owns that 403", so no inline `role="alert"` is raised for it.
   */
  onWriteError?: (action: string, error: unknown) => boolean;
}

/** `aria-describedby` target of Save's padlock (docs/47 §4). */
export const SPLIT_SAVE_REASON_ID = "split-editor-save-demo-reason";

function envelopeOf(error: unknown): { detail: string; code: string | null } | null {
  if (error === undefined || error === null) return null;
  if (error instanceof ApiError) return { detail: error.message, code: error.code };
  if (error instanceof Error) return { detail: error.message, code: null };
  return { detail: String(error), code: null };
}

export function SplitEditor({
  parent,
  initialChildren,
  categories,
  saving,
  error,
  onSave,
  onClose,
  onCategoryCreated,
  demoRefusal,
  onWriteError,
}: SplitEditorProps) {
  const isResplit = initialChildren !== undefined && initialChildren.length > 0;
  const [drafts, setDrafts] = useState<Draft[]>(() =>
    isResplit
      ? draftsFrom(initialChildren)
      : // First leg prefilled with the parent's category, the rest empty —
        // convenience only; the server never copies a category (docs/44 D5).
        [emptyDraft(parent.category_id), emptyDraft(null)],
  );
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [creatingTag, setCreatingTag] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void listTags(200).then((res) => {
      if (!cancelled) setAllTags(res.items);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const dialogRef = useRef<HTMLFormElement | null>(null);
  useDialogFocus(dialogRef);
  useEscapeToClose(() => {
    if (!saving) onClose();
  });

  const amounts = drafts.map((draft) => draft.amount);
  const remaining = remainingLabel(parent.amount_native, amounts);
  const legsOk =
    legsAreValid(parent.amount_native, amounts) && drafts.length >= 2 && drafts.length <= 20;
  // G1 (M134 §5.1 / M136 P0-3) — "locked vault destroys work".
  //
  // `submit()` dual-writes `amount_cipher` ONLY `if (isUnlocked())`, and
  // `POST /transactions` refuses every split part without one under
  // `encryption_required` (`backend/app/api/transactions.py:1877`). So a
  // locked vault + an encryption-enrolled tenant was a GUARANTEED 422 on the
  // one button the user had already typed four legs into — the typed work was
  // gone and the raw envelope was the only feedback.
  //
  // Gate BEFORE submit: the Save control is REPLACED by the reason, the
  // inline unlock restores it in place (docs/33 rule 7c), and the 422 stays
  // exactly where it is as defense-in-depth.
  const vaultUnlocked = useVaultUnlocked();
  const cipherWriteBlocked = useEncryptionRequired() && !vaultUnlocked;
  const canSubmit = remaining === "0.00" && legsOk && !saving && !cipherWriteBlocked;

  function setLeg(key: string, patch: Partial<Draft>): void {
    setDrafts((current) =>
      current.map((draft) => (draft.key === key ? { ...draft, ...patch } : draft)),
    );
  }

  function addLeg(): void {
    setDrafts((current) => (current.length >= 20 ? current : [...current, emptyDraft(null)]));
  }

  function removeLeg(key: string): void {
    setDrafts((current) => (current.length <= 2 ? current : current.filter((d) => d.key !== key)));
  }

  async function attachTag(key: string, tagId: number): Promise<void> {
    setDrafts((current) =>
      current.map((draft) =>
        draft.key === key && !draft.tagIds.includes(tagId)
          ? { ...draft, tagIds: [...draft.tagIds, tagId] }
          : draft,
      ),
    );
    noteTagUsed(tagId);
  }

  function detachTag(key: string, tagId: number): void {
    setDrafts((current) =>
      current.map((draft) =>
        draft.key === key ? { ...draft, tagIds: draft.tagIds.filter((id) => id !== tagId) } : draft,
      ),
    );
  }

  async function createAndAttach(key: string, name: string): Promise<void> {
    const trimmed = name.trim();
    if (trimmed === "") return;
    setCreatingTag(true);
    try {
      const tag = await createTag({ name: trimmed });
      setAllTags((current) => [...current, tag].sort((a, b) => a.name.localeCompare(b.name)));
      await attachTag(key, tag.id);
    } catch (error) {
      // docs/47 §4: a demo refusal is the host's ONE refusal + one info toast;
      // every other failure keeps today's behaviour (it propagates to the
      // caller's handler rather than becoming a silent swallow here).
      if (onWriteError?.("Create tag", error)) return;
      throw error;
    } finally {
      setCreatingTag(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!canSubmit) return;
    // Belt-and-braces: the control is already gone in this state, so this is
    // only reachable if a future change re-enables the button by mistake.
    if (cipherWriteBlocked) return;
    const payload: SplitChildInput[] = [];
    for (const draft of drafts) {
      const amount = dstr(dround(dec(draft.amount.trim()), 2));
      const base: SplitChildInput = {
        amount_native: amount,
        category_id: draft.categoryId === "" ? null : Number(draft.categoryId),
        note: draft.note.trim() === "" ? null : draft.note.trim().slice(0, 500),
        tag_ids: draft.tagIds,
      };
      // Dual-write the ciphertext when the vault is unlocked — the same rule
      // the row editor follows (docs/33 F-10): under `encryption_required`
      // every child MUST carry its own `amount_cipher`.
      if (isUnlocked()) {
        const bundle = await encryptTransaction({
          normalizedPayee: parent.normalized_payee,
          rawDescription: parent.raw_description,
          amountNative: base.amount_native,
          cadAmount: null,
        });
        base.amount_cipher = bundle.amount_cipher;
      }
      payload.push(base);
    }
    onSave(payload);
  }

  const envelope = envelopeOf(error);
  const disabled = saving;
  // docs/47 §4: Save's ACTION is the blocked write (`POST|PUT
  // /transactions/{id}/split`, outside the deps.py exemption set), so on the
  // demo tenant it wears the padlock, STAYS enabled, and the real 403 becomes
  // the page's one refusal. Session source is the M136 optional scope — the
  // same one `useUnlock` above already reads — so the dialog stays renderable
  // in isolation (no provider ⇒ no session ⇒ no lock).
  const user = useOptionalUser();
  const locked = demoLocks(user, "POST", `/transactions/${parent.id}/split`);
  // A refused demo write is not a malformed sum: it never reaches the
  // envelope line (docs/47 §4).
  const refusedByDemo = isDemoRefusalError(error);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay/60 p-4"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target && !saving) onClose();
      }}
    >
      <form
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="split-editor-title"
        tabIndex={-1}
        onSubmit={submit}
        className="glass max-h-[calc(100vh-2rem)] w-full max-w-2xl overflow-y-auto rounded-2xl p-5 shadow-2xl"
      >
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 id="split-editor-title" className="text-lg font-semibold">
              Split this transaction into parts
            </h2>
            <p className="mt-1 text-xs text-muted">
              {parent.normalized_payee} · total {parent.amount_native} {parent.currency} · the
              parts must add up to it exactly
            </p>
          </div>
          <button
            type="button"
            aria-label="Close split editor"
            disabled={disabled}
            onClick={onClose}
            className="text-muted transition-colors hover:text-text"
          >
            ×
          </button>
        </div>

        <div className="mt-4 space-y-3">
          {drafts.map((draft, index) => {
            const attached = allTags.filter((tag) => draft.tagIds.includes(tag.id));
            return (
              <div key={draft.key} className="rounded-xl border border-control p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-medium uppercase tracking-widest text-muted">
                    Part {index + 1}
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove part ${index + 1}`}
                    disabled={disabled || drafts.length <= 2}
                    onClick={() => removeLeg(draft.key)}
                    className="inline-flex min-h-11 items-center rounded-lg px-2 text-xs text-danger outline-none hover:bg-danger/10 focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-40"
                  >
                    Remove
                  </button>
                </div>
                <div className="mt-1 grid gap-3 sm:grid-cols-2">
                  <label className="text-xs text-muted">
                    Amount *
                    <input
                      type="number"
                      step="0.01"
                      required
                      disabled={disabled}
                      value={draft.amount}
                      aria-label={`Amount for part ${index + 1}`}
                      onChange={(event) => setLeg(draft.key, { amount: event.target.value })}
                      placeholder={index === 0 ? "-120.00" : "-40.00"}
                      className={INPUT_CLASS}
                    />
                  </label>
                  <label className="text-xs text-muted">
                    Category
                    <span className="sr-only"> for part {index + 1}</span>
                    <CategoryPicker
                      onError={(error) => onWriteError?.("Create category", error) ?? false}
                      value={draft.categoryId}
                      categories={categories}
                      disabled={disabled}
                      inputClassName={INPUT_CLASS}
                      onChange={(id) => setLeg(draft.key, { categoryId: id })}
                      onCategoryCreated={onCategoryCreated}
                    />
                  </label>
                  <label className="text-xs text-muted">
                    Note
                    <input
                      type="text"
                      maxLength={500}
                      disabled={disabled}
                      value={draft.note}
                      aria-label={`Note for part ${index + 1}`}
                      onChange={(event) => setLeg(draft.key, { note: event.target.value })}
                      placeholder="kids clothes"
                      className={INPUT_CLASS}
                    />
                  </label>
                  <div className="text-xs text-muted">
                    Tags
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      {attached.map((tag) => (
                        <span
                          key={tag.id}
                          className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent"
                        >
                          {tag.name}
                          <button
                            type="button"
                            disabled={disabled}
                            aria-label={`Remove tag ${tag.name} from part ${index + 1}`}
                            onClick={() => detachTag(draft.key, tag.id)}
                            className="ml-0.5 text-accent hover:text-danger"
                          >
                            ×
                          </button>
                        </span>
                      ))}
                    </div>
                    <div className="mt-1.5">
                      <TagPicker
                        attached={attached}
                        allTags={allTags}
                        disabled={disabled}
                        creating={creatingTag}
                        label={`Tags for part ${index + 1}`}
                        inputClassName="w-full"
                        listClassName="w-full"
                        onAttach={(tagId) => void attachTag(draft.key, tagId)}
                        onCreate={(name) => void createAndAttach(draft.key, name)}
                      />
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={disabled || drafts.length >= 20}
            onClick={addLeg}
            className="inline-flex min-h-11 items-center rounded-lg border border-control bg-veil/[0.04] px-3 text-xs text-muted outline-none transition-colors hover:border-accent/70 hover:text-accent focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
          >
            Add a part
          </button>
          <span className="text-xs text-muted tabular">
            {drafts.length} of 20 parts · 2 minimum
          </span>
        </div>

        {/* The gate: exact Decimal arithmetic, and the line SAYS the residual. */}
        <p
          role="status"
          className={`mt-4 text-sm tabular ${remaining === "0.00" ? "text-gain" : "text-danger"}`}
        >
          Remaining: {remaining}
        </p>

        {demoRefusal}
        {!refusedByDemo && envelope !== null && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {envelope.detail}
            {envelope.code !== null && (
              <span className="ml-1 text-xs">({envelope.code})</span>
            )}
          </p>
        )}

        <div className="mt-5 flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={onClose}>
            Cancel
          </Button>
          {/* G1: the pre-emptive gate — the Save control itself is replaced
              by the reason it is gone, so no typed leg is ever thrown away by
              a 422 the user was never warned about. */}
          {cipherWriteBlocked ? (
            <VaultLocked variant="blocked" capability="write:split" />
          ) : (
            // docs/47 §4: padlock + VISIBLE reason (F-4) on the control whose
            // action IS the blocked write — it stays enabled, like every lock.
            <span className="inline-flex flex-col items-end gap-1">
              <Button
                type="submit"
                variant="primary"
                size="sm"
                loading={saving}
                disabled={!canSubmit}
                aria-describedby={locked ? SPLIT_SAVE_REASON_ID : undefined}
              >
                {locked && <DemoLock />}
                {isResplit ? "Save split" : "Split"}
              </Button>
              {locked && (
                <DemoReason id={SPLIT_SAVE_REASON_ID}>
                  The demo account is read-only — this split will not be saved.
                </DemoReason>
              )}
            </span>
          )}
        </div>
      </form>
    </div>,
    document.body,
  );
}
