import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Account } from "../../api/accounts";
import type { Category } from "../../api/categories";
import type {
  Transaction,
  TransactionCreate,
  TransactionPatch,
} from "../../api/transactions";
import { getTransactionTags, setTransactionTags, removeTransactionTag } from "../../api/transactions";
import type { Tag } from "../../api/tags";
import { listTags, createTag } from "../../api/tags";
import { noteTagUsed } from "../../lib/tagRecency";
import { apiErrorMessage } from "./format";
import { Button } from "../ui/Button";
import { useToast } from "../ui/Toast";
import { DemoLock, DemoReason, demoLocks } from "../ui/DemoLock";
import { useOptionalUser } from "../../auth/authUser";
import { isDemoRefusalError } from "../../lib/demoWrite";
import { Paperclip } from "lucide-react";
import { deleteReceipt, fetchReceiptBlob, uploadReceipt } from "../../api/receipts";
import { encodeReceiptImage, ReceiptImageError } from "../../lib/receiptImage";
import { etToday } from "../../lib/budgets";
import { useDialogFocus, useEscapeToClose } from "../ui/dialogA11y";
import { CategoryPicker } from "./CategoryPicker";
import { TagPicker } from "./TagPicker";

const INPUT_CLASS =
  "field-ring mt-1 block w-full rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";

// Renders its children straight into `document.body` — no deferred mount. The
// old `mounted` flag attached the portal one commit after `TransactionEditor`
// mounted, which left `dialogRef.current` null when `useDialogFocus` ran its
// focus effect (deps `[ref, open]` never fire again), so the editor opened
// with focus still on the page behind it.
function ModalPortal({ children }: { children: ReactNode }) {
  return createPortal(children, document.body);
}

type Draft = {
  accountId: string;
  date: string;
  postingDate: string;
  rawDescription: string;
  normalizedPayee: string;
  amountNative: string;
  currency: string;
  categoryId: string;
};

interface TransactionEditorProps {
  target: Transaction | null;
  defaultAccountId?: number;
  accounts: Account[];
  categories: Category[];
  saving: boolean;
  error?: unknown;
  onSave: (payload: TransactionCreate | TransactionPatch) => void;
  onClose: () => void;
  onCategoryCreated?: (category: Category) => void;
  /** M106 (docs/09 §History drawer): hand the row to the activity-log drawer.
   *  Only offered for an EXISTING row — a row that has never been saved has
   *  no receipts to read yet. */
  onHistory?: (transaction: Transaction) => void;
  /** M108 P4 (docs/09 §Receipt images): the LIST is the only source of the
   *  row's `has_receipt` chip, so attaching or removing in here must tell the
   *  page to re-read it — otherwise the editor says "One image attached" is
   *  gone while the row behind it still shows the chip (the exact contradiction
   *  docs/09 pins: both must vanish together). */
  onReceiptChanged?: () => void;
  /**
   * docs/47 §4 — the page's ONE refusal for this dialog, rendered exactly
   * where the error line used to be. The HOST owns the `useDemoRefusal`
   * instance (its mutation owns the 403), so a refused save shows the
   * local `role="status"` explanation + one `toast.info` and NEVER the
   * `role="alert"` line the envelope used to reach.
   */
  demoRefusal?: ReactNode;
  /**
   * The HOST's demo-refusal guard (docs/47 §4) for the writes this dialog
   * dispatches itself — tags, receipts, category create. Returns true when
   * the failure is the host's `403 demo_account_restricted` to render (its
   * ONE `demo-refusal` + one `toast.info`); the inline `role="alert"` line
   * and the error toasts then stay silent for that code and nothing else.
   */
  onWriteError?: (action: string, error: unknown) => boolean;
}

/** `aria-describedby` target of the Save control's padlock (docs/47 §4). */
export const TRANSACTION_SAVE_REASON_ID = "transaction-editor-save-demo-reason";


