// Imports (docs/09, docs/03): staged-review wizard — file → account → preview
// with duplicate flags → commit/discard — plus batch status history and an
// institutions CRUD mini-editor with a CSV import-profile editor.
// Presentation pass: Upload→Review→Done stepper header, big commit CTA,
// zebra + duplicate tint retained, dot-badges for statuses, toast feedback.

import { useState, type Dispatch, type SetStateAction } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, FileUp, Inbox } from "lucide-react";
import {
  DryRunResponse,
  ImportBatch,
  PreviewRow,
  SuggestProfileResponse,
  UploadResponse,
  commitBatch,
  discardBatch,
  listImports,
  revertBatch,
  getBatch,
  previewImport,
  suggestProfile,
  uploadImport,
  uploadImportWithProfile,
  getBatchRows,
} from "../api/imports";
import {
  BROKERAGE_PROFILES,
  commitTradeBatch,
  discardTradeBatch,
  importTrades,
} from "../api/trades";
import type { TradeImportResponse, TradePreviewRow } from "../api/trades";
import { INVESTMENT_ACCOUNT_TYPES } from "../api/accounts";
import {
  ImporterProfile,
  Institution,
  ProfileSigns,
  createInstitution,
  deleteInstitution,
  listInstitutions,
  updateInstitution,
} from "../api/institutions";
import {
  createAccount,
  listAccounts,
} from "../api/accounts";
import type { Account, AccountCreateInput } from "../api/accounts";
import { hasErrorCode } from "../api/client";
import { AccountForm } from "../components/settings/AccountsSection";
import { apiErrorMessage, formatMoney } from "../components/transactions/format";
import { Badge } from "../components/ui/Badge";
import type { BadgeTone } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { HelpHint } from "../components/ui/HelpHint";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { QueryErrorPanel } from "../components/ui/QueryErrorPanel";
import { useRetryAction } from "../lib/retry";
import { SegmentedControl } from "../components/ui/SegmentedControl";
import { deferDelete } from "../components/ui/deferDelete";
import { SkeletonRows } from "../components/ui/Skeleton";
import { fadeUp } from "../components/ui/motion";
import { useToast } from "../components/ui/Toast";
import { YnabMigrationCard } from "../components/imports/YnabMigrationCard";
import { NetWorthImportCard } from "../components/imports/NetWorthImportCard";
import {
  ServerSideBlocked,
  useServerSideWriteBlocked,
  type ServerSideCapability,
} from "../components/e2ee/ServerSideBlocked";

const INPUT_CLASS =
  "field-ring rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";

// Human names for the sniffed format and where a near-duplicate twin lives —
// the preview shows labels, never internal tokens.
const FORMAT_LABELS: Record<string, string> = {
  ofx: "OFX / QFX",
  qif: "QIF",
  csv: "CSV",
};
const MATCH_SOURCE_LABELS: Record<string, string> = {
  committed: "already imported",
  staged: "in another import awaiting review",
  batch: "also in this file",
};

// --- Stepper (ui-contract Imports must-have) ---------------------------------

const STEPS = ["Upload", "Review", "Done"] as const;

