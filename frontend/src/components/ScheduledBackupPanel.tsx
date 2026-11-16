import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, History, RotateCcw, Trash2, Zap } from "lucide-react";
import {
  ARTIFACTS_PATH,
  SCHEDULE_PATH,
  deleteArtifact,
  downloadArtifact,
  getArtifact,
  getSchedule,
  listArtifacts,
  putSchedule,
  restoreBackup,
  uploadArtifact,
} from "../api/backup";
import type { BackupArtifact, BackupSchedule } from "../api/backup";
import { getFullExport } from "../api/export";
import { decryptArtifact, readWireFile } from "../lib/backupArtifact";
import { runScheduledBackup, TICK_MS } from "../lib/scheduledBackup";
import type { ScheduledBackupDeps } from "../lib/scheduledBackup";
import { getDEK, isUnlocked } from "../lib/vault";
import { VaultLocked, VAULT_LOCK_COPY, VAULT_LOCK_DESTINATION } from "./e2ee/VaultLocked";
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
 * Scheduled backup & restore (M56, docs/11 § "Scheduled backup & restore").
 *
 * Two surfaces in one file: the Settings panel (schedule form, artifact list,
 * restore) and the runner the AppShell mounts (ticks while the app is open —
 * encryption is client-side, so a run needs an open tab).
 *
 * Honest copy (docs/33 wording rule): the artifact is encrypted in the
 * browser under the vault DEK, and the status line below never claims more
 * than that — the same rows sit in plaintext columns served by the API.
 */

const FIELD =
  "field-ring w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors placeholder:text-muted hover:border-accent/70";
const SELECT_FIELD = `${FIELD} cursor-pointer`;

export const BACKUP_SCHEDULE_QUERY = ["backupSchedule"] as const;
export const BACKUP_ARTIFACTS_QUERY = ["backupArtifacts"] as const;

/** docs/47 §4 (F-2/F-4): the visible reasons bound to the two locked controls. */
export const BACKUP_SCHEDULE_REASON_ID = "backup-schedule-demo-reason";
export const BACKUP_SCHEDULE_REASON =
  "The demo account is read-only — saving this schedule would write it.";
export const BACKUP_NOW_REASON_ID = "backup-now-demo-reason";
export const BACKUP_NOW_REASON =
  "The demo account is read-only — Back up now would write an artifact.";

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
const FREQUENCY_LABELS: Record<BackupSchedule["frequency"], string> = {
  off: "Off",
  daily: "Daily",
  weekly: "Weekly",
  monthly: "Monthly",
};

/** `1.2 MB` / `480 KB` for the artifact list. */
export function formatArtifactSize(chars: number): string {
  if (chars >= 1_000_000) return `${(chars / 1_000_000).toFixed(1)} MB`;
  if (chars >= 1_000) return `${Math.round(chars / 1_000)} KB`;
  return `${chars} B`;
}

/** Real wiring: api helpers + the session DEK (null while locked). */
export function backupDeps(): ScheduledBackupDeps {
  return {
    now: () => new Date(),
    getSchedule,
    listArtifacts,
    exportSnapshot: getFullExport,
    getDEK: () => (isUnlocked() ? getDEK() : null),
    upload: uploadArtifact,
  };
}

/**
 * The runner (docs/11 due semantics): mounts with the shell, ticks on mount
 * and every `TICK_MS`. Silent by design — a failed or locked run writes no
 * artifact and is retried on the next tick; the panel's artifact list is the
 * visible record of what actually succeeded.
 */
