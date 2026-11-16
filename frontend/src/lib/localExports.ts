/**
 * Local storage for scheduled exports (M75, docs/11 § "Scheduled export").
 *
 * A scheduled run does NOT download a file — it stores the ZIP blob in
 * IndexedDB under the period key (`2026-09-28` / `2026-W39` / `2026-09`) and
 * the Settings panel lists them for Download / Delete. The archive therefore
 * stays on the user's own machine: no plaintext export is written to a third
 * server-side store, and keep-last-N is enforced locally the moment a record
 * is written.
 *
 * One record per user (`user:<id>` key), holding that user's array sorted
 * newest-first. Keying by user id keeps a shared browser from presenting one
 * account's archives under another's session after a logout/login.
 *
 * Availability follows the same rule as `privacyBackup.ts`: IndexedDB may be
 * missing or blocked (private mode, storage pressure). Callers treat a
 * rejection as "unsupported" and fall back to the one-click download.
 */

const DB_NAME = "fintrack-exports";
const DB_VERSION = 1;
const STORE = "exports";

/** Metadata the list view renders; `blob` is fetched only on Download. */
export interface StoredExportMeta {
  /** The idempotency key of the run (docs/11 period keys). */
  period: string;
  /** When the run actually happened (ISO-8601, local time). */
  createdAt: string;
  sizeBytes: number;
  filename: string;
}

export interface StoredExport extends StoredExportMeta {
  blob: Blob;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("IndexedDB unavailable"));
      return;
    }
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
  });
}

function requestToPromise<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function sortNewestFirst(items: StoredExport[]): StoredExport[] {
  return [...items].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

function userKey(userId: string): string {
  return `user:${userId}`;
}

async function readAll(userId: string): Promise<StoredExport[]> {
  const db = await openDb();
  try {
    const store = db.transaction(STORE, "readonly").objectStore(STORE);
    const value = await requestToPromise(store.get(userKey(userId)));
    return Array.isArray(value) ? (value as StoredExport[]) : [];
  } finally {
    db.close();
  }
}

async function writeAll(userId: string, items: StoredExport[]): Promise<void> {
  const db = await openDb();
  try {
    const transaction = db.transaction(STORE, "readwrite");
    transaction.objectStore(STORE).put(items, userKey(userId));
    await new Promise<void>((resolve, reject) => {
      transaction.oncomplete = () => resolve();
      transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB write failed"));
      transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB write failed"));
    });
  } finally {
    db.close();
  }
}

/** `true` when this browser can hold the archives (private-mode safe). */
export function localExportsAvailable(): boolean {
  return typeof indexedDB !== "undefined";
}

/** Newest first. Rejects when IndexedDB is unavailable. */
export async function listLocalExports(userId: string): Promise<StoredExportMeta[]> {
  const items = await readAll(userId);
  return sortNewestFirst(items).map(({ period, createdAt, sizeBytes, filename }) => ({
    period,
    createdAt,
    sizeBytes,
    filename,
  }));
}

/**
 * Upsert by period, then enforce keep-last-N. Returns the new list so the
 * panel can re-render without a second read.
 */
export async function putLocalExport(
  userId: string,
  record: StoredExport,
  keepLastN: number,
): Promise<StoredExportMeta[]> {
  const items = (await readAll(userId)).filter((item) => item.period !== record.period);
  items.push(record);
  const kept = sortNewestFirst(items).slice(0, Math.max(1, keepLastN));
  await writeAll(userId, kept);
  return kept.map(({ period, createdAt, sizeBytes, filename }) => ({
    period,
    createdAt,
    sizeBytes,
    filename,
  }));
}

export async function deleteLocalExport(userId: string, period: string): Promise<StoredExportMeta[]> {
  const items = (await readAll(userId)).filter((item) => item.period !== period);
  await writeAll(userId, items);
  return sortNewestFirst(items).map(({ period: kept, createdAt, sizeBytes, filename }) => ({
    period: kept,
    createdAt,
    sizeBytes,
    filename,
  }));
}

/** The stored ZIP for one period, or `null` (already deleted elsewhere). */
export async function readLocalExport(userId: string, period: string): Promise<Blob | null> {
  const items = await readAll(userId);
  return sortNewestFirst(items).find((item) => item.period === period)?.blob ?? null;
}
