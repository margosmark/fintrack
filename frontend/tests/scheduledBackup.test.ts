// @vitest-environment node
// Scheduled backup — docs/11 § "Schedule model" / § "Due semantics" (M56).
//
// Every rule here is pure and clock-injected, so this file pins the whole
// schedule contract: period labels (including the ISO week-year boundary),
// nextDue for all three frequencies, the due predicate (no artifact ⇒ due,
// off ⇒ never), and the run step's outcome machine — including that a locked
// vault writes NOTHING (there is no server-side fallback) and that a failed
// export/upload is simply retried on the next tick.
import { beforeEach, describe, expect, it } from "vitest";
import type { ArtifactWire, BackupArtifact, BackupSchedule } from "../src/api/backup";
import { decryptArtifact } from "../src/lib/backupArtifact";
import {
  isDue,
  nextDue,
  periodFor,
  runScheduledBackup,
  TICK_MS,
  type ScheduledBackupDeps,
} from "../src/lib/scheduledBackup";
import { createVault, getDEK, lockVault } from "../src/lib/vault";

const OFF: BackupSchedule = {
  frequency: "off",
  hour: 3,
  weekday: 0,
  day_of_month: 1,
  keep_last_n: 5,
};

const SNAPSHOT = {
  accounts: [{ id: 1, name: "Chequing" }],
  transactions: [{ id: 1, raw_description: "CAFE MOCHA" }],
};

/** A registry-shaped export body (every value an array — docs/11 artifact format). */
const MALFORMED = { accounts: "not an array" };

interface Harness {
  deps: ScheduledBackupDeps;
  uploads: ArtifactWire[];
  exports: number;
}

function harness(
  init: {
    schedule?: BackupSchedule;
    items?: BackupArtifact[];
    snapshot?: unknown;
    dek?: CryptoKey | null;
    failExport?: boolean;
    failUpload?: boolean;
    now?: Date;
  } = {},
): Harness {
  const uploads: ArtifactWire[] = [];
  let exports = 0;
  const deps: ScheduledBackupDeps = {
    now: () => init.now ?? new Date(2026, 8, 28, 3, 0, 0),
    getSchedule: async () => init.schedule ?? OFF,
    listArtifacts: async () => ({ items: init.items ?? [] }),
    exportSnapshot: async () => {
      exports += 1;
      if (init.failExport) throw new Error("GET /export/full 500");
      return init.snapshot ?? SNAPSHOT;
    },
    getDEK: () => (init.dek === undefined ? null : init.dek),
    upload: async (wire) => {
      if (init.failUpload) throw new Error("POST /backup/artifacts 500");
      uploads.push(wire);
      return { id: uploads.length };
    },
  };
  return {
    deps,
    uploads,
    get exports() {
      return exports;
    },
  };
}

const DAILY: BackupSchedule = { ...OFF, frequency: "daily", hour: 3 };
const WEEKLY_MONDAY: BackupSchedule = { ...OFF, frequency: "weekly", hour: 3, weekday: 0 };
const MONTHLY_7TH: BackupSchedule = { ...OFF, frequency: "monthly", hour: 3, day_of_month: 7 };

function artifact(createdAt: string): BackupArtifact {
  return { id: 1, period: "2026-09-27", created_at: createdAt, size_bytes: 10 };
}

beforeEach(async () => {
  lockVault();
  await createVault("correct horse battery");
});

describe("TICK_MS", () => {
  it("is the documented 5-minute cadence", () => {
    expect(TICK_MS).toBe(300_000);
  });
});

describe("periodFor — the idempotency key of one run", () => {
  it("daily is the local calendar date", () => {
    expect(periodFor("daily", new Date(2026, 8, 28, 23, 59))).toBe("2026-09-28");
    expect(periodFor("daily", new Date(2026, 0, 1, 0, 0))).toBe("2026-01-01");
  });

  it("weekly is the ISO week (Monday-start label, padded)", () => {
    // 2026-09-28 is a Monday → the week that ISO calls 2026-W40.
    expect(periodFor("weekly", new Date(2026, 8, 28))).toBe("2026-W40");
    expect(periodFor("weekly", new Date(2026, 9, 4))).toBe("2026-W40"); // same Sunday
    expect(periodFor("weekly", new Date(2026, 0, 5))).toBe("2026-W02"); // zero padding
  });

  it("weekly crosses the ISO week-YYYY boundary correctly", () => {
    // 2021-01-01 (Friday) belongs to the week-year 2020, not 2021.
    expect(periodFor("weekly", new Date(2021, 0, 1))).toBe("2020-W53");
    // …and the very next Monday starts the new week-year.
    expect(periodFor("weekly", new Date(2021, 0, 4))).toBe("2021-W01");
    expect(periodFor("weekly", new Date(2026, 11, 31))).toBe("2026-W53");
  });

  it("monthly is the local YYYY-MM", () => {
    expect(periodFor("monthly", new Date(2026, 8, 28))).toBe("2026-09");
    expect(periodFor("monthly", new Date(2026, 11, 31, 23, 59))).toBe("2026-12");
  });
});