function Stepper({ current }: { current: number }) {
  return (
    <ol className="flex items-center gap-2" aria-label={`Step ${current + 1} of ${STEPS.length}`}>
      {STEPS.map((label, i) => {
        const done = i < current;
        const active = i === current;
        const pending = i > current;
        return (
          <li key={label} className="flex flex-1 items-center gap-2 last:flex-none">
            <span
              className={`flex size-6 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold transition-all duration-300 ${
                done
                  ? "bg-gain/20 text-gain ring-1 ring-gain/40"
                  : active
                    ? "bg-gradient-to-br from-accent to-accent-soft text-accent-ink shadow-glow-sm"
                    : "bg-veil/[0.06] text-muted ring-1 ring-inset ring-veil/[0.08]"
              }`}
            >
              {done ? <Check className="size-3.5" aria-hidden /> : i + 1}
            </span>
            <span
              className={`text-xs transition-colors ${
                active ? "font-medium text-text" : pending ? "text-muted" : "text-gain"
              }`}
            >
              {label}
            </span>
            {i < STEPS.length - 1 && (
              <span className="relative h-px flex-1 overflow-hidden rounded bg-veil/[0.08]">
                {/* Progress fill sweeps toward the active step. */}
                <motion.span
                  initial={false}
                  animate={{ scaleX: done || active ? 1 : 0 }}
                  transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
                  style={{ originX: 0 }}
                  className="absolute inset-0 bg-gradient-to-r from-accent/60 to-accent-soft/60"
                />
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

// --- Status badge (dot + label treatment) ------------------------------------

const STATUS_TONE: Record<ImportBatch["status"], BadgeTone> = {
  pending_review: "warn",
  committed: "success",
  discarded: "neutral",
  reverted: "danger",
};

/**
 * Display labels for the batch enum (M68): the raw enum token is an internal
 * name, so the badge shows the workflow word — `pending_review` renders as
 * "Review", never the snake_case value.
 */
const STATUS_LABEL: Record<ImportBatch["status"], string> = {
  pending_review: "Review",
  committed: "Committed",
  discarded: "Discarded",
  reverted: "Reverted",
};

function StatusBadge({ status }: { status: ImportBatch["status"] }) {
  return <Badge tone={STATUS_TONE[status]}>{STATUS_LABEL[status]}</Badge>;
}

// --- Column mapping wizard (LLM-suggested CSV column mapping) ---------------

type MappingMode = "signed" | "split";

interface MappingDraft {
  mode: MappingMode;
  columns: Record<string, string>;
  dateFormat: string;
  decimalComma: boolean;
  creditsPositive: boolean;
  /** True when columns map by 1-based position (no header row). */
  headerless: boolean;
}

/**
 * Every column key this wizard and the suggester can emit (docs/03 profile
 * contract), grouped by what owns it. Payloads are assembled from THIS list —
 * never by spreading whatever sits in the draft — because the draft record is
 * persistent and a mode switch used to leave the other mode's keys behind
 * (M111: `credit: ""` shipped after switching to single-amount, 422'd by the
 * backend's non-empty-header check).
 */
const BASE_COLUMNS = ["date", "raw_description"] as const;
const OPTIONAL_COLUMNS = ["posting_date", "currency"] as const;
const MODE_COLUMNS: Record<MappingMode, readonly string[]> = {
  signed: ["amount_native"],
  split: ["debit", "credit"],
};

/** The keys owned by the amount mode the draft is NOT in. */
function foreignColumns(mode: MappingMode): readonly string[] {
  return MODE_COLUMNS[mode === "signed" ? "split" : "signed"];
}

/**
 * The only shape `MappingDraft.columns` may take: the other amount mode's keys
 * are deleted and blank mappings dropped. Applied when a suggestion is ingested
 * and at every mode switch (the "belt" half of the M111 fix — `draftToProfile`
 * is the other half). A blank select reads back as `""` either way
 * (`value ?? ""`), so dropping the key is invisible to the UI. Values are kept
 * verbatim — only emptiness is judged, never the header spelling.
 */
function columnsForMode(
  columns: Record<string, string>,
  mode: MappingMode,
): Record<string, string> {
  const drop = new Set(foreignColumns(mode));
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(columns)) {
    if (drop.has(key) || value.trim() === "") continue;
    out[key] = value;
  }
  return out;
}

/** `columns` for the profile payload: keyed by mode, never an empty string. */
function payloadColumns(draft: MappingDraft): Record<string, string> {
  const own = [...BASE_COLUMNS, ...MODE_COLUMNS[draft.mode], ...OPTIONAL_COLUMNS];
  const forbidden = new Set(foreignColumns(draft.mode));
  const columns: Record<string, string> = {};
  for (const key of own) {
    const value = draft.columns[key];
    // An empty select must be OMITTED, never sent as "" — the backend rejects
    // a blank mapping, and a hidden one is what made this fail before.
    if (value !== undefined && value.trim() !== "") columns[key] = value;
  }
  // A key outside the known list (a future suggester field) passes through only
  // when populated, and never the other mode's keys.
  for (const [key, value] of Object.entries(draft.columns)) {
    if (own.includes(key) || forbidden.has(key) || value.trim() === "") continue;
    columns[key] = value;
  }
  return columns;
}

// Exported for the M111 regression test (stale debit/credit after a mode
// switch must never reach the API).
export function draftFromSuggestion(sug: SuggestProfileResponse): MappingDraft {
  const hasSplit = Boolean(sug.columns.debit && sug.columns.credit);
  const mode: MappingMode = hasSplit ? "split" : "signed";
  return {
    mode,
    // The suggester echoes the whole record, so it can hand back the other
    // mode's keys or blank values — normalize before the draft ever holds them.
    columns: columnsForMode(sug.columns, mode),
    dateFormat: sug.date_format,
    decimalComma: sug.decimal_comma,
    creditsPositive: sug.credits_positive,
    headerless: sug.headerless ?? false,
  };
}

export function draftToProfile(draft: MappingDraft): ImporterProfile {
  const columns = payloadColumns(draft);
  if (draft.mode === "split") {
    return {
      columns,
      date_format: draft.dateFormat,
      decimal_comma: draft.decimalComma,
      headerless: draft.headerless,
    };
  }
  return {
    columns,
    date_format: draft.dateFormat,
    decimal_comma: draft.decimalComma,
    credits_positive: draft.creditsPositive,
    headerless: draft.headerless,
  };
}

// Exported for the M111 regression test (mode-switch round trip).
export function ColumnMappingWizard({
  headers,
  suggestedProfile,
  onConfirm,
  onCancel,
  saving,
}: {
  headers: { label: string; value: string }[];
  suggestedProfile: SuggestProfileResponse;
  onConfirm: (profile: ImporterProfile, saveToInstitution: boolean) => void;
  onCancel: () => void;
  saving: boolean;
}) {
  const [draft, setDraft] = useState<MappingDraft>(() =>
    draftFromSuggestion(suggestedProfile),
  );
  const [saveToInstitution, setSaveToInstitution] = useState(false);

  // Headerful: options are the real header names. Headerless: synthesize
  // 1-based "Column N" labels (positions) from the detected column count.
  const columnOptions = draft.headerless
    ? Array.from({ length: headers.length }, (_, i) => ({
        label: `Column ${i + 1}`,
        value: String(i + 1),
      }))
    : headers;

  const required =
    draft.mode === "split"
      ? ["date", "raw_description", "debit", "credit"]
      : ["date", "amount_native", "raw_description"];
  const missing = required.filter((key) => !draft.columns[key]?.trim());

  return (
    <motion.div
      variants={fadeUp}
      initial="hidden"
      animate="show"
      className="mt-3 rounded-lg border border-veil/[0.06] p-4"
    >
      <div className="mb-3 flex items-center justify-between">
        <div>
          <p className="text-sm font-medium">Column Mapping</p>
          {suggestedProfile.confidence > 0 && (
            <p className="text-xs text-muted">
              {suggestedProfile.reasoning || `Confidence: ${(suggestedProfile.confidence * 100).toFixed(0)}%`}
            </p>
          )}
        </div>
        <Badge tone={suggestedProfile.confidence >= 0.7 ? "success" : suggestedProfile.confidence >= 0.4 ? "warn" : "danger"}>
          {suggestedProfile.confidence >= 0.7 ? "High confidence" : suggestedProfile.confidence >= 0.4 ? "Review needed" : "Low confidence"}
        </Badge>
      </div>

      {/* CSV headers preview */}
      <div className="mb-3 flex flex-wrap gap-1.5">
        {columnOptions.map((h) => (
          <span key={h.value} className="rounded bg-veil/[0.06] px-2 py-0.5 text-xs text-muted">
            {h.label}
          </span>
        ))}
      </div>

      {draft.headerless && (
        <p className="mb-3 text-xs text-muted">
          Columns are mapped by position (1-based) because no header row was detected.
        </p>
      )}

      <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
        <input
          type="checkbox"
          checked={draft.headerless}
          onChange={(e) => setDraft({ ...draft, headerless: e.target.checked, columns: {} })}
          className="size-3.5 accent-[rgb(var(--color-accent))]"
        />
        This file has no header row
      </label>

      <div className="grid gap-2 sm:grid-cols-2">
        <div className="flex items-center gap-3 sm:col-span-2">
          <span className="text-xs text-muted">Amount columns</span>
          <SegmentedControl<MappingMode>
            layoutId="mapping-mode"
            size="sm"
            ariaLabel="Column mapping mode"
            options={[
              { value: "signed", label: "Single amount", title: "One signed amount column" },
              { value: "split", label: "Debit & Credit", title: "Separate debit and credit columns" },
            ]}
            value={draft.mode}
            onChange={(mode) =>
              // M111 belt: switching modes deletes the other mode's keys (and
              // any blank mapping), so `columns` can't accumulate stale state.
              setDraft({ ...draft, mode, columns: columnsForMode(draft.columns, mode) })
            }
          />
        </div>

        <label className="text-xs text-muted">
          Date column
          <select
            value={draft.columns.date ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, columns: { ...draft.columns, date: e.target.value } })
            }
            className={`${INPUT_CLASS} mt-1 block w-full`}
          >
            <option value="">Select…</option>
            {columnOptions.map((h) => (
              <option key={h.value} value={h.value}>{h.label}</option>
            ))}
          </select>
        </label>

        <label className="text-xs text-muted">
          Description column
          <select
            value={draft.columns.raw_description ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, columns: { ...draft.columns, raw_description: e.target.value } })
            }
            className={`${INPUT_CLASS} mt-1 block w-full`}
          >
            <option value="">Select…</option>
            {columnOptions.map((h) => (
              <option key={h.value} value={h.value}>{h.label}</option>
            ))}
          </select>
        </label>

        {draft.mode === "signed" ? (
          <label className="text-xs text-muted">
            Amount column
            <select
              value={draft.columns.amount_native ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, columns: { ...draft.columns, amount_native: e.target.value } })
              }
              className={`${INPUT_CLASS} mt-1 block w-full`}
            >
              <option value="">Select…</option>
            {columnOptions.map((h) => (
              <option key={h.value} value={h.value}>{h.label}</option>
            ))}
            </select>
          </label>
        ) : (
          <>
            <label className="text-xs text-muted">
              Debit column
              <select
                value={draft.columns.debit ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, columns: { ...draft.columns, debit: e.target.value } })
                }
                className={`${INPUT_CLASS} mt-1 block w-full`}
              >
                <option value="">Select…</option>
            {columnOptions.map((h) => (
              <option key={h.value} value={h.value}>{h.label}</option>
            ))}
              </select>
            </label>
            <label className="text-xs text-muted">
              Credit column
              <select
                value={draft.columns.credit ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, columns: { ...draft.columns, credit: e.target.value } })
                }
                className={`${INPUT_CLASS} mt-1 block w-full`}
              >
                <option value="">Select…</option>
            {columnOptions.map((h) => (
              <option key={h.value} value={h.value}>{h.label}</option>
            ))}
              </select>
            </label>
          </>
        )}

        <label className="text-xs text-muted">
          Posting date column (optional)
          <select
            value={draft.columns.posting_date ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, columns: { ...draft.columns, posting_date: e.target.value } })
            }
            className={`${INPUT_CLASS} mt-1 block w-full`}
          >
            <option value="">None</option>
            {columnOptions.map((h) => (
              <option key={h.value} value={h.value}>{h.label}</option>
            ))}
          </select>
        </label>

        <label className="text-xs text-muted">
          Currency column (optional)
          <select
            value={draft.columns.currency ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, columns: { ...draft.columns, currency: e.target.value } })
            }
            className={`${INPUT_CLASS} mt-1 block w-full`}
          >
            <option value="">None</option>
            {columnOptions.map((h) => (
              <option key={h.value} value={h.value}>{h.label}</option>
            ))}
          </select>
        </label>

        <label className="text-xs text-muted">
          Date format
          <input
            value={draft.dateFormat}
            onChange={(e) => setDraft({ ...draft, dateFormat: e.target.value })}
            placeholder="%Y-%m-%d"
            className={`${INPUT_CLASS} mt-1 block w-full`}
          />
        </label>

        <div className="flex flex-col gap-2">
          <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={draft.decimalComma}
              onChange={(e) => setDraft({ ...draft, decimalComma: e.target.checked })}
              className="size-3.5 accent-[rgb(var(--color-accent))]"
            />
            Decimal comma
          </label>
          {draft.mode === "signed" && (
            <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={draft.creditsPositive}
                onChange={(e) => setDraft({ ...draft, creditsPositive: e.target.checked })}
                className="size-3.5 accent-[rgb(var(--color-accent))]"
              />
              Credits positive
            </label>
          )}
          <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={saveToInstitution}
              onChange={(e) => setSaveToInstitution(e.target.checked)}
              className="size-3.5 accent-[rgb(var(--color-accent))]"
            />
            Save as institution profile
          </label>
        </div>
      </div>

      {missing.length > 0 && (
        <p className="mt-2 text-xs text-warn">
          Required columns missing: {missing.join(", ")}
        </p>
      )}

      <div className="mt-3 flex gap-2">
        <Button
          variant="primary"
          size="sm"
          loading={saving}
          disabled={missing.length > 0}
          onClick={() => onConfirm(draftToProfile(draft), saveToInstitution)}
        >
          Confirm mappings
        </Button>
        <Button variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </motion.div>
  );
}

