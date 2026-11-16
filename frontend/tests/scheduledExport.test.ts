// @vitest-environment node
// Scheduled export — docs/11 § "Due semantics" (M75).
//
// Same contract style as scheduledBackup.test.ts, one difference that the
// whole feature turns on: a locked vault does NOT block a run (the server
// exports `E2EE:<ciphertext>` for cipher-backed fields), and a successful run
// writes LOCALLY — nothing is uploaded. Everything is clock-injected, so the
// period label, the due predicate and the outcome machine are all pinned
// here without a server, a browser or a timer.
import { describe, expect, it } from "vitest";
import type { ExportSchedule, VaultState } from "../src/api/export";
import type { StoredExportMeta } from "../src/lib/localExports";
import {
  EXPORT_TICK_MS,
  runScheduledExport,
  type NewStoredExport,
  type ScheduledExportDeps,
} from "../src/lib/scheduledExport";

const OFF: ExportSchedule = {
  frequency: "off",
  hour: 3,
  weekday: 0,
  day_of_month: 1,
  keep_last_n: 5,
};
const DAILY: ExportSchedule = { ...OFF, frequency: "daily" };

/** Monday 2026-09-28 at 03:00 LOCAL — `hour: 3` is due exactly then. */
const NOW = new Date(2026, 8, 28, 3, 0, 0);

function meta(period: string, createdAt: Date): StoredExportMeta {
  return {
    period,
    createdAt: createdAt.toISOString(),
    sizeBytes: 1024,
    filename: `fin-tracker-export-${period}.zip`,
  };
}

interface Harness {
  deps: ScheduledExportDeps;
  stored: NewStoredExport[];
  keepLastNs: number[];
  exportCalls: VaultState[];
}

function harness(
  init: {
    schedule?: ExportSchedule;
    items?: StoredExportMeta[];
    failExport?: boolean;
    failStore?: boolean;
    vault?: VaultState;
    now?: Date;
  } = {},
): Harness {
  const stored: NewStoredExport[] = [];
  const keepLastNs: number[] = [];
  const exportCalls: VaultState[] = [];
  const deps: ScheduledExportDeps = {
    now: () => init.now ?? NOW,
    getSchedule: async () => init.schedule ?? DAILY,
    listExports: async () => init.items ?? [],
    exportZip: async (vault) => {
      exportCalls.push(vault);
      if (init.failExport) throw new Error("GET /export/zip 500");
      return new Blob(["zip-bytes"], { type: "application/zip" });
    },
    storeExport: async (record, keepLastN) => {
      if (init.failStore) throw new Error("IndexedDB write failed");
      stored.push(record);
      keepLastNs.push(keepLastN);
    },
    vaultState: () => init.vault ?? "locked",
  };
  return { deps, stored, keepLastNs, exportCalls };
}

describe("scheduled export — outcome machine", () => {
  it("ticks every 5 minutes (AppShell's runner cadence)", () => {
    expect(EXPORT_TICK_MS).toBe(300_000);
  });

  it("does nothing when the schedule is off — no export call", async () => {
    const h = harness({ schedule: OFF });
    expect(await runScheduledExport(h.deps)).toBe("off");
    expect(h.exportCalls).toHaveLength(0);
    expect(h.stored).toHaveLength(0);
  });

  it("runs on the first tick with no record yet and stores the period key", async () => {
    const h = harness();
    expect(await runScheduledExport(h.deps)).toBe("stored");
    expect(h.exportCalls).toEqual(["locked"]);
    expect(h.stored).toHaveLength(1);
    expect(h.stored[0].period).toBe("2026-09-28"); // daily key, LOCAL date
    expect(h.stored[0].filename).toBe("fin-tracker-export-2026-09-28.zip");
    expect(h.stored[0].createdAt).toBe(NOW.toISOString());
    expect(h.stored[0].sizeBytes).toBeGreaterThan(0);
    expect(h.keepLastNs).toEqual([5]); // keep_last_n travels from the schedule
  });

  it("skips when the current period already ran (no second ZIP per period)", async () => {
    const h = harness({ items: [meta("2026-09-28", NOW)] });
    expect(await runScheduledExport(h.deps)).toBe("skipped");
    expect(h.exportCalls).toHaveLength(0);
  });

  it("runs when the schedule's next due instant has passed", async () => {
    const h = harness({ items: [meta("2026-09-27", new Date(2026, 8, 27, 3, 0, 0))] });
    expect(await runScheduledExport(h.deps)).toBe("stored");
    expect(h.stored[0].period).toBe("2026-09-28");
  });

  it("exports with the session's vault state instead of assuming one", async () => {
    const unlocked = harness({ vault: "unlocked" });
    expect(await runScheduledExport(unlocked.deps)).toBe("stored");
    expect(unlocked.exportCalls).toEqual(["unlocked"]);

    const locked = harness({ vault: "locked" });
    expect(await runScheduledExport(locked.deps)).toBe("stored");
    expect(locked.exportCalls).toEqual(["locked"]);
  });

  it("writes nothing and retries next tick when the export fails", async () => {
    const h = harness({ failExport: true });
    expect(await runScheduledExport(h.deps)).toBe("export_failed");
    expect(h.stored).toHaveLength(0);
  });

  it("reports storage failure distinctly from export failure", async () => {
    const h = harness({ failStore: true });
    expect(await runScheduledExport(h.deps)).toBe("store_failed");
    expect(h.exportCalls).toHaveLength(1); // the ZIP was fetched, only the write failed
  });

  it("force runs even when the period is not due yet (the Export-now button)", async () => {
    const h = harness({ items: [meta("2026-09-28", NOW)] });
    expect(await runScheduledExport(h.deps, { force: true })).toBe("stored");
    expect(h.exportCalls).toEqual(["locked"]);
  });

  it("labels weekly and monthly periods with the documented keys", async () => {
    const weekly = harness({ schedule: { ...OFF, frequency: "weekly" } });
    expect(await runScheduledExport(weekly.deps)).toBe("stored");
    expect(weekly.stored[0].period).toBe("2026-W40"); // ISO week of 2026-09-28

    const monthly = harness({ schedule: { ...OFF, frequency: "monthly" } });
    expect(await runScheduledExport(monthly.deps)).toBe("stored");
    expect(monthly.stored[0].period).toBe("2026-09");
  });
});
