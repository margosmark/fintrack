// @vitest-environment node
// Client↔Python interop fixture — docs/11 M56 → Verification #4.
//
// `backend/tests/test_backup.py::test_client_produced_fixture_decrypts_in_python`
// reads `tests/fixtures/backupArtifact.v1.json` and decrypts it with an
// INDEPENDENT Python implementation (PBKDF2 KEK → AES-GCM unwrap of the DEK →
// `v1.` field envelope). Neither implementation is trusted to agree with the
// other by declaration: this file writes that fixture from the real client
// code, and also verifies the committed copy end-to-end in JS so a broken
// fixture is caught here too.
//
// Regenerate with:
//   WRITE_BACKUP_FIXTURE=1 NODE_ENV=test npx vitest run tests/backupFixture.test.ts
import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  buildPayload,
  decryptArtifact,
  encryptArtifact,
  readWireFile,
  type BackupPayload,
} from "../src/lib/backupArtifact";
import { decryptField, fromBase64, toBase64 } from "../src/lib/fields";
import { createVault, getDEK, lockVault, type WrappedVault } from "../src/lib/vault";
import type { ArtifactWire } from "../src/api/backup";

/** cwd IS the vitest root (frontend/) — `new URL(..., import.meta.url)` breaks in jsdom. */
const FIXTURE_PATH = "tests/fixtures/backupArtifact.v1.json";

// No @types/node in this project (tsconfig covers `src` only), so declare the
// one Node global this test reads.
declare const process: { env: Record<string, string | undefined> };

const PASSPHRASE = "interop fixture passphrase";
const PERIOD = "2026-09-28";
const EXPORTED_AT = new Date("2026-09-28T03:12:44.123Z");
const PBKDF2_ITERATIONS = 600_000; // docs/33 KEK parameters
const DEK_DOMAIN = 0x01; // vault.ts wrapIV domain byte

// `transaction_events` (M106, docs/11 § E2EE vault) is in the snapshot on
// purpose: the receipt payload is the one cell type that is nested, hashed
// and non-ASCII at once, so it is what a byte-level round trip is most likely
// to lose. `payload_hash` is the backend's own canonical digest
// (`services/tx_events.payload_hash`), asserted on the Python side after an
// independent decrypt.
const EVENT_PAYLOAD = {
  fields: ["amount_native", "note"],
  changes: { amount_native: "-12.34", note: "Café ☕ 食品 😀" },
};
const EVENT_HASH = "56e780e57aca0d680d2e27ee08ac4402039822b89361cd30c7a960084e0ac8ca";

const SNAPSHOT = {
  accounts: [{ id: 1, name: "Chequing", type: "checking", currency: "CAD" }],
  categories: [{ id: 1, name: "Coffee" }],
  transactions: [
    { id: 1, account_id: 1, raw_description: "CAFE MOCHA", amount_native: "-4.25" },
  ],
  transaction_events: [
    {
      id: 7,
      tx_id: 1,
      user_id: 1,
      event_kind: "edited",
      payload: EVENT_PAYLOAD,
      payload_hash: EVENT_HASH,
      actor: 1,
      created_at: "2026-09-28T03:11:00Z",
      updated_at: "2026-09-28T03:11:00Z",
    },
  ],
};

interface Fixture {
  passphrase: string;
  salt: string;
  wrappedDEK: string;
  wire: ArtifactWire;
  expected: BackupPayload;
}

/** The real client path: one vault, one payload, one encrypted wire object. */
async function makeArtifact(): Promise<{
  bundle: WrappedVault;
  payload: BackupPayload;
  wire: ArtifactWire;
}> {
  lockVault();
  const bundle = await createVault(PASSPHRASE);
  const payload = buildPayload(SNAPSHOT, PERIOD, EXPORTED_AT);
  const wire = await encryptArtifact(payload, getDEK());
  return { bundle, payload, wire };
}

/** First row of one snapshot table, typed (the payload keeps rows unknown). */
function firstRow(payload: BackupPayload, table: string): Record<string, unknown> {
  const rows: unknown[] = payload.snapshot[table] ?? [];
  return (rows[0] ?? {}) as Record<string, unknown>;
}

function toFixture(
  bundle: WrappedVault,
  payload: BackupPayload,
  wire: ArtifactWire,
): Fixture {
  return {
    passphrase: PASSPHRASE,
    salt: toBase64(bundle.salt),
    wrappedDEK: toBase64(bundle.wrappedDEK),
    wire,
    expected: payload,
  };
}

/**
 * Independent unwrap in this test only: PBKDF2 KEK → AES-GCM decrypt of the
 * wrapped DEK (the exact sequence vault.ts::unlock performs, spelled out so
 * the fixture's own shape is checked rather than vault.ts's internals).
 */