function draftFrom(
  target: Transaction | null,
  defaultAccountId: number | undefined,
  accounts: Account[],
): Draft {
  const account = target
    ? accounts.find((item) => item.id === target.account_id)
    : accounts.find((item) => item.id === defaultAccountId) ?? accounts[0];
  return {
    accountId: String(target?.account_id ?? account?.id ?? ""),
    // The default booking date is the user's ET today (`today_et()`'s twin),
    // not the UTC date: `toISOString()` reads TOMORROW for anyone east of UTC
    // in the evening, which would silently book the transaction a day ahead.
    date: target?.date ?? etToday(),
    postingDate: target?.posting_date ?? "",
    rawDescription: target?.raw_description ?? "",
    normalizedPayee: target?.normalized_payee ?? "",
    amountNative: target?.amount_native ?? "",
    currency: target?.currency ?? account?.currency ?? "CAD",
    categoryId: target?.category_id == null ? "" : String(target.category_id),
  };
}

export function TransactionEditor({
  target,
  defaultAccountId,
  accounts,
  categories,
  saving,
  error,
  onSave,
  onClose,
  onCategoryCreated,
  onHistory,
  onReceiptChanged,
  demoRefusal,
  onWriteError,
}: TransactionEditorProps) {
  const [draft, setDraft] = useState(() => draftFrom(target, defaultAccountId, accounts));
  const [validationError, setValidationError] = useState<string | null>(null);
  // Start from what the row already carries (`TransactionPublic.tags`), so the
  // initial fetch below CONFIRMS instead of changing the count — the picker's
  // reset-on-attach effect keys off that count, and a change arriving while
  // the user is typing would close the list under their cursor (M104).
  const [tags, setTags] = useState<Tag[]>(() => target?.tags ?? []);
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [creatingTag, setCreatingTag] = useState(false);
  const [tagError, setTagError] = useState<string | null>(null);
  const toast = useToast();
  // --- M108 P4 (docs/09 §Receipt images) ------------------------------------
  // One image per transaction; the editor is the ONLY place to replace or
  // remove it (the row's paperclip only attaches). Local state instead of a
  // refetch so the field flips the instant the PUT resolves.
  const [hasReceipt, setHasReceipt] = useState<boolean>(target?.has_receipt ?? false);
  const [receiptBusy, setReceiptBusy] = useState(false);
  const receiptFileRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setHasReceipt(target?.has_receipt ?? false);
  }, [target?.id, target?.has_receipt]);
  

  useEffect(() => {
    setDraft(draftFrom(target, defaultAccountId, accounts));
    setValidationError(null);
  }, [target, defaultAccountId, accounts]);
  // Refresh the tags of the transaction being edited (the row's copy above is
  // the first paint; this is the authoritative set).
  useEffect(() => {
    if (target) {
      getTransactionTags(target.id).then((res) => setTags(res.items));
    } else {
      setTags([]);
    }
  }, [target]);

  // Load all available tags for the picker.
  useEffect(() => {
    listTags(200).then((res) => setAllTags(res.items));
  }, []);

  // --- M108 P4: the three things a person can do to the image ---------------
  async function attachReceipt(file: File): Promise<void> {
    if (target === null) return;
    setReceiptBusy(true);
    try {
      // Re-encoded first (docs/09 §Receipt images): a phone photo is too big
      // for the server's 2 MB cap as shot, so the browser shrinks it before
      // the upload rather than letting the server turn it down after.
      const encoded = await encodeReceiptImage(file);
      await uploadReceipt(target.id, encoded.blob, encoded.filename);
      setHasReceipt(true);
      onReceiptChanged?.();
      toast.success("Image attached");
    } catch (error) {
      if (guardWrite("Attach image", error)) return;
      toast.error(
        "Could not attach that image",
        error instanceof ReceiptImageError ? error.message : apiErrorMessage(error),
      );
    } finally {
      setReceiptBusy(false);
    }
  }

  async function viewReceipt(): Promise<void> {
    if (target === null) return;
    setReceiptBusy(true);
    try {
      const blob = await fetchReceiptBlob(target.id);
      const url = URL.createObjectURL(blob);
      // Anchor click, not window.open: the fetch resolves after the gesture.
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) {
      if (guardWrite("Open image", error)) return;
      toast.error("Could not open that image", apiErrorMessage(error));
    } finally {
      setReceiptBusy(false);
    }
  }

  async function removeReceipt(): Promise<void> {
    if (target === null) return;
    setReceiptBusy(true);
    try {
      await deleteReceipt(target.id);
      setHasReceipt(false);
      onReceiptChanged?.();
      toast.success("Image removed");
    } catch (error) {
      if (guardWrite("Remove image", error)) return;
      toast.error("Could not remove that image", apiErrorMessage(error));
    } finally {
      setReceiptBusy(false);
    }
  }

  /**
   * Hand a write failure to the host first: a demo refusal becomes the page's
   * ONE refusal + one `toast.info`, and this dialog then raises nothing for
   * that code (docs/47 §4). Returns true when the host took it.
   */
  function guardWrite(action: string, error: unknown): boolean {
    return onWriteError?.(action, error) ?? false;
  }

  const dialogRef = useRef<HTMLFormElement | null>(null);
  useDialogFocus(dialogRef);
  useEscapeToClose(() => {
    if (!saving) onClose();
  });

  function set(field: keyof Draft, value: string): void {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  async function attachTag(tagId: number): Promise<void> {
    if (!target) return;
    setTagError(null);
    try {
      const res = await setTransactionTags(target.id, [...tags.map((t) => t.id), tagId]);
      setTags(res.items);
      noteTagUsed(tagId);
    } catch (e) {
      if (guardWrite("Add tag", e)) return;
      setTagError(apiErrorMessage(e));
    }
  }

  async function detachTag(tagId: number): Promise<void> {
    if (!target) return;
    setTagError(null);
    try {
      await removeTransactionTag(target.id, tagId);
      const res = await getTransactionTags(target.id);
      setTags(res.items);
    } catch (e) {
      if (guardWrite("Remove tag", e)) return;
      setTagError(apiErrorMessage(e));
    }
  }

  async function createAndAttachTag(name: string): Promise<void> {
    const trimmed = name.trim();
    if (!target || trimmed === "") return;
    setCreatingTag(true);
    setTagError(null);
    try {
      const newTag = await createTag({ name: trimmed });
      setAllTags((prev) => [...prev, newTag].sort((a, b) => a.name.localeCompare(b.name)));
      const res = await setTransactionTags(target.id, [...tags.map((t) => t.id), newTag.id]);
      setTags(res.items);
      noteTagUsed(newTag.id);
    } catch (e) {
      if (guardWrite("Create tag", e)) return;
      setTagError(apiErrorMessage(e));
    } finally {
      setCreatingTag(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!draft.accountId || !draft.date || !draft.rawDescription.trim() || !draft.amountNative) {
      setValidationError("Account, date, description, and amount are required.");
      return;
    }
    setValidationError(null);
    const payload = {
      account_id: Number(draft.accountId),
      date: draft.date,
      posting_date: draft.postingDate || null,
      raw_description: draft.rawDescription,
      normalized_payee: draft.normalizedPayee || null,
      amount_native: draft.amountNative,
      currency: draft.currency || null,
      category_id: draft.categoryId ? Number(draft.categoryId) : null,
    };
    onSave(payload);
  }

  const disabled = saving;
  // docs/47 §4: Save's ACTION is the blocked write — `POST /transactions` or
  // `PUT /transactions/{id}`, neither in `deps.py`'s exemption set — so on a
  // demo session the control wears the padlock, STAYS enabled (an inert
  // control cannot explain itself), and the real 403 becomes the host's one
  // refusal + one `toast.info`.
  //
  // Session source: the M136 optional user scope, not `useAuth`. This dialog
  // is a portal leaf that must render in isolation; outside an `AuthProvider`
  // the scope is null and the verdict degrades to "no session, no lock" —
  // the honest degradation `auth/authUser.ts` documents — instead of throwing.
  const user = useOptionalUser();
  const locked = demoLocks(
    user,
    target === null ? "POST" : "PUT",
    target === null ? "/transactions" : `/transactions/${target.id}`,
  );
  // A demo refusal is not a validation failure: it never reaches the
  // `role="alert"` line (docs/47 §4 / docs/09 §Read-only sessions #3).
  const refusedByDemo = isDemoRefusalError(error);
  return (
    <ModalPortal>
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
        aria-labelledby="transaction-editor-title"
        tabIndex={-1}
        onSubmit={submit}
        className="glass max-h-[calc(100vh-2rem)] w-full max-w-2xl overflow-y-auto rounded-2xl p-5 shadow-2xl"
      >
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 id="transaction-editor-title" className="text-lg font-semibold">
              {target ? "Edit transaction" : "New transaction"}
            </h2>
            <p className="mt-1 text-xs text-muted">Ledger fields and cached CAD conversion</p>
          </div>
          <button
            type="button"
            aria-label="Close transaction editor"
            disabled={disabled}
            onClick={onClose}
            className="text-muted transition-colors hover:text-text"
          >
            ×
          </button>
        </div>

        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-muted sm:col-span-2">
            Account *
            <select
              required
              disabled={disabled}
              value={draft.accountId}
              onChange={(event) => set("accountId", event.target.value)}
              className={INPUT_CLASS}
            >
              <option value="">Select account</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name} ({account.currency})
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-muted">
            Transaction date *
            <input
              required
              type="date"
              disabled={disabled}
              value={draft.date}
              onChange={(event) => set("date", event.target.value)}
              className={INPUT_CLASS}
            />
          </label>
          <label className="text-xs text-muted">
            Posting date
            <input
              type="date"
              disabled={disabled}
              value={draft.postingDate}
              onChange={(event) => set("postingDate", event.target.value)}
              className={INPUT_CLASS}
            />
          </label>
          <label className="text-xs text-muted">
            Payee
            <input
              disabled={disabled}
              value={draft.normalizedPayee}
              onChange={(event) => set("normalizedPayee", event.target.value)}
              placeholder="Defaults from description"
              className={INPUT_CLASS}
            />
          </label>
          <label className="text-xs text-muted">
            Amount *
            <input
              required
              type="number"
              step="0.01"
              disabled={disabled}
              value={draft.amountNative}
              onChange={(event) => set("amountNative", event.target.value)}
              placeholder="-25.00"
              className={INPUT_CLASS}
            />
          </label>
          <label className="text-xs text-muted">
            Currency
            <input
              disabled={disabled}
              value={draft.currency}
              onChange={(event) => set("currency", event.target.value.toUpperCase())}
              maxLength={3}
              pattern="[A-Z]{3}"
              className={INPUT_CLASS}
            />
          </label>
          <label className="text-xs text-muted">
            Category
            <CategoryPicker
              value={draft.categoryId}
              categories={categories}
              disabled={disabled}
              inputClassName={INPUT_CLASS}
              onError={(error) => guardWrite("Create category", error)}
              onChange={(id) => set("categoryId", id)}
              onCategoryCreated={onCategoryCreated}
            />
          </label>
          {/* M104 (docs/09 §Tag UX): tags promote ABOVE the raw description —
              they are the primary organizational tool for import-heavy users,
              so the section is labelled, full-width, and no longer the last
              thing before the fold. Category stays the single-value control
              it always was, which is what the label says out loud. */}
          {target && (
            <div className="sm:col-span-2">
              <span className="text-xs text-muted">
                Tags — as many as you like; category stays single
              </span>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {tags.map((tag) => (
                  <span
                    key={tag.id}
                    className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent"
                  >
                    {tag.name}
                    <button
                      type="button"
                      disabled={disabled}
                      onClick={() => void detachTag(tag.id)}
                      className="ml-0.5 text-accent hover:text-danger"
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
              <div className="mt-1.5">
                <TagPicker
                  attached={tags}
                  allTags={allTags}
                  disabled={disabled}
                  creating={creatingTag}
                  label="Add or create tag"
                  inputClassName="w-full"
                  listClassName="w-full"
                  onAttach={(tagId) => void attachTag(tagId)}
                  onCreate={(name) => void createAndAttachTag(name)}
                />
              </div>
              {tagError !== null && (
                <p role="alert" className="mt-1 text-xs text-danger">
                  {tagError}
                </p>
              )}
            </div>
          )}
          <label className="text-xs text-muted sm:col-span-2">
            Raw description *
            <input
              required
              disabled={disabled}
              value={draft.rawDescription}
              onChange={(event) => set("rawDescription", event.target.value)}
              className={INPUT_CLASS}
            />
          </label>
        </div>

        {/* M108 P4 (docs/09 §Receipt images). One-to-one: attaching when
            there is none, replacing when there is, and a Remove that is
            always exactly one control — not a separate "change" path. The
            panel exists because OCR is out of scope (the card's own note);
            when that drops, this is where the extracted fields land. */}
        {target !== null && (
          <div>
            <label htmlFor="receipt-image" className="text-xs text-muted">
              Receipt image
            </label>
            {hasReceipt ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="inline-flex min-h-11 items-center gap-1.5 rounded-full border border-control bg-veil/[0.06] px-3 py-1 text-xs text-muted">
                  <Paperclip className="size-3.5" aria-hidden />
                  One image attached
                </span>
                <button
                  type="button"
                  onClick={() => void viewReceipt()}
                  disabled={receiptBusy}
                  className="min-h-11 rounded px-3 py-1 text-xs text-accent outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
                >
                  View
                </button>
                <button
                  type="button"
                  onClick={() => void removeReceipt()}
                  disabled={receiptBusy}
                  className="min-h-11 rounded px-3 py-1 text-xs text-danger outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-50"
                >
                  Remove
                </button>
              </div>
            ) : (
              <>
                <button
                  type="button"
                  onClick={() => receiptFileRef.current?.click()}
                  disabled={receiptBusy}
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-control bg-veil/[0.04] px-3 py-1.5 text-xs text-muted outline-none transition-colors hover:border-accent/70 hover:text-accent focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
                >
                  <Paperclip className="size-3.5" aria-hidden />
                  {receiptBusy ? "Attaching…" : "Attach image"}
                </button>
                <p className="mt-1 text-xs text-muted">
                  webp/jpeg/png/gif, up to 2 MB — shrunk in the browser first.
                </p>
              </>
            )}
            {/* The input lives here rather than on the page, so its label and
                state stay with the controls that use it. */}
            <input
              id="receipt-image"
              ref={receiptFileRef}
              type="file"
              accept="image/webp,image/jpeg,image/png,image/gif"
              className="sr-only"
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (file !== undefined) void attachReceipt(file);
              }}
            />
          </div>
        )}

        {/* docs/47 §4 / docs/09 §Read-only sessions #3: a refused demo write
            renders the local `role="status"` explanation (passed in by the
            host that owns the 403) instead of the error line — and it is
            EITHER that or the alert, never both. */}
        {demoRefusal}
        {!refusedByDemo &&
          (validationError !== null || (error !== undefined && error !== null)) && (
            <p role="alert" className="mt-3 text-sm text-danger">
              {validationError ?? apiErrorMessage(error)}
            </p>
          )}
        <div className="mt-5 flex justify-end gap-2">
          {/* M106 (docs/09 §History drawer): the row's receipts, one click from
              where the row is already being edited. The page closes THIS dialog
              and opens the drawer, so only one dialog is ever mounted. */}
          {target !== null && onHistory !== undefined && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={disabled}
              onClick={() => onHistory(target)}
              aria-label="History for this transaction"
            >
              History
            </Button>
          )}
          <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={onClose}>
            Cancel
          </Button>
          <span className="inline-flex flex-col items-end gap-1">
            <Button
              type="submit"
              variant="primary"
              size="sm"
              loading={saving}
              aria-describedby={locked ? TRANSACTION_SAVE_REASON_ID : undefined}
            >
              {locked && <DemoLock />}
              {target ? "Save changes" : "Add transaction"}
            </Button>
            {/* docs/47 §4: the VISIBLE reason the padlock binds to (F-4) —
                same dialog, non-zero box, never sr-only. */}
            {locked && (
              <DemoReason id={TRANSACTION_SAVE_REASON_ID}>
                The demo account is read-only — saving this transaction would be refused.
              </DemoReason>
            )}
          </span>
        </div>
      </form>
      </div>
    </ModalPortal>
  );
}
