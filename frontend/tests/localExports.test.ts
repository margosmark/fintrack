// @vitest-environment node
// Local storage for scheduled exports — docs/11 § "Scheduled export" (M75).
//
// The archive lives in IndexedDB, so these tests pin the two properties the
// feature's honesty depends on: keep-last-N is enforced the moment a record
// is written (a stale archive can never outlive its retention), and records
// are keyed per user so a shared browser never lists another account's
// archives under a fresh session.
import "fake-indexeddb/auto";
import { describe, expect, it } from "vitest";
import {
  deleteLocalExport,
  listLocalExports,
  localExportsAvailable,
  putLocalExport,
  readLocalExport,
  type StoredExport,
} from "../src/lib/localExports";

function record(period: string, createdAt: string, sizeBytes = 2048): StoredExport {
  return {
    period,
    createdAt,
    sizeBytes,
    filename: `fin-tracker-export-${period}.zip`,
    blob: new Blob([`zip-${period}`], { type: "application/zip" }),
  };
}

describe("localExports (IndexedDB)", () => {
  it("reports availability whenever the API exists", () => {
    expect(localExportsAvailable()).toBe(true);
  });

  it("stores a record and returns it newest-first as metadata only", async () => {
    await putLocalExport("user-a", record("2026-09-27", "2026-09-27T03:00:00.000Z"), 5);
    const items = await putLocalExport(
      "user-a",
      record("2026-09-28", "2026-09-28T03:00:00.000Z"),
      5,
    );

    expect(items).toHaveLength(2);
    expect(items[0].period).toBe("2026-09-28");
    expect(items[1].period).toBe("2026-09-27");
    // Metadata only — the blob must not be dragged through every list render.
    expect(Object.keys(items[0]).sort()).toEqual([
      "createdAt",
      "filename",
      "period",
      "sizeBytes",
    ]);

    const blob = await readLocalExport("user-a", "2026-09-27");
    expect(blob).not.toBeNull();
    expect(await blob!.text()).toBe("zip-2026-09-27");
  });

  it("upserts by period instead of duplicating a re-run", async () => {
    await putLocalExport("user-b", record("2026-09-28", "2026-09-28T03:00:00.000Z", 1000), 5);
    await putLocalExport("user-b", record("2026-09-28", "2026-09-28T09:00:00.000Z", 2000), 5);

    const items = await listLocalExports("user-b");
    expect(items).toHaveLength(1);
    expect(items[0].createdAt).toBe("2026-09-28T09:00:00.000Z");
    expect(items[0].sizeBytes).toBe(2000);
  });

  it("enforces keep-last-N the moment a record is written", async () => {
    const days = ["2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"];
    for (const day of days) {
      await putLocalExport("user-c", record(day, `${day}T03:00:00.000Z`), 3);
    }
    const items = await listLocalExports("user-c");
    expect(items.map((item) => item.period)).toEqual(["2026-09-24", "2026-09-23", "2026-09-22"]);
    expect(await readLocalExport("user-c", "2026-09-20")).toBeNull();
  });

  it("keeps one account's archives out of another's list", async () => {
    await putLocalExport("user-d", record("2026-09-28", "2026-09-28T03:00:00.000Z"), 5);
    await putLocalExport("user-e", record("2026-01-02", "2026-01-02T03:00:00.000Z"), 5);

    expect((await listLocalExports("user-d")).map((i) => i.period)).toEqual(["2026-09-28"]);
    expect((await listLocalExports("user-e")).map((i) => i.period)).toEqual(["2026-01-02"]);
    expect(await readLocalExport("user-e", "2026-09-28")).toBeNull();
  });

  it("deletes exactly one period and reports the remainder", async () => {
    await putLocalExport("user-f", record("2026-09-27", "2026-09-27T03:00:00.000Z"), 5);
    await putLocalExport("user-f", record("2026-09-28", "2026-09-28T03:00:00.000Z"), 5);

    const after = await deleteLocalExport("user-f", "2026-09-28");
    expect(after.map((i) => i.period)).toEqual(["2026-09-27"]);
    expect(await readLocalExport("user-f", "2026-09-28")).toBeNull();
    expect(await readLocalExport("user-f", "2026-09-27")).not.toBeNull();
  });

  it("returns an empty list for a user with no archives", async () => {
    expect(await listLocalExports("user-never")).toEqual([]);
    expect(await readLocalExport("user-never", "2026-09-28")).toBeNull();
  });
});
