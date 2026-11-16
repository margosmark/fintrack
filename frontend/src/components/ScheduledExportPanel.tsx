import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileArchive, Lock, Trash2, Zap } from "lucide-react";
import {
  EXPORT_SCHEDULE_PATH,
  downloadOfxExport,
  fetchZip,
  getExportSchedule,
  putExportSchedule,
} from "../api/export";
import type { ExportSchedule, VaultState } from "../api/export";
import { listAccounts } from "../api/accounts";
import {
  deleteLocalExport,
  listLocalExports,
  localExportsAvailable,
  putLocalExport,
  readLocalExport,
} from "../lib/localExports";
import { runScheduledExport, EXPORT_TICK_MS } from "../lib/scheduledExport";
import type { ScheduledExportDeps } from "../lib/scheduledExport";
import { isUnlocked } from "../lib/vault";
import { useVaultUnlocked } from "./e2ee/useUnlock";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import { QueryErrorPanel } from "./ui/QueryErrorPanel";
import { SkeletonRows } from "./ui/Skeleton";
import { useToast } from "./ui/Toast";
import {
  DemoLock,
  DemoReason,
  DemoRefusal,
  useDemoRefusal,
  useDemoWriteLock,
} from "./ui/DemoLock";
import { apiErrorMessage } from "./transactions/format";

/**
 * Scheduled export & the one-click exports (M75, docs/11 § "Scheduled
 * export").
 *
 * Three surfaces in one file:
 * - the Settings panel: schedule form, stored archives, Download / Delete;
 * - the runner the AppShell mounts (ticks while the app is open — the ZIP is
 *   built in this browser's session, so a run needs an open tab);
 * - `OfxExportRow`, the per-account OFX download (docs/11 § OFX round trip).
 *
 * Honest copy rule (docs/33 wording): this is an UNENCRYPTED archive stored
 * in this browser's IndexedDB. We say exactly that — never "private" or
 * "secure" — and the vault state only decides whether cipher-backed fields
 * ship as `E2EE:<ciphertext>` or as plaintext.
 */

const FIELD =
  "field-ring w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors placeholder:text-muted hover:border-accent/70";
const SELECT_FIELD = `${FIELD} cursor-pointer`;

export const EXPORT_SCHEDULE_QUERY = ["exportSchedule"] as const;
export const EXPORT_LIST_QUERY = ["exportList"] as const;

/** docs/47 §4 (F-2/F-4): the visible reason bound to the locked Save control. */
export const EXPORT_SCHEDULE_REASON_ID = "export-schedule-demo-reason";
export const EXPORT_SCHEDULE_REASON =
  "The demo account is read-only — saving this schedule would write it.";

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
const FREQUENCY_LABELS: Record<ExportSchedule["frequency"], string> = {
  off: "Off",
  daily: "Daily",
  weekly: "Weekly",
  monthly: "Monthly",
};