describe("nextDue — first local instant strictly after `after`", () => {
  it("is null when the schedule is off", () => {
    expect(nextDue(OFF, new Date(2026, 8, 28))).toBeNull();
  });

  it("daily: hour:00 today if not yet reached, otherwise tomorrow", () => {
    const before = nextDue(DAILY, new Date(2026, 8, 28, 2, 59));
    expect(before).toEqual(new Date(2026, 8, 28, 3, 0));
    // Strictly after: exactly hour:00 rolls to the next day.
    const at = nextDue(DAILY, new Date(2026, 8, 28, 3, 0));
    expect(at).toEqual(new Date(2026, 8, 29, 3, 0));
    const after = nextDue(DAILY, new Date(2026, 8, 28, 18, 30));
    expect(after).toEqual(new Date(2026, 8, 29, 3, 0));
  });

  it("weekly (weekday 0 = Monday, as the picker labels it) lands on a Monday", () => {
    // 2026-09-29 is a Tuesday → the next Monday is 2026-10-05.
    const due = nextDue(WEEKLY_MONDAY, new Date(2026, 8, 29, 4, 0));
    expect(due).toEqual(new Date(2026, 9, 5, 3, 0));
    expect(due?.getDay()).toBe(1); // JS getDay(): 1 = Monday

    // From Monday 03:00 itself the next run is the following Monday.
    expect(nextDue(WEEKLY_MONDAY, new Date(2026, 8, 28, 3, 0))).toEqual(
      new Date(2026, 9, 5, 3, 0),
    );

    // weekday 6 = Sunday (the picker's last label).
    const sunday: BackupSchedule = { ...WEEKLY_MONDAY, weekday: 6 };
    const dueSunday = nextDue(sunday, new Date(2026, 8, 28, 3, 0));
    expect(dueSunday?.getDay()).toBe(0); // JS getDay(): 0 = Sunday
    expect(dueSunday).toEqual(new Date(2026, 9, 4, 3, 0)); // 2026-10-04
  });

  it("monthly: the configured day of month, hour:00", () => {
    expect(nextDue(MONTHLY_7TH, new Date(2026, 8, 28))).toEqual(new Date(2026, 9, 7, 3, 0));
    expect(nextDue(MONTHLY_7TH, new Date(2026, 8, 1))).toEqual(new Date(2026, 8, 7, 3, 0));
    // Past the 7th → next month's 7th (never a day > 28, docs/11).
    expect(nextDue(MONTHLY_7TH, new Date(2026, 9, 7, 3, 0))).toEqual(new Date(2026, 10, 7, 3, 0));
  });
});

describe("isDue — run iff on AND (no artifact OR now >= nextDue(latest))", () => {
  it("is false when the schedule is off", () => {
    expect(isDue(OFF, null, new Date(2026, 8, 28))).toBe(false);
  });

  it("no artifact yet ⇒ due immediately", () => {
    expect(isDue(DAILY, null, new Date(2026, 8, 28, 3, 0))).toBe(true);
    expect(isDue(WEEKLY_MONDAY, null, new Date(2026, 8, 28, 12, 0))).toBe(true);
    expect(isDue(MONTHLY_7TH, null, new Date(2026, 8, 28, 12, 0))).toBe(true);
  });

  it("not due before the next instant, due at and after it", () => {
    const latest = new Date(2026, 8, 28, 3, 0).toISOString();
    expect(isDue(DAILY, latest, new Date(2026, 8, 28, 12, 0))).toBe(false);
    expect(isDue(DAILY, latest, new Date(2026, 8, 29, 2, 59))).toBe(false);
    expect(isDue(DAILY, latest, new Date(2026, 8, 29, 3, 0))).toBe(true);
    expect(isDue(DAILY, latest, new Date(2026, 8, 30, 0, 0))).toBe(true);
  });

  it("weekly waits for the next matching weekday", () => {
    const monday = new Date(2026, 8, 28, 3, 0).toISOString();
    expect(isDue(WEEKLY_MONDAY, monday, new Date(2026, 9, 4, 12, 0))).toBe(false); // next Mon 03:00
    expect(isDue(WEEKLY_MONDAY, monday, new Date(2026, 9, 5, 3, 0))).toBe(true);
  });
});