// --- Upload wizard ------------------------------------------------------------

/**
 * M112 — the wizard's auto-launch keys off the envelope's machine-readable
 * `code`, never the human `detail`.
 *
 * A thin wrapper over `hasErrorCode` (`frontend/src/api/client.ts`), which
 * holds the reasoning for the whole app: `apiErrorMessage(err)` is
 * `ApiError.message`, and `ApiError` calls `super(detail)` — so the old
 * `msg.includes("missing_importer_profile")` test could never match the code:
 * the branch was dead, the 422 fell through to `toast.error`, and the wizard
 * never opened (docs/21 documented the intent as working). `code` is the
 * contract field (docs/08) and survives copy edits to `detail`.
 */
export function isMissingImporterProfile(err: unknown): boolean {
  return hasErrorCode(err, "missing_importer_profile");
}

interface UploadWizardProps {
  preview: UploadResponse | null;
  setPreview: Dispatch<SetStateAction<UploadResponse | null>>;
  included: Set<number>;
  setIncluded: Dispatch<SetStateAction<Set<number>>>;
  mappingWizard: { headers: { label: string; value: string }[]; profile: SuggestProfileResponse } | null;
  setMappingWizard: Dispatch<SetStateAction<{ headers: { label: string; value: string }[]; profile: SuggestProfileResponse } | null>>;
}

function UploadWizard({
  preview,
  setPreview,
  included,
  setIncluded,
  mappingWizard,
  setMappingWizard,
}: UploadWizardProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => listAccounts(200) });
  const institutions = useQuery({
    queryKey: ["institutions"],
    queryFn: () => listInstitutions(200),
  });

  const [file, setFile] = useState<File | null>(null);
  const [accountId, setAccountId] = useState("");
  // Step 3 lingers after a commit until the user starts a new upload.
  const [doneFlash, setDoneFlash] = useState(false);
  // Inline "+ New account" creation (docs/14): error text from the {detail}
  // envelope while the compact form is open.
  const [newAccountError, setNewAccountError] = useState<string | null>(null);
  // Column mapping wizard state

  // Investment CSV import state
  const [investmentPreview, setInvestmentPreview] = useState<TradeImportResponse | null>(null);
  // M71 cancel-safe dry run: parsed rows + column roles + duplicate verdicts,
  // with NOTHING persisted. Confirm is a separate upload call; cancel is a no-op.
  const [dryRun, setDryRun] = useState<DryRunResponse | null>(null);
  // Wizard-confirmed mapping profile, replayed on Confirm so the confirmed
  // import matches the previewed one exactly.
  const [confirmedProfile, setConfirmedProfile] = useState<
    Record<string, unknown> | null
  >(null);
  // Per-row "include anyway" selections for duplicate rows (committed via include_ids).
  const [modeOverride, setModeOverride] = useState<"auto" | "investment" | "bank">("auto");
  // M82 per-institution brokerage profile (docs/41). "" = auto-detect from the
  // file header; picking an institution pins the parser and refuses a file that
  // does not match that broker's export instead of force-fitting it.
  const [brokerProfile, setBrokerProfile] = useState("");
  const isInvestmentImport = investmentPreview !== null;
  // M181 (docs/33 rule 6): every WRITE in this wizard is refused server-side for
  // an opted-in tenant — `POST /imports/upload`, `POST /trades/import` and both
  // commits — so each write control is replaced by its reason BEFORE submit.
  // The zero-write previews (`POST /imports/preview`, the mapping wizard) stay
  // live: they persist no row, so the guard has nothing to refuse (docs/33
  // exclusion table). ENROLMENT ALONE — unlocking the vault cannot enable a
  // server-side import (rule 7d).
  const serverWriteBlocked = useServerSideWriteBlocked();

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ["imports"] });
    void queryClient.invalidateQueries({ queryKey: ["transactions"] });
  }

  const createInlineAccount = useMutation({
    mutationFn: (input: AccountCreateInput) => createAccount(input),
    onSuccess: (account) => {
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      setNewAccountError(null);
      setAccountId(String(account.id));
      toast.success("Account created", account.name);
    },
    onError: (err) => setNewAccountError(apiErrorMessage(err)),
  });

  const step =
    dryRun !== null || preview !== null || investmentPreview !== null
      ? 1
      : doneFlash
        ? 2
        : 0;

  // Fetch LLM-suggested column mappings from CSV
  const fetchHeaders = useMutation({
    mutationFn: (f: File) => suggestProfile(f),
    onSuccess: async (result: SuggestProfileResponse, f: File) => {
      const text = await f.text();
      const firstLine = text.split("\n")[0];
      const named = firstLine.split(",").map((h: string) => {
        const t = h.trim().replace(/^"|"$/g, "");
        return { label: t, value: t };
      });
      setMappingWizard({ headers: named, profile: result });
    },
    onError: (err: Error) => toast.error("Failed to analyze CSV", apiErrorMessage(err)),
  });

  // M71 dry run: same validation chain as upload, ZERO writes (docs/03).
  const runPreview = useMutation({
    mutationFn: (profile?: Record<string, unknown>) =>
      previewImport(file as File, Number(accountId), profile),
    onSuccess: (data: DryRunResponse) => {
      setDryRun(data);
      setDoneFlash(false);
      setMappingWizard(null);
    },
    onError: (err: Error) => {
      if (isMissingImporterProfile(err)) {
        // No mapping yet: collect one, then re-run the preview with it.
        if (file) fetchHeaders.mutate(file);
      } else {
        toast.error("Preview failed", apiErrorMessage(err));
      }
    },
  });

  // Upload with profile override (wizard-confirmed mappings)
  const uploadWithProfile = useMutation({
    mutationFn: ({ profile }: { profile: ImporterProfile }) =>
      uploadImportWithProfile(file as File, Number(accountId), profile as unknown as Record<string, unknown>),
    onSuccess: (data: UploadResponse) => {
      setPreview(data);
      setIncluded(new Set());
      setDoneFlash(false);
      setMappingWizard(null);
      setDryRun(null);
      setConfirmedProfile(null);
      invalidate();
      toast.success(
        "Statement parsed",
        `${data.imported} new · ${data.duplicates_skipped} duplicates skipped${data.unresolved > 0 ? ` · ${data.unresolved} unresolved` : ""}`,
      );
    },
    onError: (err: Error) => toast.error("Upload failed", apiErrorMessage(err)),
  });

  // Import mode: auto-derived from the selected account type, overridable via
  // the SegmentedControl below (e.g. an investment account whose file is
  // actually a bank statement, or vice-versa).
  const selectedAccount = (accounts.data?.items ?? []).find(
    (a: Account) => String(a.id) === accountId,
  );
  const derivedMode: "investment" | "bank" =
    selectedAccount && selectedAccount.type in INVESTMENT_ACCOUNT_TYPES ? "investment" : "bank";
  const mode = modeOverride === "auto" ? derivedMode : modeOverride;
  const isInvestmentAccount = mode === "investment";
  // Which write the next press would make — the same branch `handleSubmit`
  // takes, so the notice and the request can never name different routes.
  const uploadCapability: ServerSideCapability = isInvestmentAccount
    ? "import:trades"
    : "import:statement";
  /** Both commits funnel here: bank batches and staged trade batches differ. */
  const commitCapability: ServerSideCapability = isInvestmentImport
    ? "import:trades-commit"
    : "import:statement-commit";

  // Standard upload (uses institution profile) — routes to investment CSV for investment accounts
  const upload = useMutation({
    mutationFn: async () => {
      if (isInvestmentAccount) {
        return importTrades(file as File, Number(accountId), brokerProfile || null);
      }
      return uploadImport(file as File, Number(accountId));
    },
    onSuccess: (data: UploadResponse | TradeImportResponse) => {
      if ("batch_id" in data && !("status" in data)) {
        // TradeImportResponse — investment CSV
        setInvestmentPreview(data as TradeImportResponse);
      } else {
        setPreview(data as UploadResponse);
      }
      setIncluded(new Set());
      setDoneFlash(false);
      setMappingWizard(null);
      setDryRun(null);
      setConfirmedProfile(null);
      invalidate();
      const d = data as { imported?: number; duplicates_skipped?: number; unresolved?: number };
      toast.success(
        isInvestmentAccount ? "Trades parsed" : "Statement parsed",
        `${d.imported ?? 0} new · ${d.duplicates_skipped ?? 0} duplicates skipped${(d.unresolved ?? 0) > 0 ? ` · ${d.unresolved} unresolved` : ""}`,
      );
    },
    onError: (err: Error) => {
      const msg = apiErrorMessage(err);
      if (isInvestmentAccount) {
        toast.error("Import failed", msg);
        return;
      }
      // Bank-statement parser: a missing importer profile triggers the
      // column-mapping wizard so the user can map the columns manually.
      if (isMissingImporterProfile(err)) {
        if (file) fetchHeaders.mutate(file);
      } else {
        toast.error("Upload failed", msg);
      }
    },
  });