/** `1.2 MB` / `480 KB` for the archive list (bytes, same as the backup list). */
export function formatExportSize(bytes: number): string {
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${Math.round(bytes / 1_000)} KB`;
  return `${bytes} B`;
}

/** This session's vault state: `locked` is the server's fail-closed default. */
export function vaultState(): VaultState {
  return isUnlocked() ? "unlocked" : "locked";
}

/**
 * Real wiring: the same `api/export` helpers the one-click buttons use (so
 * the scheduled run and a manual click go through one auth path) + IndexedDB
 * under this user's key.
 */
export function exportDeps(userId: string | number): ScheduledExportDeps {
  const storageUserId = String(userId);
  return {
    now: () => new Date(),
    getSchedule: getExportSchedule,
    listExports: () => listLocalExports(storageUserId),
    exportZip: fetchZip,
    storeExport: (record, keepLastN) => putLocalExport(storageUserId, record, keepLastN),
    vaultState,
  };
}

/**
 * The runner (docs/11 due semantics): mounts with the shell, ticks on mount
 * and every `EXPORT_TICK_MS`. Silent by design — a failed run writes no
 * archive and is retried next tick; the panel's list is the visible record.
 */
export function ScheduledExportRunner({ userId }: { userId: string | number }) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!localExportsAvailable()) return;
    let stopped = false;
    const tick = async (): Promise<void> => {
      if (stopped) return;
      try {
        const outcome = await runScheduledExport(exportDeps(userId));
        if (outcome === "stored") {
          void queryClient.invalidateQueries({ queryKey: EXPORT_LIST_QUERY });
        }
      } catch {
        // The runner must never break the shell; the next tick retries.
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), EXPORT_TICK_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [queryClient, userId]);

  return null;
}

export function ScheduledExportPanel({ userId }: { userId: string | number }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const storageUserId = String(userId);
  const supported = localExportsAvailable();
  // docs/47 §13 ex.4 (F-2): `PUT /export/schedule` is a genuinely blocked
  // demo write — the Save control wears the padlock, stays enabled, and the
  // REAL 403 becomes one refusal element + one `toast.info`, never the
  // error-shaped path (`role="alert"` + a red line).
  const locked = useDemoWriteLock("PUT", EXPORT_SCHEDULE_PATH);
  const demo = useDemoRefusal();
  const scheduleQuery = useQuery({
    queryKey: EXPORT_SCHEDULE_QUERY,
    queryFn: getExportSchedule,
  });

  const [draft, setDraft] = useState<ExportSchedule | null>(null);
  const [error, setError] = useState<string | null>(null);
  // M136 rule 7c: the ONE lock-state subscription (`useVaultUnlocked` follows
  // the `vaultchange` event `lib/vault.ts` dispatches on create / unlock / lock
  // / revoke), not a private copy — the status line re-renders the moment the
  // vault moves, with no polling and no network call.
  const unlocked = useVaultUnlocked();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (scheduleQuery.data !== undefined && draft === null) setDraft(scheduleQuery.data);
  }, [scheduleQuery.data, draft]);

  const save = useMutation({
    mutationFn: (next: ExportSchedule) => putExportSchedule(next),
    onSuccess: (saved) => {
      setDraft(saved);
      setError(null);
      queryClient.setQueryData(EXPORT_SCHEDULE_QUERY, saved);
      toast.success("Export schedule saved");
    },
    onError: (err) => {
      // docs/47 §4: a demo refusal is NOT an error — one `toast.info` + the
      // local `role="status"` element, and nothing red (F-2's measured
      // repro was exactly this path raising `role="alert"`).
      if (demo.onError("Save export schedule", err)) {
        setError(null);
        return;
      }
      setError(apiErrorMessage(err));
      toast.error("Could not save schedule", apiErrorMessage(err));
    },
  });

  const archivesQuery = useQuery({
    queryKey: EXPORT_LIST_QUERY,
    queryFn: () => listLocalExports(storageUserId),
    enabled: supported,
    refetchInterval: 60_000,
  });

  async function exportNow(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const outcome = await runScheduledExport(exportDeps(userId), { force: true });
      await queryClient.invalidateQueries({ queryKey: EXPORT_LIST_QUERY });
      if (outcome === "stored") {
        toast.success("Export stored", "ZIP saved in this browser — download it below.");
      } else {
        toast.error("Export failed", "Nothing was stored — try again.");
      }
    } catch (err) {
      toast.error("Export failed", apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function download(period: string, filename: string): Promise<void> {
    try {
      const blob = await readLocalExport(storageUserId, period);
      if (blob === null) {
        toast.error("Archive missing", "It may have been deleted — export again.");
        return;
      }
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        anchor.click();
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (err) {
      toast.error("Download failed", apiErrorMessage(err));
    }
  }

  const remove = useMutation({
    mutationFn: (period: string) => deleteLocalExport(storageUserId, period),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: EXPORT_LIST_QUERY });
      toast.info("Archive deleted", "Deletion is permanent — there is no recycle bin.");
    },
    onError: (err) => toast.error("Could not delete", apiErrorMessage(err)),
  });

  const archives = archivesQuery.data ?? [];
  const lastExport = archives[0];

  if (scheduleQuery.isPending) {
    return (
      <div className="space-y-2 border-t border-veil/[0.06] pt-4">
        <h3 className="text-sm font-medium">Scheduled export</h3>
        <SkeletonRows rows={2} />
      </div>
    );
  }
  if (scheduleQuery.isError || draft === null) {
    return (
      <div className="space-y-2 border-t border-veil/[0.06] pt-4">
        <h3 className="text-sm font-medium">Scheduled export</h3>
        <QueryErrorPanel query={scheduleQuery} message={scheduleQuery.error?.message ?? "Not loaded"} />
      </div>
    );
  }

  return (
    <div className="space-y-3 border-t border-veil/[0.06] pt-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium">Scheduled export</h3>
        <Badge tone={draft.frequency === "off" ? "neutral" : "accent"}>
          {FREQUENCY_LABELS[draft.frequency]}
        </Badge>
      </div>

      <p className="text-xs leading-relaxed text-muted">
        Builds the full ZIP and keeps it in this browser&apos;s local storage (IndexedDB) under
        keep-last-N. Nothing is uploaded — the archive never leaves this device unless you
        download it. A run needs an open tab; missed periods are skipped, not backfilled.
        {!unlocked && (
          <>
            {" "}
            <span className="text-muted">
              Vault locked: cipher-backed fields export as their E2EE ciphertext, not plaintext.
            </span>
          </>
        )}
      </p>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <label className="col-span-2 flex flex-col gap-1 text-xs text-muted">
          Frequency
          <select
            value={draft.frequency}
            onChange={(e) =>
              setDraft({ ...draft, frequency: e.target.value as ExportSchedule["frequency"] })
            }
            className={SELECT_FIELD}
            aria-label="Export frequency"
          >
            {(Object.keys(FREQUENCY_LABELS) as ExportSchedule["frequency"][]).map((value) => (
              <option key={value} value={value}>
                {FREQUENCY_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Hour
          <select
            value={draft.hour}
            onChange={(e) => setDraft({ ...draft, hour: Number(e.target.value) })}
            className={SELECT_FIELD}
            aria-label="Export hour"
          >
            {Array.from({ length: 24 }, (_, hour) => (
              <option key={hour} value={hour}>
                {String(hour).padStart(2, "0")}:00
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Weekday
          <select
            value={draft.weekday}
            onChange={(e) => setDraft({ ...draft, weekday: Number(e.target.value) })}
            disabled={draft.frequency !== "weekly"}
            className={SELECT_FIELD}
            aria-label="Export weekday"
          >
            {WEEKDAY_LABELS.map((label, index) => (
              <option key={label} value={index}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Day of month
          <select
            value={draft.day_of_month}
            onChange={(e) => setDraft({ ...draft, day_of_month: Number(e.target.value) })}
            disabled={draft.frequency !== "monthly"}
            className={SELECT_FIELD}
            aria-label="Export day of month"
          >
            {Array.from({ length: 28 }, (_, index) => index + 1).map((day) => (
              <option key={day} value={day}>
                {day}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label className="flex flex-col gap-1 text-xs text-muted sm:max-w-[14rem]">
        Keep last N archives (1–20)
        <input
          type="number"
          min={1}
          max={20}
          value={draft.keep_last_n}
          onChange={(e) => setDraft({ ...draft, keep_last_n: Number(e.target.value) })}
          className={FIELD}
        />
      </label>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="primary"
          size="sm"
          loading={save.isPending}
          // M92 F-M91-4: the twin of ScheduledBackupPanel's save control —
          // two identically named "Save schedule" buttons on one Settings
          // page made the first-match click save the wrong form. Unique name
          // here too, matching the visible text (WCAG 2.5.3).
          aria-label="Save export schedule"
          aria-describedby={locked ? EXPORT_SCHEDULE_REASON_ID : undefined}
          onClick={() => save.mutate(draft)}
        >
          {locked && <DemoLock />}
          Save export schedule
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          loading={busy}
          disabled={!supported}
          onClick={() => void exportNow()}
        >
          <Zap className="size-3.5" aria-hidden /> Export now
        </Button>
        {!supported && <span className="text-xs text-muted">Local storage unavailable</span>}
        {!unlocked && (
          <span className="flex items-center gap-1 text-xs text-muted">
            <Lock className="size-3.5" aria-hidden /> Vault locked — ciphertext export
          </span>
        )}
      </div>

      {/* docs/47 §4: the VISIBLE reason the padlock above binds to (F-4) —
          same panel, non-zero box, never sr-only. */}
      {locked && (
        <DemoReason id={EXPORT_SCHEDULE_REASON_ID}>{EXPORT_SCHEDULE_REASON}</DemoReason>
      )}
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}

      <p className="text-xs text-muted tabular">
        {lastExport === undefined
          ? "No scheduled export yet."
          : `Last export: ${new Date(lastExport.createdAt).toLocaleString()}`}
      </p>

      {error !== null && <p className="text-sm text-danger">{error}</p>}

      <div className="space-y-2 border-t border-veil/[0.06] pt-3">
        <div className="flex items-center gap-2">
          <FileArchive className="size-3.5 text-muted" aria-hidden />
          <h4 className="text-xs font-semibold uppercase tracking-widest text-muted">
            Exports ({archives.length}/{draft.keep_last_n})
          </h4>
        </div>
        {supported && archivesQuery.isPending && <SkeletonRows rows={2} />}
        {supported && archivesQuery.isError && (
          <QueryErrorPanel query={archivesQuery} message={archivesQuery.error?.message ?? "Not loaded"} />
        )}
        {supported && archivesQuery.data !== undefined && archives.length === 0 && (
          <p className="text-xs text-muted">
            Nothing stored yet. The first run happens as soon as the schedule is on.
          </p>
        )}
        <ul className="space-y-1">
          {archives.map((archive) => (
            <li
              key={archive.period}
              className="flex flex-wrap items-center gap-2 rounded-lg border border-veil/[0.06] px-2 py-1.5 text-xs"
            >
              <span className="font-medium tabular">{archive.period}</span>
              <span className="text-muted tabular">
                {new Date(archive.createdAt).toLocaleString()}
              </span>
              <span className="text-muted tabular">{formatExportSize(archive.sizeBytes)}</span>
              <span className="ml-auto flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => void download(archive.period, archive.filename)}
                  aria-label={`Download export ${archive.period}`}
                >
                  <Download className="size-3.5" aria-hidden />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  loading={remove.isPending && remove.variables === archive.period}
                  onClick={() => remove.mutate(archive.period)}
                  aria-label={`Delete export ${archive.period}`}
                >
                  <Trash2 className="size-3.5" aria-hidden />
                </Button>
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/**
 * Per-account OFX statement download (docs/11 § "OFX round trip"). Separate
 * from the ZIP because dedup is keyed per account — one statement per
 * account, so a file never mixes two accounts' rows.
 */
export function OfxExportRow() {
  const toast = useToast();
  const [accountId, setAccountId] = useState<string>("");
  const accountsQuery = useQuery({
    queryKey: ["accounts", "forOfx"],
    queryFn: () => listAccounts(200, 0),
    staleTime: 60_000,
  });
  const options = useMemo(
    () => accountsQuery.data?.items ?? [],
    [accountsQuery.data],
  );

  const download = useMutation({
    mutationFn: (id: number) => downloadOfx(id),
    onSuccess: (filename) =>
      toast.success("OFX downloaded", `${filename} — re-imports into this account with 0 new rows.`),
    onError: (err) => toast.error("OFX export failed", apiErrorMessage(err)),
  });

  return (
    <div className="flex flex-wrap items-end gap-2">
      <label className="flex flex-1 flex-col gap-1 text-xs text-muted sm:max-w-[18rem]">
        OFX statement (one account)
        <select
          value={accountId}
          onChange={(e) => setAccountId(e.target.value)}
          className={SELECT_FIELD}
          aria-label="Account for the OFX statement"
          disabled={accountsQuery.isPending || accountsQuery.isError}
        >
          <option value="">
            {accountsQuery.isPending ? "Loading accounts…" : "Choose an account"}
          </option>
          {options.map((account) => (
            <option key={account.id} value={account.id}>
              {account.name}
            </option>
          ))}
        </select>
      </label>
      <Button
        type="button"
        variant="outline"
        size="sm"
        loading={download.isPending}
        disabled={accountId === ""}
        onClick={() => download.mutate(Number(accountId))}
      >
        <Download className="size-3.5" aria-hidden /> Download OFX
      </Button>
      {accountsQuery.isError && (
        <span className="text-xs text-danger">{accountsQuery.error?.message}</span>
      )}
    </div>
  );
}

async function downloadOfx(accountId: number): Promise<string> {
  return downloadOfxExport(accountId, vaultState());
}