describe("runScheduledBackup — the outcome machine", () => {
  it("does nothing at all when the schedule is off", async () => {
    const h = harness({ schedule: OFF });
    expect(await runScheduledBackup(h.deps)).toBe("off");
    expect(h.exports).toBe(0);
    expect(h.uploads).toHaveLength(0);
  });

  it("skips when not due (a fresh artifact exists)", async () => {
    const h = harness({
      schedule: DAILY,
      items: [artifact(new Date(2026, 8, 28, 3, 0).toISOString())],
      now: new Date(2026, 8, 28, 12, 0),
      dek: getDEK(),
    });
    expect(await runScheduledBackup(h.deps)).toBe("skipped");
    expect(h.exports).toBe(0);
    expect(h.uploads).toHaveLength(0);
  });

  it("locked vault ⇒ vault_locked with NO export and NO upload", async () => {
    const h = harness({ schedule: DAILY, dek: null });
    expect(await runScheduledBackup(h.deps)).toBe("vault_locked");
    expect(h.exports).toBe(0);
    expect(h.uploads).toHaveLength(0);
  });

  it("export failure ⇒ export_failed, retried on the next tick", async () => {
    const failing = harness({ schedule: DAILY, failExport: true, dek: getDEK() });
    expect(await runScheduledBackup(failing.deps)).toBe("export_failed");
    expect(failing.uploads).toHaveLength(0);

    // The next tick simply tries again against a healthy endpoint.
    const recovered = harness({ schedule: DAILY, dek: getDEK() });
    expect(await runScheduledBackup(recovered.deps)).toBe("uploaded");
    expect(recovered.uploads).toHaveLength(1);
  });

  it("upload failure ⇒ failed, nothing stored, retried next tick", async () => {
    const h = harness({ schedule: DAILY, failUpload: true, dek: getDEK() });
    expect(await runScheduledBackup(h.deps)).toBe("failed");
    expect(h.uploads).toHaveLength(0);
    expect(h.exports).toBe(1); // it tried, and will try again
  });

  it("a malformed export body never reaches the wire", async () => {
    const h = harness({ schedule: DAILY, snapshot: MALFORMED, dek: getDEK() });
    expect(await runScheduledBackup(h.deps)).toBe("export_failed");
    expect(h.uploads).toHaveLength(0);
  });

  it("uploads exactly {period, ciphertext} for the current period", async () => {
    const h = harness({ schedule: DAILY, dek: getDEK(), now: new Date(2026, 8, 28, 3, 0) });
    expect(await runScheduledBackup(h.deps)).toBe("uploaded");
    expect(h.uploads).toHaveLength(1);
    const wire = h.uploads[0];
    expect(Object.keys(wire).sort()).toEqual(["ciphertext", "period"]);
    expect(wire.period).toBe("2026-09-28");
    expect(wire.ciphertext.split(".")[0]).toBe("v1");

    // …and it decrypts back to the snapshot under the session DEK.
    const payload = await decryptArtifact(wire.ciphertext, getDEK());
    expect(payload.v).toBe(1);
    expect(payload.kind).toBe("fintrack-backup");
    expect(payload.period).toBe("2026-09-28");
    expect(payload.exported_at).toBe(new Date(2026, 8, 28, 3, 0).toISOString());
    expect(payload.snapshot).toEqual(SNAPSHOT);
  });

  it("force (Back up now) ignores the due check but never the vault", async () => {
    const fresh = [artifact(new Date(2026, 8, 28, 3, 0).toISOString())];
    const h = harness({ schedule: DAILY, items: fresh, dek: getDEK() });
    expect(await runScheduledBackup(h.deps, { force: true })).toBe("uploaded");
    expect(h.uploads).toHaveLength(1);

    const locked = harness({ schedule: DAILY, items: fresh, dek: null });
    expect(await runScheduledBackup(locked.deps, { force: true })).toBe("vault_locked");
    expect(locked.uploads).toHaveLength(0);
  });

  it("no artifact yet ⇒ the first run happens immediately", async () => {
    const h = harness({ schedule: DAILY, dek: getDEK() });
    expect(await runScheduledBackup(h.deps)).toBe("uploaded");
    expect(h.uploads[0].period).toBe("2026-09-28");
  });
});