async function unwrapFixtureDEK(fixture: Fixture): Promise<CryptoKey> {
  const salt = fromBase64(fixture.salt);
  const wrapped = fromBase64(fixture.wrappedDEK);
  const material = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(fixture.passphrase),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  const kek = await crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"],
  );
  const iv = new Uint8Array(12);
  iv.set(salt.subarray(0, 12));
  iv[11] ^= DEK_DOMAIN;
  const raw = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, kek, wrapped);
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["decrypt"]);
}

async function verifyFixture(fixture: Fixture): Promise<void> {
  const dek = await unwrapFixtureDEK(fixture);
  const plaintext = await decryptField(fixture.wire.ciphertext, dek);
  expect(JSON.parse(plaintext)).toEqual(fixture.expected);
}

describe("backup artifact envelope (docs/11 § Artifact format)", () => {
  it("produces {period, ciphertext} with a v1 envelope and the right payload", async () => {
    const { payload, wire } = await makeArtifact();
    expect(Object.keys(wire).sort()).toEqual(["ciphertext", "period"]);
    expect(wire.period).toBe(PERIOD);
    const parts = wire.ciphertext.split(".");
    expect(parts).toHaveLength(3);
    expect(parts[0]).toBe("v1");
    expect(await decryptArtifact(wire.ciphertext, getDEK())).toEqual(payload);
    expect(payload).toMatchObject({ v: 1, kind: "fintrack-backup", period: PERIOD });
    expect(payload.exported_at).toBe(EXPORTED_AT.toISOString());
    expect(firstRow(payload, "transactions").raw_description).toBe("CAFE MOCHA");
    // M106: a receipt payload (nested + hashed + non-ASCII) survives intact.
    expect(firstRow(payload, "transaction_events").payload).toEqual(EVENT_PAYLOAD);
    expect(firstRow(payload, "transaction_events").payload_hash).toBe(EVENT_HASH);
  });

  it("fresh IV per artifact: same plaintext, different ciphertext", async () => {
    const first = await makeArtifact();
    const second = await makeArtifact();
    expect(second.wire.ciphertext).not.toBe(first.wire.ciphertext);
  });

  it("fails closed on a wrong key and on a foreign payload", async () => {
    const { wire } = await makeArtifact();
    lockVault();
    await createVault("a different passphrase entirely");
    await expect(decryptArtifact(wire.ciphertext, getDEK())).rejects.toThrow();

    // Decrypts under the right key but is not a fintrack-backup document.
    const dek = getDEK();
    const foreign = await encryptArtifact(
      { v: 1, kind: "fintrack-export", period: PERIOD, exported_at: "x", snapshot: SNAPSHOT },
      dek,
    );
    await expect(decryptArtifact(foreign.ciphertext, dek)).rejects.toThrow(
      "Not a fin-track backup payload.",
    );
  });

  it("refuses to build a payload from a malformed export body", () => {
    expect(() => buildPayload(SNAPSHOT, PERIOD, EXPORTED_AT)).not.toThrow();
    expect(() => buildPayload("{}", PERIOD, EXPORTED_AT)).toThrow();
    expect(() => buildPayload({ accounts: "not-an-array" }, PERIOD, EXPORTED_AT)).toThrow();
    expect(() => buildPayload({}, PERIOD, EXPORTED_AT)).toThrow();
    expect(() => buildPayload(null, PERIOD, EXPORTED_AT)).toThrow();
  });

  it("reads a downloaded .ftbackup.json back into the wire object", async () => {
    const { wire } = await makeArtifact();
    const file = new File([JSON.stringify(wire)], "fin-tracker-backup-2026-09-28.ftbackup.json", {
      type: "application/json",
    });
    expect(await readWireFile(file)).toEqual(wire);

    await expect(readWireFile(new File(["not json"], "x.json"))).rejects.toThrow(
      "That file is not JSON.",
    );
    await expect(readWireFile(new File(["{}"], "x.json"))).rejects.toThrow(
      "That file is not a backup artifact.",
    );
  });
});

describe("interop fixture consumed by backend/tests/test_backup.py", () => {
  it.runIf(process.env.WRITE_BACKUP_FIXTURE === "1")("regenerates the fixture", async () => {
    const { bundle, payload, wire } = await makeArtifact();
    const fixture = toFixture(bundle, payload, wire);
    writeFileSync(FIXTURE_PATH, `${JSON.stringify(fixture, null, 2)}\n`, "utf8");
    await verifyFixture(JSON.parse(readFileSync(FIXTURE_PATH, "utf8")) as Fixture);
  });

  it("verifies the committed fixture end-to-end in JS", async () => {
    const fixture = JSON.parse(readFileSync(FIXTURE_PATH, "utf8")) as Fixture;
    expect(fixture.passphrase).toBe(PASSPHRASE);
    expect(firstRow(fixture.expected, "transactions").raw_description).toBe("CAFE MOCHA");
    expect(firstRow(fixture.expected, "transaction_events").payload).toEqual(EVENT_PAYLOAD);
    await verifyFixture(fixture);
  });
});