const finish = useMutation({
    mutationFn: async ({ batchId, commit }: { batchId: number; commit: boolean }) => {
      if (isInvestmentImport) {
        if (commit) {
          await commitTradeBatch(batchId, Array.from(included));
        } else {
          await discardTradeBatch(batchId);
        }
        return;
      }
      if (commit) {
        await commitBatch(batchId, Array.from(included));
      } else {
        await discardBatch(batchId);
      }
    },
    onSuccess: (_data, variables) => {
      if (variables.commit) {
        toast.success(isInvestmentImport ? "Trades committed" : "Batch committed", isInvestmentImport ? "Trades are live and ACB pools rebuilt." : "Its rows are now live transactions.");
      } else {
        toast.info("Batch discarded");
      }
      setPreview(null);
      setInvestmentPreview(null);
      setFile(null);
      setIncluded(new Set());
      setDoneFlash(variables.commit);
      invalidate();
    },
    onError: (err, variables) =>
      toast.error(variables.commit ? "Commit failed" : "Discard failed", apiErrorMessage(err)),
  });

  // F-RETRY (docs/47 §3): a failed UPLOAD is a pure staging POST — the same
  // file and account are still in state and nothing was written — so re-running
  // it verbatim is the honest way back. A failed COMMIT is not: the batch may
  // have partially landed, and its recovery is the batch history below (revert
  // within the window), never a blind second POST. So only the upload half
  // carries a retry, and only while its file is still chosen.
  const uploadRetry = useRetryAction({
    retry: upload.isError && file !== null ? () => upload.mutate() : null,
    pending: upload.isPending,
  });

  function startOver() {
    setPreview(null);
    setInvestmentPreview(null);
    setDryRun(null);
    setConfirmedProfile(null);
    setFile(null);
    setDoneFlash(false);
    setMappingWizard(null);
    setModeOverride("auto");
    setBrokerProfile("");
  }

  function cancelPreview() {
    // Cancel-safe by construction: the dry run wrote nothing, so clearing it
    // is the whole undo (docs/03 preview contract).
    setDryRun(null);
    setConfirmedProfile(null);
    toast.info("Preview cancelled", "Nothing was imported.");
  }

  function confirmImport() {
    // Rule 6 defense-in-depth: the Confirm control is replaced below for an
    // opted-in tenant; this refuses the call if anything re-enables it.
    if (serverWriteBlocked) return;
    if (confirmedProfile !== null) {
      uploadWithProfile.mutate({
        profile: confirmedProfile as unknown as ImporterProfile,
      });
    } else {
      upload.mutate();
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (mappingWizard) {
      // Wizard is active: user must click "Confirm mappings"
      return;
    }
    if (isInvestmentAccount) {
      // Trade import already previews + stages in one call (docs/03).
      // Rule 6 defense-in-depth: the control is replaced below, but Enter in a
      // field would still submit — refuse before the doomed POST /trades/import.
      if (serverWriteBlocked) return;
      upload.mutate();
      return;
    }
    // Bank statement: dry run first, Confirm persists (docs/03) — and the dry
    // run is a ZERO-write preview, so it stays available while gated.
    runPreview.mutate();
  }

  function handleWizardConfirm(profile: ImporterProfile, saveToInstitution: boolean) {
    if (saveToInstitution) {
      const selectedAccountId = Number(accountId);
      const account = (accounts.data?.items ?? []).find((a: Account) => a.id === selectedAccountId);
      if (account?.institution_id) {
        updateInstitution(account.institution_id, {
          importer_profile: profile,
        }).catch(() => {
          // Non-fatal: profile still works for this upload
        });
      }
    }
    setConfirmedProfile(profile as unknown as Record<string, unknown>);
    runPreview.mutate(profile as unknown as Record<string, unknown>);
  }

  return (
    <Card hover className="p-4">
      <CardTitle right={<Stepper current={step} />}>Upload a statement</CardTitle>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={handleSubmit}
      >
        {/* min-w-0 + max-w-64: the intrinsic file-input width overflowed this
            row's parent at 320 (M82-F3). */}
        <label className="min-w-0 text-xs text-muted">
          Statement file (.csv / .qfx / .qif / .ofx)
          <input
            type="file"
            accept=".csv,.ofx,.qfx,.qif"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setDoneFlash(false);
              setMappingWizard(null);
              // A new file invalidates a previously confirmed mapping: the
              // next preview must re-resolve the profile it will confirm with.
              setConfirmedProfile(null);
            }}
            className="mt-1 block w-full max-w-64 text-sm text-muted file:mr-2 file:cursor-pointer file:rounded-lg file:border-0 file:bg-veil/[0.06] file:px-2.5 file:py-1.5 file:text-sm file:text-text hover:file:bg-veil/[0.1]"
          />
        </label>
        <label className="text-xs text-muted">
          Account
          <select
            value={accountId}
            onChange={(e) => {
              setAccountId(e.target.value);
              setConfirmedProfile(null); // a different account may be a different institution
            }}
            className={`${INPUT_CLASS} mt-1 block max-w-48`}
            required
          >
            <option value="" disabled>
              Select account…
            </option>
            {(accounts.data?.items ?? []).map((account: Account) => (
              <option key={account.id} value={String(account.id)}>
                {account.name} ({account.currency})
              </option>
            ))}
            <option value="__new__">+ New account…</option>
          </select>
        </label>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted">Import as</span>
          <SegmentedControl
            ariaLabel="Import mode"
            layoutId="import-mode"
            size="sm"
            value={mode}
            onChange={(v) => setModeOverride(v as "investment" | "bank")}
            options={[
              { value: "bank", label: "Bank statement" },
              { value: "investment", label: "Investment trades" },
            ]}
          />
        </div>
        {/* M82: one parser per brokerage. Pinning an institution refuses a file
            that does not match that broker's export instead of force-fitting it;
            leaving it on auto-detect runs the header matcher. */}
        {isInvestmentAccount && (
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted">Broker export</span>
            <select
              value={brokerProfile}
              onChange={(e) => setBrokerProfile(e.target.value)}
              className={`${INPUT_CLASS} mt-1 block max-w-64`}
            >
              <option value="">Auto-detect from the file</option>
              {BROKERAGE_PROFILES.map((profile) => (
                <option key={profile.slug} value={profile.slug}>
                  {profile.label}
                </option>
              ))}
            </select>
          </label>
        )}
        {mappingWizard === null && dryRun === null && (
          <>
            {/* Rule 6: an investment upload STAGES rows server-side, so for an
                opted-in tenant the control is replaced by its reason. The bank
                path's "Preview import" is the ZERO-write dry run and stays. */}
            {serverWriteBlocked && isInvestmentAccount ? (
              <ServerSideBlocked capability={uploadCapability} id="import-upload-blocked" />
            ) : (
              <Button
                type="submit"
                variant="primary"
                loading={upload.isPending || runPreview.isPending}
                disabled={file === null || accountId === "" || accountId === "__new__"}
              >
                <FileUp className="size-4" aria-hidden />
                {isInvestmentAccount ? "Upload & parse" : "Preview import"}
              </Button>
            )}
            {file !== null && accountId !== "" && accountId !== "__new__" && (
              <Button
                type="button"
                variant="ghost"
                loading={fetchHeaders.isPending}
                onClick={() => {
                  if (file) fetchHeaders.mutate(file);
                }}
              >
                Configure mappings
              </Button>
            )}
          </>
        )}
        {doneFlash && (
          <Button variant="ghost" onClick={startOver}>
            Start another import
          </Button>
        )}
      </form>

      {/* Inline account creation (docs/14): first-run users never leave the
          wizard; the shared AccountsSection form keeps one source of truth. */}
      {accountId === "__new__" && (
        <motion.div
          variants={fadeUp}
          initial="hidden"
          animate="show"
          className="mt-3 max-w-md rounded-lg border border-veil/[0.06] p-3"
        >
          <p className="mb-2 text-xs font-semibold uppercase tracking-widest text-muted">
            New account
          </p>
          <AccountForm
            institutions={institutions.data?.items ?? []}
            error={newAccountError}
            submitLabel="Create & select"
            onSubmit={(input) => createInlineAccount.mutate(input)}
          />
        </motion.div>
      )}

      {/* Column mapping wizard */}
      {mappingWizard !== null && (
        <ColumnMappingWizard
          headers={mappingWizard.headers}
          suggestedProfile={mappingWizard.profile}
          onConfirm={handleWizardConfirm}
          onCancel={() => setMappingWizard(null)}
          saving={uploadWithProfile.isPending}
        />
      )}

      {/* M71 dry-run preview: column roles, row verdicts, cached-FX result —
          all read-only. Confirm performs the real (staging) upload; cancel
          simply never calls it, so the server keeps no residue. */}
      {dryRun !== null && (
        <motion.div variants={fadeUp} initial="hidden" animate="show" className="mt-4">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted">
            <Badge tone="accent">Preview — nothing is saved yet</Badge>
            <Badge tone="neutral">{FORMAT_LABELS[dryRun.format] ?? dryRun.format}</Badge>
            <Badge tone="success">{dryRun.summary.new} new</Badge>
            <Badge tone={dryRun.summary.duplicates > 0 ? "warn" : "neutral"}>
              {dryRun.summary.duplicates} exact duplicate
              {dryRun.summary.duplicates === 1 ? "" : "s"}
            </Badge>
            <Badge tone={dryRun.summary.near_duplicates > 0 ? "warn" : "neutral"}>
              {dryRun.summary.near_duplicates} near-duplicate
              {dryRun.summary.near_duplicates === 1 ? "" : "s"}
            </Badge>
            <Badge tone={dryRun.summary.unresolved > 0 ? "warn" : "neutral"}>
              {dryRun.summary.unresolved} unresolved (no cached fx rate)
            </Badge>
          </div>

          <div className="mt-3 rounded-lg border border-veil/[0.06] p-3">
            <p className="text-xs font-semibold uppercase tracking-widest text-muted">
              Column roles
            </p>
            <ul className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm">
              {dryRun.mapping.fields.map((f) => (
                <li key={f.field} className="text-muted">
                  <span className="text-text">{f.field}</span> ← {f.source}
                </li>
              ))}
            </ul>
            {dryRun.mapping.kind === "csv_profile" && (
              <p className="mt-2 text-xs text-muted">
                {dryRun.mapping.mode === "split"
                  ? "Debit and credit columns"
                  : "Signed amount column"}
                {" · dates read as "}
                {dryRun.mapping.date_format}
                {dryRun.mapping.decimal_comma ? " · decimal comma" : ""}
                {dryRun.mapping.credits_positive
                  ? " · credits positive"
                  : " · debits positive"}
                {dryRun.mapping.headerless ? " · no header row" : ""}
              </p>
            )}
          </div>

          <div role="region" aria-label="Import preview table" tabIndex={0} className="mt-3 overflow-x-auto rounded-lg border border-veil/[0.06]">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                  <th className="px-3 py-2 font-medium">Date</th>
                  <th className="px-3 py-2 font-medium">Description</th>
                  <th className="px-3 py-2 text-right font-medium">Amount</th>
                  <th className="px-3 py-2 text-right font-medium">CAD</th>
                  <th className="px-3 py-2 font-medium">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {dryRun.rows.map((row, index) => (
                  <tr
                    key={`${row.date}-${index}`}
                    className={
                      row.verdict === "duplicate"
                        ? "bg-danger/[0.07] text-muted"
                        : row.verdict === "near_duplicate"
                          ? "bg-warn/[0.06]"
                          : index % 2 === 1
                            ? "bg-veil/[0.02]"
                            : ""
                    }
                  >
                    <td className="whitespace-nowrap px-3 py-1.5 tabular">{row.date}</td>
                    <td className="max-w-72 truncate px-3 py-1.5">{row.raw_description}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right tabular">
                      {/* M139 D4: the import wizard is an ACTIVE working
                          surface — the user is entering and matching data
                          here, so amounts stay readable by design. */}
                      {formatMoney(row.amount_native, row.currency, false)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right text-muted tabular">
                      {row.cad_amount === null
                        ? "—"
                        : formatMoney(row.cad_amount, "CAD", false)}
                    </td>
                    <td className="px-3 py-1.5">
                      {row.verdict === "new" && <Badge tone="success">new</Badge>}
                      {row.verdict === "duplicate" && (
                        <Badge tone="danger">exact duplicate</Badge>
                      )}
                      {row.verdict === "near_duplicate" && (
                        <span className="flex flex-wrap items-center gap-1.5">
                          <Badge tone="warn">near-duplicate</Badge>
                          {row.near_duplicate_match !== null && (
                            <span className="text-xs text-muted">
                              matches “{row.near_duplicate_match.raw_description}” ·{" "}
                              {row.near_duplicate_match.delta_days === 0
                                ? "same day"
                                : `${row.near_duplicate_match.delta_days} day${
                                    row.near_duplicate_match.delta_days === 1 ? "" : "s"
                                  } apart`}
                              {" · "}
                              {MATCH_SOURCE_LABELS[row.near_duplicate_match.source] ??
                                "already imported"}
                            </span>
                          )}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            {/* Rule 6: Confirm is the POST /imports/upload that STAGES the rows
                the dry run just showed — the preview stays, the write does not. */}
            {serverWriteBlocked ? (
              <ServerSideBlocked capability={uploadCapability} id="import-confirm-blocked" />
            ) : (
              <Button
                variant="primary"
                size="lg"
                loading={upload.isPending || uploadWithProfile.isPending}
                onClick={confirmImport}
                disabled={upload.isPending || uploadWithProfile.isPending}
              >
                Confirm import of {dryRun.summary.total} row
                {dryRun.summary.total === 1 ? "" : "s"}
              </Button>
            )}
            <Button
              variant="outline"
              size="lg"
              disabled={upload.isPending || uploadWithProfile.isPending}
              onClick={cancelPreview}
            >
              Cancel — import nothing
            </Button>
          </div>
          <p className="mt-2 text-xs text-muted">
            Nothing is written until you press Confirm. Near-duplicates import as
            normal rows and stay flagged in the batch, so you can drop the batch or
            fix the row afterwards — nothing is ever dropped for you.
          </p>
        </motion.div>
      )}

      {preview !== null && (
        <motion.div variants={fadeUp} initial="hidden" animate="show" className="mt-4">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted">
            <StatusBadge status={preview.status} />
            <Badge tone="accent">{preview.imported} new</Badge>
            <Badge tone="neutral">{preview.duplicates_skipped} duplicates skipped</Badge>
            <Badge tone={preview.unresolved > 0 ? "warn" : "neutral"}>
              {preview.unresolved} unresolved (no cached fx rate)
            </Badge>
          </div>
          <div role="region" aria-label="Imported transactions table" tabIndex={0} className="mt-2 overflow-x-auto rounded-lg border border-veil/[0.06]">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                  <th className="px-3 py-2 font-medium">Date</th>
                  <th className="px-3 py-2 font-medium">Description</th>
                  <th className="px-3 py-2 text-right font-medium">Amount</th>
                  <th className="px-3 py-2 text-right font-medium">CAD</th>
                  <th className="px-3 py-2 font-medium">Duplicate</th>
                </tr>
              </thead>
              <tbody>
                {preview.preview.map((row: PreviewRow, index) => (
                  <tr
                    key={`${row.date}-${index}`}
                    // Zebra + duplicate tint retained (docs/03 review UX).
                    className={
                      row.duplicate
                        ? "bg-danger/[0.07] text-muted"
                        : index % 2 === 1
                          ? "bg-veil/[0.02]"
                          : ""
                    }
                  >
                    <td className="whitespace-nowrap px-3 py-1.5 tabular">{row.date}</td>
                    <td className="max-w-72 truncate px-3 py-1.5">{row.raw_description}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right tabular">
                      {/* M139 D4: the import wizard is an ACTIVE working
                          surface — the user is entering and matching data
                          here, so amounts stay readable by design. */}
                      {formatMoney(row.amount_native, row.currency, false)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right text-muted tabular">
                      {row.cad_amount === null
                        ? "—"
                        : formatMoney(row.cad_amount, "CAD", false)}
                    </td>
                    <td className="px-3 py-1.5">
                      {row.duplicate ? (
                        included.has(row.id) ? (
                          <Button
                            type="button"
                            variant="accent-soft"
                            size="sm"
                            onClick={() =>
                              setIncluded((s) => {
                                const n = new Set(s);
                                n.delete(row.id);
                                return n;
                              })
                            }
                          >
                            <Check className="h-3 w-3" /> included
                          </Button>
                        ) : (
                          <Button
                            type="button"
                            variant="danger"
                            size="sm"
                            onClick={() => setIncluded((s) => new Set(s).add(row.id))}
                          >
                            duplicate · include anyway
                          </Button>
                        )
                      ) : row.near_duplicate ? (
                        <span className="flex flex-wrap items-center gap-1.5">
                          <Badge tone="warn">near-duplicate</Badge>
                          {row.near_duplicate_match ? (
                            <span className="text-xs text-muted">
                              matches “{row.near_duplicate_match.raw_description}”
                            </span>
                          ) : null}
                        </span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* Big commit CTA (ui-contract).
              Rule 6: the commit is the POST /imports/{id}/commit that makes the
              staged rows live — replaced by its reason. Discard is a removal
              path and stays (docs/33 exclusion table). */}
          <div className="mt-4 flex flex-wrap items-center gap-3">
            {serverWriteBlocked ? (
              <ServerSideBlocked capability={commitCapability} id="import-commit-blocked" />
            ) : (
              <Button
                variant="primary"
                size="lg"
                loading={finish.isPending}
                onClick={() => finish.mutate({ batchId: preview.id, commit: true })}
                disabled={finish.isPending}
              >
                Commit {preview.imported} new transaction{preview.imported === 1 ? "" : "s"}
              </Button>
            )}
            <Button
              variant="outline"
              size="lg"
              disabled={finish.isPending}
              onClick={() => finish.mutate({ batchId: preview.id, commit: false })}
            >
              Discard batch
            </Button>
          </div>
        </motion.div>
      )}

      {/* Investment CSV preview */}
      {investmentPreview !== null && (
        <motion.div variants={fadeUp} initial="hidden" animate="show" className="mt-4">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted">
            <Badge tone="accent">{investmentPreview.imported} new</Badge>
            <Badge tone="neutral">{investmentPreview.duplicates_skipped} duplicates skipped</Badge>
            {investmentPreview.institution && (
              <Badge tone="neutral">{investmentPreview.institution}</Badge>
            )}
          </div>
          {investmentPreview.notices.length > 0 && (
            <ul className="mt-2 space-y-1 text-xs text-muted">
              {investmentPreview.notices.map((notice) => (
                <li key={notice}>{notice}</li>
              ))}
            </ul>
          )}
          <div role="region" aria-label="Investment import preview table" tabIndex={0} className="mt-2 overflow-x-auto rounded-lg border border-veil/[0.06]">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                  <th className="px-3 py-2 font-medium">Date</th>
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 font-medium">Action</th>
                  <th className="px-3 py-2 text-right font-medium">Qty</th>
                  <th className="px-3 py-2 text-right font-medium">Price</th>
                  <th className="px-3 py-2 text-right font-medium">Commission</th>
                  <th className="px-3 py-2 font-medium">Currency</th>
                  <th className="px-3 py-2 font-medium">Duplicate</th>
                </tr>
              </thead>
              <tbody>
                {investmentPreview.preview.map((row: TradePreviewRow, index) => (
                  <tr
                    key={`${row.date}-${row.symbol}-${index}`}
                    className={
                      row.duplicate
                        ? "bg-danger/[0.07] text-muted"
                        : index % 2 === 1
                          ? "bg-veil/[0.02]"
                          : ""
                    }
                  >
                    <td className="whitespace-nowrap px-3 py-1.5 tabular">{row.date}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 font-mono text-xs">{row.symbol}</td>
                    <td className="whitespace-nowrap px-3 py-1.5">{row.action}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right tabular">{row.quantity}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right tabular">{row.price_native}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right tabular">{row.commission}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 font-mono text-xs">{row.currency}</td>
                    <td className="px-3 py-1.5">
                      {row.duplicate ? (
                        included.has(row.id) ? (
                          <Button
                            type="button"
                            variant="accent-soft"
                            size="sm"
                            onClick={() =>
                              setIncluded((s) => {
                                const n = new Set(s);
                                n.delete(row.id);
                                return n;
                              })
                            }
                          >
                            <Check className="h-3 w-3" /> included
                          </Button>
                        ) : (
                          <Button
                            type="button"
                            variant="danger"
                            size="sm"
                            onClick={() => setIncluded((s) => new Set(s).add(row.id))}
                          >
                            duplicate · include anyway
                          </Button>
                        )
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {investmentPreview.unmapped.length > 0 && (
            <div className="mt-3 rounded-lg border border-veil/[0.06] p-3">
              <p className="text-xs font-semibold uppercase tracking-widest text-muted">
                {investmentPreview.unmapped.length} row
                {investmentPreview.unmapped.length === 1 ? "" : "s"} skipped (unknown/malformed)
                {investmentPreview.institution
                  ? ` — not in ${investmentPreview.institution}'s activity vocabulary; nothing was imported for them`
                  : " — extend ACTIVITY_MAP to import them"}
              </p>
              <div role="region" aria-label="Skipped rows table" tabIndex={0} className="mt-2 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                      <th className="px-2 py-1.5 font-medium">Line</th>
                      <th className="px-2 py-1.5 font-medium">Activity</th>
                      <th className="px-2 py-1.5 font-medium">Symbol</th>
                      <th className="px-2 py-1.5 font-medium">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {investmentPreview.unmapped.map((row) => (
                      <tr key={row.line} className="border-b border-veil/[0.04] last:border-0">
                        <td className="whitespace-nowrap px-2 py-1.5 tabular text-muted">{row.line}</td>
                        <td className="px-2 py-1.5">
                          {row.activity_type}
                          {row.activity_sub_type ? ` / ${row.activity_sub_type}` : ""}
                        </td>
                        <td className="whitespace-nowrap px-2 py-1.5 font-mono text-xs">{row.symbol || "—"}</td>
                        <td className="px-2 py-1.5 text-muted">{row.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-3">
            {/* Rule 6: POST /trades/import/{id}/commit — replaced by its reason.
                Discard stays (docs/33 exclusion table). */}
            {serverWriteBlocked ? (
              <ServerSideBlocked capability={commitCapability} id="trade-commit-blocked" />
            ) : (
              <Button
                variant="primary"
                size="lg"
                loading={finish.isPending}
                onClick={() => finish.mutate({ batchId: investmentPreview.batch_id, commit: true })}
                disabled={finish.isPending}
              >
                Commit {investmentPreview.imported} new trade{investmentPreview.imported === 1 ? "" : "s"}
              </Button>
            )}
            <Button
              variant="outline"
              size="lg"
              disabled={finish.isPending}
              onClick={() => finish.mutate({ batchId: investmentPreview.batch_id, commit: false })}
            >
              Discard batch
            </Button>
          </div>
        </motion.div>
      )}

      {!preview && !investmentPreview && (upload.isError || finish.isError) && (
        <div className="mt-3">
          <ErrorPanel
            message={apiErrorMessage(upload.isError ? upload.error : finish.error)}
            onRetry={uploadRetry.onRetry}
            retrying={uploadRetry.retrying}
          />
        </div>
      )}
    </Card>
  );
}

// --- Batch history ------------------------------------------------------------

const BATCH_PAGE_SIZE = 50;

/** Exported for tests (m95UndoDelete) — not routed on its own. */
export function BatchHistory({ onOpenBatch }: { onOpenBatch: (id: number) => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [offset, setOffset] = useState(0);
  const batches = useQuery({
    queryKey: ["imports", "list", offset],
    queryFn: () => listImports({ limit: BATCH_PAGE_SIZE, offset }),
  });

  // M95 P3 (docs/09 §Destructive actions): batches hidden while their revert
  // is pending — row out on click, request sent only after the 5s window.
  const [pendingReverts, setPendingReverts] = useState<Set<number>>(
    () => new Set(),
  );
  const items = (batches.data?.items ?? []).filter(
    (batch) => !pendingReverts.has(batch.id),
  );
  const total = batches.data?.total ?? 0;

  return (
    <Card hover className="p-4">
      <CardTitle>Batches</CardTitle>
      <HelpHint text="Imports are staged first and only counted after you commit the batch. Re-importing the same file will not create duplicates." />
      {batches.isPending && <SkeletonRows rows={4} />}
      {batches.isError && <QueryErrorPanel query={batches} />}
      {!batches.isPending && !batches.isError && total === 0 && (
        <EmptyState
          icon={<Inbox />}
          title="No imports yet"
          hint="Upload your first statement above — it lands here marked Review until you commit it."
          preview={{
            src: "/help/imports.png",
            alt: "The Imports page with a committed batch in the history list",
          }}
          action={{ label: "Import a statement", to: "/imports" }}
        />
      )}
      <ul className="divide-y divide-veil/[0.05] text-sm">
        {items.map((batch) => (
          <li
            key={batch.id}
            className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2 transition-colors hover:bg-veil/[0.02]"
          >
            <StatusBadge status={batch.status} />
            <span className="font-medium">{batch.file_name}</span>
            <span className="text-muted">{batch.institution || `account #${batch.account_id}`}</span>
            <span className="tabular text-muted">
              {batch.imported} imported · {batch.duplicates_skipped} duplicates ·{" "}
              {batch.unresolved} unresolved
            </span>
            {batch.status === "pending_review" && (
              <Button
                variant="primary"
                size="sm"
                disabled={false}
                onClick={() => onOpenBatch(batch.id)}
              >
                Open
              </Button>
            )}
            {batch.status === "committed" && (
              <Button
                variant="danger"
                size="sm"
                onClick={() =>
                  deferDelete(toast, {
                    title: "Reverted import batch",
                    detail: `${batch.file_name} — ${batch.imported} transactions`,
                    hide: () =>
                      setPendingReverts((prev) => {
                        const next = new Set(prev);
                        next.add(batch.id);
                        return next;
                      }),
                    restore: () =>
                      setPendingReverts((prev) => {
                        const next = new Set(prev);
                        next.delete(batch.id);
                        return next;
                      }),
                    commit: () => revertBatch(batch.id),
                    onCommitted: () => {
                      void queryClient.invalidateQueries({ queryKey: ["imports"] });
                      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
                      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
                    },
                    onError: (message) => toast.error("Revert failed", message),
                  })
                }
              >
                Revert
              </Button>
            )}
          </li>
        ))}
      </ul>
      {total > BATCH_PAGE_SIZE && (
        <div className="mt-3 flex items-center justify-between text-sm text-muted">
          <span className="tabular">
            Showing {offset + 1}–{Math.min(offset + BATCH_PAGE_SIZE, total)} of {total}
          </span>
          <span className="flex gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={offset === 0 || batches.isPending}
              onClick={() => setOffset(Math.max(0, offset - BATCH_PAGE_SIZE))}
            >
              Previous
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={offset + BATCH_PAGE_SIZE >= total || batches.isPending}
              onClick={() => setOffset(offset + BATCH_PAGE_SIZE)}
            >
              Next
            </Button>
          </span>
        </div>
      )}
    </Card>
  );
}

// --- Institutions + CSV profiles ----------------------------------------------

/** CSV profile mode: one signed amount column vs separate debit/credit columns. */
type ProfileMode = "signed" | "split";

/** Editable draft of one institution's CSV importer profile (docs/03). */
interface ProfileDraft {
  mode: ProfileMode;
  dateColumn: string;
  amountColumn: string;
  debitColumn: string;
  creditColumn: string;
  descriptionColumn: string;
  postingDateColumn: string;
  currencyColumn: string;
  dateFormat: string;
  decimalComma: boolean;
  creditsPositive: boolean;
  /** When true, column values are 1-based indices; the parser reads no header row. */
  headerless: boolean;
  signs: ProfileSigns;
  /** True once the user touches a sign chip OR the profile already carries
   * `signs` — only then is the key written back (contract pin 4). */
  signsTouched: boolean;
}

function draftFromProfile(profile: ImporterProfile | null): ProfileDraft {
  const columns = profile?.columns ?? {};
  const hasSplit = Boolean(columns.debit && columns.credit);
  return {
    // Split profiles round-trip as split; anything else edits as signed.
    mode: hasSplit ? "split" : "signed",
    dateColumn: columns.date ?? "",
    amountColumn: columns.amount_native ?? "",
    debitColumn: columns.debit ?? "",
    creditColumn: columns.credit ?? "",
    descriptionColumn: columns.raw_description ?? "",
    postingDateColumn: columns.posting_date ?? "",
    currencyColumn: columns.currency ?? "",
    dateFormat: typeof profile?.date_format === "string" ? profile.date_format : "%Y-%m-%d",
    decimalComma: profile?.decimal_comma === true,
    creditsPositive: profile?.credits_positive === true,
    headerless: profile?.headerless === true,
    signs: {
      debit: profile?.signs?.debit === 1 ? 1 : -1,
      credit: profile?.signs?.credit === -1 ? -1 : 1,
    },
    signsTouched: profile?.signs !== undefined,
  };
}

/** Optional column key → draft field; blank input means "unmapped". */
function applyOptionalColumns(
  columns: Record<string, string>,
  postingDateColumn: string,
  currencyColumn: string,
): Record<string, string> {
  if (postingDateColumn.trim()) columns.posting_date = postingDateColumn.trim();
  if (currencyColumn.trim()) columns.currency = currencyColumn.trim();
  return columns;
}

function toPayload(draft: ProfileDraft): ImporterProfile {
  if (draft.mode === "split") {
    const payload: ImporterProfile = {
      columns: applyOptionalColumns(
        {
          date: draft.dateColumn.trim(),
          raw_description: draft.descriptionColumn.trim(),
          debit: draft.debitColumn.trim(),
          credit: draft.creditColumn.trim(),
        },
        draft.postingDateColumn,
        draft.currencyColumn,
      ),
      date_format: draft.dateFormat,
      decimal_comma: draft.decimalComma,
      headerless: draft.headerless,
    };
    // Contract pin 4: signs only enter the payload once a chip was toggled.
    if (draft.signsTouched) payload.signs = { ...draft.signs };
    return payload;
  }
  return {
    columns: applyOptionalColumns(
      {
        date: draft.dateColumn.trim(),
        amount_native: draft.amountColumn.trim(),
        raw_description: draft.descriptionColumn.trim(),
      },
      draft.postingDateColumn,
      draft.currencyColumn,
    ),
    date_format: draft.dateFormat,
    decimal_comma: draft.decimalComma,
    credits_positive: draft.creditsPositive,
    headerless: draft.headerless,
  };
}

/** −/+ toggle bound to one split-mode column's configured sign (docs/03 v2). */
function SignChip({ label, value, onToggle }: {
  label: string;
  value: -1 | 1;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={`Toggle ${label} sign`}
      title={`${label}: imported ${value === -1 ? "negative" : "positive"} — click to flip`}
      className="field-ring rounded border border-control bg-surface px-1.5 text-[11px] leading-4 text-text transition-colors hover:border-accent/70"
    >
      {value === -1 ? "\u2212" : "+"}
    </button>
  );
}


function ProfileEditor({
  institution,
  onSaved,
}: {
  institution: Institution;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<ProfileDraft>(() =>
    draftFromProfile(institution.importer_profile),
  );
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => updateInstitution(institution.id, { importer_profile: toPayload(draft) }),
    onSuccess: () => {
      setError(null);
      toast.success("CSV profile saved", institution.name);
      onSaved();
    },
    onError: (err) => {
      setError(apiErrorMessage(err));
      toast.error("Profile save failed", apiErrorMessage(err));
    },
  });

  // Client-side pre-check for the friendliest message; the server re-validates.
  const required =
    draft.mode === "split"
      ? ["date", "raw_description", "debit", "credit"]
      : ["date", "amount_native", "raw_description"];
  const values: Record<string, string> = {
    date: draft.dateColumn,
    amount_native: draft.amountColumn,
    raw_description: draft.descriptionColumn,
    debit: draft.debitColumn,
    credit: draft.creditColumn,
  };
  const missing = required.filter((key) => !values[key].trim());
  const headerlessInvalid =
    draft.headerless &&
    required.some((key) => values[key].trim() && !/^\d+$/.test(values[key].trim()));

  return (
    <form
      className="mt-2 grid gap-2 rounded-lg border border-veil/[0.06] bg-surface p-3 sm:grid-cols-2"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <div className="flex items-center gap-3 sm:col-span-2">
        <span className="text-xs text-muted">Amount columns</span>
        <SegmentedControl<ProfileMode>
          layoutId="csv-profile-mode"
          size="sm"
          ariaLabel="CSV profile mode"
          options={[
            { value: "signed", label: "Single amount", title: "One signed amount column" },
            { value: "split", label: "Debit & Credit", title: "Separate debit and credit columns" },
          ]}
          value={draft.mode}
          onChange={(mode) => {
            setError(null);
            setDraft({ ...draft, mode });
          }}
        />
      </div>
      <label className="flex cursor-pointer items-center gap-2 text-xs text-muted sm:col-span-2">
        <input
          type="checkbox"
          checked={draft.headerless}
          onChange={(e) => {
            setError(null);
            setDraft({ ...draft, headerless: e.target.checked });
          }}
          className="size-3.5 accent-[rgb(var(--color-accent))]"
        />
        File has no header row (map columns by position)
      </label>
      <label className="text-xs text-muted">
        Date column name
        <input
          value={draft.dateColumn}
          onChange={(e) => setDraft({ ...draft, dateColumn: e.target.value })}
          placeholder={draft.headerless ? "Column number (1-based)" : "Trans. Date"}
          className={`${INPUT_CLASS} mt-1 block w-full`}
        />
      </label>
      {draft.mode === "signed" ? (
        <label className="text-xs text-muted">
          Amount column name
          <input
            value={draft.amountColumn}
            onChange={(e) => setDraft({ ...draft, amountColumn: e.target.value })}
            placeholder={draft.headerless ? "Column number (1-based)" : "Amount"}
            className={`${INPUT_CLASS} mt-1 block w-full`}
          />
        </label>
      ) : (
        <>
          <label className="text-xs text-muted">
            <span className="flex items-center gap-1.5">
              Debit column name (money out)
              <SignChip
                label="debit"
                value={draft.signs.debit}
                onToggle={() =>
                  setDraft({
                    ...draft,
                    signsTouched: true,
                    signs: { ...draft.signs, debit: draft.signs.debit === -1 ? 1 : -1 },
                  })
                }
              />
            </span>
            <input
              value={draft.debitColumn}
              onChange={(e) => setDraft({ ...draft, debitColumn: e.target.value })}
              placeholder={draft.headerless ? "Column number (1-based)" : "Withdrawal"}
              className={`${INPUT_CLASS} mt-1 block w-full`}
            />
          </label>
          <label className="text-xs text-muted">
            <span className="flex items-center gap-1.5">
              Credit column name (money in)
              <SignChip
                label="credit"
                value={draft.signs.credit}
                onToggle={() =>
                  setDraft({
                    ...draft,
                    signsTouched: true,
                    signs: { ...draft.signs, credit: draft.signs.credit === 1 ? -1 : 1 },
                  })
                }
              />
            </span>
            <input
              value={draft.creditColumn}
              onChange={(e) => setDraft({ ...draft, creditColumn: e.target.value })}
              placeholder={draft.headerless ? "Column number (1-based)" : "Deposit"}
              className={`${INPUT_CLASS} mt-1 block w-full`}
            />
          </label>
        </>
      )}
      <label className="text-xs text-muted">
        Description column name
        <input
          value={draft.descriptionColumn}
          onChange={(e) => setDraft({ ...draft, descriptionColumn: e.target.value })}
          placeholder={draft.headerless ? "Column number (1-based)" : "Description"}
          className={`${INPUT_CLASS} mt-1 block w-full`}
        />
      </label>
      <label className="text-xs text-muted">
        Posting date column name (optional)
        <input
          value={draft.postingDateColumn}
          onChange={(e) => setDraft({ ...draft, postingDateColumn: e.target.value })}
          placeholder={draft.headerless ? "Column number (1-based)" : "Posted"}
          className={`${INPUT_CLASS} mt-1 block w-full`}
        />
      </label>
      <label className="text-xs text-muted sm:col-span-2">
        Currency column name (optional)
        <input
          value={draft.currencyColumn}
          onChange={(e) => setDraft({ ...draft, currencyColumn: e.target.value })}
          placeholder={draft.headerless ? "Column number (1-based)" : "Currency"}
          className={`${INPUT_CLASS} mt-1 block w-full`}
        />
      </label>
      <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
        <input
          type="checkbox"
          checked={draft.decimalComma}
          onChange={(e) => setDraft({ ...draft, decimalComma: e.target.checked })}
          className="size-3.5 accent-[rgb(var(--color-accent))]"
        />
        Decimal comma ("1.234,56")
      </label>
      {draft.mode === "signed" && (
        <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            checked={draft.creditsPositive}
            onChange={(e) => setDraft({ ...draft, creditsPositive: e.target.checked })}
            className="size-3.5 accent-[rgb(var(--color-accent))]"
          />
          Credits positive (debits negative in file)
        </label>
      )}
      {missing.length > 0 && (
        <p className="text-xs text-warn sm:col-span-2">
          columns must map at least: {missing.join(", ")}
        </p>
      )}
      {headerlessInvalid && (
        <p className="text-xs text-warn sm:col-span-2">
          Headerless profiles require 1-based column numbers for every mapped column.
        </p>
      )}
      {missing.length === 0 && error !== null && (
        <p className="text-xs text-danger sm:col-span-2">{error}</p>
      )}
      <div className="sm:col-span-2">
        <Button
          type="submit"
          variant="primary"
          size="sm"
          loading={save.isPending}
          disabled={missing.length > 0 || headerlessInvalid}
        >
          Save profile
        </Button>
      </div>
    </form>
  );
}

function InstitutionsEditor() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const institutions = useQuery({
    queryKey: ["institutions"],
    queryFn: () => listInstitutions(200),
  });

  const [name, setName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ["institutions"] });
  }

  const create = useMutation({
    mutationFn: () => createInstitution({ name: name.trim() }),
    onSuccess: () => {
      setName("");
      setCreateError(null);
      invalidate();
      toast.success("Institution added");
    },
    onError: (err) => {
      setCreateError(apiErrorMessage(err));
      toast.error("Could not add institution", apiErrorMessage(err));
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteInstitution(id),
    onSuccess: () => {
      invalidate();
      toast.info("Institution deleted");
    },
    onError: (err) => toast.error("Delete failed", apiErrorMessage(err)),
  });

  function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <Card hover className="p-4">
      <CardTitle>Institutions</CardTitle>
      <p className="-mt-1 mb-3 text-sm text-muted">
        A CSV profile maps statement columns for the generic-CSV importer.
      </p>

      <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted">
          New institution
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Big Bank"
            className={`${INPUT_CLASS} mt-1 block w-56`}
          />
        </label>
        <Button
          type="submit"
          variant="primary"
          loading={create.isPending}
          disabled={name.trim() === ""}
        >
          Add
        </Button>
      </form>
      {createError !== null && (
        <p className="mt-2 text-sm text-danger">{createError}</p>
      )}

      {institutions.isPending && (
        <div className="mt-3">
          <SkeletonRows rows={2} />
        </div>
      )}
      {institutions.isError && (
        <div className="mt-3">
          <QueryErrorPanel query={institutions} />
        </div>
      )}

      <ul className="mt-3 space-y-2">
        {(institutions.data?.items ?? []).map((institution) => (
          <li key={institution.id}>
            <div className="rounded-lg border border-veil/[0.06] p-3 text-sm transition-colors hover:border-veil/[0.12]">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">{institution.name}</span>
                <span className="flex gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setOpenId(openId === institution.id ? null : institution.id)}
                  >
                    {openId === institution.id ? "Close profile" : "Edit CSV profile"}
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    loading={remove.isPending}
                    onClick={() => {
                      if (window.confirm(`Delete institution "${institution.name}"?`)) {
                        remove.mutate(institution.id);
                      }
                    }}
                  >
                    Delete
                  </Button>
                </span>
              </div>
              {openId === institution.id && (
                <ProfileEditor
                  institution={institution}
                  onSaved={() => {
                    invalidate();
                    setOpenId(null);
                  }}
                />
              )}
            </div>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function Imports() {
  const toast = useToast();
  const [preview, setPreview] = useState<UploadResponse | null>(null);
  const [included, setIncluded] = useState<Set<number>>(new Set());
  const [mappingWizard, setMappingWizard] = useState<{
    headers: { label: string; value: string }[];
    profile: SuggestProfileResponse;
  } | null>(null);

  const openBatch = useMutation({
    mutationFn: async (id: number) => {
      const [batch, rows] = await Promise.all([getBatch(id), getBatchRows(id)]);
      return { batch, rows };
    },
    onSuccess: ({ batch, rows }) => {
      setPreview({ ...batch, preview: rows } as UploadResponse);
      setIncluded(new Set());
      setMappingWizard(null);
    },
    onError: (err: Error) => toast.error("Could not open batch", apiErrorMessage(err)),
  });

  return (
    <section className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Imports</h1>
        <p className="mt-1 text-sm text-muted">
          Statements land in a batch marked Review; committing it makes its rows live.{" "}
          <Link to="/help/import-guides" className="text-accent hover:underline">
            Per-bank import guides
          </Link>
        </p>
      </div>
      <UploadWizard
        preview={preview}
        setPreview={setPreview}
        included={included}
        setIncluded={setIncluded}
        mappingWizard={mappingWizard}
        setMappingWizard={setMappingWizard}
      />
      <YnabMigrationCard />
      <NetWorthImportCard />
      <BatchHistory onOpenBatch={(id: number) => openBatch.mutate(id)} />
      <InstitutionsEditor />
    </section>
  );
}