export function ScheduledBackupRunner() {
  const queryClient = useQueryClient();

  useEffect(() => {
    let stopped = false;
    const tick = async (): Promise<void> => {
      if (stopped) return;
      try {
        const outcome = await runScheduledBackup(backupDeps());
        if (outcome === "uploaded") {
          void queryClient.invalidateQueries({ queryKey: BACKUP_ARTIFACTS_QUERY });
        }
      } catch {
        // The runner must never break the shell; the next tick retries.
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), TICK_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [queryClient]);

  return null;
}

/** One pending restore: the decrypted payload plus where it came from. */
interface PendingRestore {
  label: string;
  payload: Awaited<ReturnType<typeof decryptArtifact>>;
}

export function ScheduledBackupPanel() {
  const toast = useToast();
  const queryClient = useQueryClient();
  // docs/47 §13 ex.4 (F-2): BOTH writes in this panel are genuinely blocked
  // demo writes — each control wears the padlock, stays enabled, and routes
  // its REAL 403 through the one refusal element instead of the error path.
  // `Back up now` shows its padlock only while it is enabled: a vault-locked
  // control is disabled with `VaultLocked`'s own reason (docs/47 §4 — E2EE
  // unavailability never borrows the demo lock).
  const scheduleLocked = useDemoWriteLock("PUT", SCHEDULE_PATH);
  const nowLocked = useDemoWriteLock("POST", ARTIFACTS_PATH);
  const demo = useDemoRefusal();
  const scheduleQuery = useQuery({ queryKey: BACKUP_SCHEDULE_QUERY, queryFn: getSchedule });
  const artifactsQuery = useQuery({
    queryKey: BACKUP_ARTIFACTS_QUERY,
    queryFn: listArtifacts,
    refetchInterval: 60_000,
  });

  const [draft, setDraft] = useState<BackupSchedule | null>(null);
  const [error, setError] = useState<string | null>(null);
  // M136: the shell-level subscription, not a private copy — the vault
  // dispatches `vaultchange` on lock/unlock and every surface now follows it
  // through the same hook.
  const unlocked = useVaultUnlocked();
  const [pending, setPending] = useState<PendingRestore | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const [busyId, setBusyId] = useState<number | "now" | null>(null);

  useEffect(() => {
    if (scheduleQuery.data !== undefined && draft === null) setDraft(scheduleQuery.data);
  }, [scheduleQuery.data, draft]);

  const save = useMutation({
    mutationFn: (next: BackupSchedule) => putSchedule(next),
    onSuccess: (saved) => {
      setDraft(saved);
      setError(null);
      queryClient.setQueryData(BACKUP_SCHEDULE_QUERY, saved);
      toast.success("Backup schedule saved");
    },
    onError: (err) => {
      // docs/47 §4: a demo refusal is not an error (F-2) — one `toast.info`
      // + the local `role="status"` element, nothing red, nothing `alert`.
      if (demo.onError("Save backup schedule", err)) {
        setError(null);
        return;
      }
      setError(apiErrorMessage(err));
      toast.error("Could not save schedule", apiErrorMessage(err));
    },
  });

  async function backupNow(): Promise<void> {
    setBusyId("now");
    setError(null);
    try {
      const outcome = await runScheduledBackup(backupDeps(), { force: true });
      await queryClient.invalidateQueries({ queryKey: BACKUP_ARTIFACTS_QUERY });
      if (outcome === "uploaded") {
        toast.success("Backup saved", "Encrypted in this browser, stored on the server.");
      } else if (outcome === "vault_locked") {
        toast.error("Vault is locked", VAULT_LOCK_COPY.client + " " + VAULT_LOCK_DESTINATION);
      } else if (outcome === "too_large") {
        toast.error("Backup too large", "The artifact is over the 8 MB upload limit.");
      } else {
        toast.error("Backup failed", "Nothing was stored — try again.");
      }
    } catch (err) {
      // The upload's 403 is a demo refusal, not a failure (F-2).
      if (demo.onError("Back up now", err)) return;
      toast.error("Backup failed", apiErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  }

  async function startRestoreFromArtifact(artifact: BackupArtifact): Promise<void> {
    setBusyId(artifact.id);
    setError(null);
    try {
      const detail = await getArtifact(artifact.id);
      const dek = getDEK(); // throws when locked — guarded by the UI state
      setPending({ label: `artifact ${artifact.period}`, payload: await decryptArtifact(detail.ciphertext, dek) });
      setConfirmText("");
    } catch (err) {
      const message = apiErrorMessage(err);
      setError(message);
      toast.error("Could not open backup", message);
    } finally {
      setBusyId(null);
    }
  }

  async function startRestoreFromFile(file: File): Promise<void> {
    setError(null);
    try {
      const wire = await readWireFile(file);
      const dek = getDEK();
      setPending({ label: `file ${file.name}`, payload: await decryptArtifact(wire.ciphertext, dek) });
      setConfirmText("");
    } catch (err) {
      const message = apiErrorMessage(err);
      setError(message);
      toast.error("Could not read backup file", message);
    }
  }

  const restore = useMutation({
    mutationFn: () => {
      if (pending === null) throw new Error("Nothing to restore");
      return restoreBackup(pending.payload);
    },
    onSuccess: (result) => {
      const tables = Object.keys(result.restored).length;
      setPending(null);
      setConfirmText("");
      void queryClient.invalidateQueries();
      toast.success(
        `Restored ${tables} tables`,
        result.absent_tables.length > 0
          ? `${result.absent_tables.length} table(s) had no rows in the backup.`
          : undefined,
      );
    },
    onError: (err) => {
      setError(apiErrorMessage(err));
      toast.error("Restore rejected", apiErrorMessage(err));
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteArtifact(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: BACKUP_ARTIFACTS_QUERY });
      toast.info("Backup deleted", "Deletion is permanent — there is no recycle bin.");
    },
    onError: (err) => toast.error("Could not delete", apiErrorMessage(err)),
  });

  async function download(artifact: BackupArtifact): Promise<void> {
    try {
      const detail = await getArtifact(artifact.id);
      const name = downloadArtifact(detail);
      toast.success("Backup downloaded", `${name} — still encrypted.`);
    } catch (err) {
      toast.error("Download failed", apiErrorMessage(err));
    }
  }

  if (scheduleQuery.isPending) {
    return (
      <div className="space-y-2 border-t border-veil/[0.06] pt-4">
        <h3 className="text-sm font-medium">Scheduled backup</h3>
        <SkeletonRows rows={2} />
      </div>
    );
  }
  if (scheduleQuery.isError || draft === null) {
    return (
      <div className="space-y-2 border-t border-veil/[0.06] pt-4">
        <h3 className="text-sm font-medium">Scheduled backup</h3>
        <QueryErrorPanel query={scheduleQuery} message={scheduleQuery.error?.message ?? "Not loaded"} />
      </div>
    );
  }

  const items = artifactsQuery.data?.items ?? [];
  const lastBackup = items[0];

  return (
    <div className="space-y-3 border-t border-veil/[0.06] pt-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium">Scheduled backup</h3>
        <Badge tone={draft.frequency === "off" ? "neutral" : "accent"}>
          {FREQUENCY_LABELS[draft.frequency]}
        </Badge>
      </div>

      <p className="text-xs leading-relaxed text-muted">
        Encrypted in this browser under your vault key before it is uploaded — the server stores
        ciphertext it cannot open, plus the schedule and size.{" "}
        <span className="text-muted">
          Client-side encryption in progress — the server can still read your data.
        </span>{" "}
        A run needs an open tab with the vault unlocked.
      </p>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <label className="col-span-2 flex flex-col gap-1 text-xs text-muted">
          Frequency
          <select
            value={draft.frequency}
            onChange={(e) =>
              setDraft({ ...draft, frequency: e.target.value as BackupSchedule["frequency"] })
            }
            className={SELECT_FIELD}
            aria-label="Backup frequency"
          >
            {(Object.keys(FREQUENCY_LABELS) as BackupSchedule["frequency"][]).map((value) => (
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
            aria-label="Backup hour"
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
            aria-label="Backup weekday"
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
            aria-label="Backup day of month"
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
        Keep last N backups (1–20)
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
          // M92 F-M91-4: Settings carries TWO schedule forms, so both buttons
          // must be addressable by a unique name (docs/09 naming contract) —
          // a first-match click on "Save schedule" silently saved the wrong
          // form. The aria-label matches the visible text (WCAG 2.5.3).
          aria-label="Save backup schedule"
          aria-describedby={scheduleLocked ? BACKUP_SCHEDULE_REASON_ID : undefined}
          onClick={() => save.mutate(draft)}
        >
          {scheduleLocked && <DemoLock />}
          Save backup schedule
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          loading={busyId === "now"}
          disabled={!unlocked}
          // Padlock only while the control can act: a vault-locked button is
          // disabled with VaultLocked's reason and never wears a lock (§4).
          aria-describedby={unlocked && nowLocked ? BACKUP_NOW_REASON_ID : undefined}
          onClick={() => void backupNow()}
        >
          {unlocked && nowLocked && <DemoLock />}
          <Zap className="size-3.5" aria-hidden /> Back up now
        </Button>
        {/* docs/33 rule 7g: this is the FOURTH lock phrase the tree had
            ("Vault locked — unlock to back up or restore", and the toast
            below's "Unlock it in Encryption above"). One component, one
            string, and the disabled buttons beside it now say WHY. */}
        {!unlocked && <VaultLocked variant="blocked" capability="backup:sched" />}
      </div>

      {/* docs/47 §4: the VISIBLE reasons the padlocks above bind to (F-4). */}
      {scheduleLocked && (
        <DemoReason id={BACKUP_SCHEDULE_REASON_ID}>{BACKUP_SCHEDULE_REASON}</DemoReason>
      )}
      {unlocked && nowLocked && (
        <DemoReason id={BACKUP_NOW_REASON_ID}>{BACKUP_NOW_REASON}</DemoReason>
      )}
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}

      <p className="text-xs text-muted tabular">
        {lastBackup === undefined
          ? "No scheduled backup yet."
          : `Last backup: ${lastBackup.created_at === null ? "—" : new Date(lastBackup.created_at).toLocaleString()}`}
      </p>

      {error !== null && <p className="text-sm text-danger">{error}</p>}

      {/* Artifact list — metadata only until an action fetches one row. */}
      <div className="space-y-2 border-t border-veil/[0.06] pt-3">
        <div className="flex items-center gap-2">
          <History className="size-3.5 text-muted" aria-hidden />
          <h4 className="text-xs font-semibold uppercase tracking-widest text-muted">
            Backups ({items.length}/{draft.keep_last_n})
          </h4>
        </div>
        {artifactsQuery.isPending && <SkeletonRows rows={2} />}
        {artifactsQuery.isError && (
          <QueryErrorPanel query={artifactsQuery} message={artifactsQuery.error?.message ?? "Not loaded"} />
        )}
        {artifactsQuery.data !== undefined && items.length === 0 && (
          <p className="text-xs text-muted">
            Nothing stored yet. The first run happens as soon as the schedule is on and the vault
            is unlocked.
          </p>
        )}
        <ul className="space-y-1">
          {items.map((artifact) => (
            <li
              key={artifact.id}
              className="flex flex-wrap items-center gap-2 rounded-lg border border-veil/[0.06] px-2 py-1.5 text-xs"
            >
              <span className="font-medium tabular">{artifact.period}</span>
              <span className="text-muted tabular">
                {artifact.created_at === null ? "—" : new Date(artifact.created_at).toLocaleString()}
              </span>
              <span className="text-muted tabular">{formatArtifactSize(artifact.size_bytes)}</span>
              <span className="ml-auto flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => void download(artifact)}
                  aria-label={`Download backup ${artifact.period}`}
                >
                  <Download className="size-3.5" aria-hidden />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  loading={busyId === artifact.id}
                  disabled={!unlocked}
                  onClick={() => void startRestoreFromArtifact(artifact)}
                  aria-label={`Restore backup ${artifact.period}`}
                >
                  <RotateCcw className="size-3.5" aria-hidden />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  loading={remove.isPending && remove.variables === artifact.id}
                  onClick={() => remove.mutate(artifact.id)}
                  aria-label={`Delete backup ${artifact.period}`}
                >
                  <Trash2 className="size-3.5" aria-hidden />
                </Button>
              </span>
            </li>
          ))}
        </ul>
        <label className="block text-xs text-muted">
          Restore from a downloaded file (.ftbackup.json)
          <input
            type="file"
            accept=".json,.ftbackup.json,application/json"
            disabled={!unlocked}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void startRestoreFromFile(file);
              e.target.value = "";
            }}
            className="mt-1 block w-full text-xs file:mr-2 file:rounded-md file:border file:border-border file:bg-surface-raised file:px-2 file:py-1 file:text-xs file:text-text"
          />
        </label>
      </div>

      {/* Restore confirmation — the typed literal the API requires. */}
      {pending !== null && (
        <div className="space-y-2 rounded-lg border border-danger/30 bg-danger/5 p-3">
          <p className="text-xs leading-relaxed text-text">
            Restore from <span className="font-medium">{pending.label}</span>? This{" "}
            <span className="font-semibold text-danger">replaces every domain row</span> in this
            account with the backup&apos;s contents ({Object.keys(pending.payload.snapshot).length} tables).
            Your login, devices and MCP tokens are kept. Backups, credentials and other accounts are
            untouched. Type <span className="font-mono">restore</span> to confirm.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder="restore"
              aria-label='Type "restore" to confirm'
              className={`${FIELD} max-w-[10rem] font-mono`}
            />
            <Button
              type="button"
              variant="primary"
              size="sm"
              loading={restore.isPending}
              disabled={confirmText !== "restore"}
              onClick={() => restore.mutate()}
            >
              Restore now
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setPending(null);
                setConfirmText("");
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
