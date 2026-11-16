// M92 F-M91-4 — the two schedule forms on Settings must not share a name.
//
// `ScheduledBackupPanel` and `ScheduledExportPanel` both render a primary
// "Save schedule" button, so a first-match click (by accessible name, by a
// test helper, or by a screen-reader user's quick pick) silently saved the
// WRONG form. This pins the fix on the real components:
//
//   1. both controls exist and carry DISTINCT accessible names;
//   2. each visible label equals its `aria-label` (WCAG 2.5.3 label-in-name);
//   3. no duplicate `aria-label` anywhere in the rendered pair;
//   4. each button actually saves its own form — the other one is untouched.
//
// The page-level uniqueness check QA runs on the deployed build covers the
// whole Settings DOM; this is the component-level regression tripwire.
import { cleanup, act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getSchedule, listArtifacts, putSchedule } from "../src/api/backup";
import { getExportSchedule, putExportSchedule } from "../src/api/export";
import { ScheduledBackupPanel } from "../src/components/ScheduledBackupPanel";
import { ScheduledExportPanel } from "../src/components/ScheduledExportPanel";
import { ToastProvider } from "../src/components/ui/Toast";

// docs/47 §4 (F-2): the panels now ask the session whether it is read-only
// (the demo write-lock verdict). Stub ONLY `useAuth` — every other export
// stays real — so this naming pin renders without an AuthProvider and with a
// live-tenant (null) session: zero padlocks, identical button names.
vi.mock("../src/auth/AuthContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/auth/AuthContext")>();
  return { ...actual, useAuth: () => ({ user: null, setUser: () => {} }) };
});

// The vault's in-memory lock state, driven by hand: `lib/vault.ts` dispatches
// `vaultchange` on create / unlock / lock / revoke and every surface follows it
// through the ONE subscription (`useVaultUnlocked`). Mocking only `isUnlocked`
// (everything else real) lets the test move the vault without a passphrase.
const vault = vi.hoisted(() => ({ locked: true }));

vi.mock("../src/lib/vault", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/vault")>();
  return { ...actual, isUnlocked: () => !vault.locked };
});

vi.mock("../src/api/backup", () => ({
  ARTIFACTS_PATH: "/backup/artifacts",
  SCHEDULE_PATH: "/backup/schedule",
  deleteArtifact: vi.fn(),
  downloadArtifact: vi.fn(),
  getArtifact: vi.fn(),
  getSchedule: vi.fn(),
  listArtifacts: vi.fn(),
  putSchedule: vi.fn(),
  restoreBackup: vi.fn(),
  uploadArtifact: vi.fn(),
}));

vi.mock("../src/api/export", () => ({
  EXPORT_SCHEDULE_PATH: "/export/schedule",
  downloadOfxExport: vi.fn(),
  fetchZip: vi.fn(),
  getExportSchedule: vi.fn(),
  getFullExport: vi.fn(),
  putExportSchedule: vi.fn(),
}));

vi.mock("../src/api/accounts", () => ({
  listAccounts: vi.fn(),
}));

const SCHEDULE = {
  frequency: "off",
  hour: 3,
  weekday: 0,
  day_of_month: 1,
  keep_last_n: 5,
} as const;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function renderPair() {
  vi.mocked(getSchedule).mockResolvedValue({ ...SCHEDULE });
  vi.mocked(listArtifacts).mockResolvedValue([]);
  vi.mocked(getExportSchedule).mockResolvedValue({ ...SCHEDULE });
  vi.mocked(putSchedule).mockImplementation(async (next) => ({ ...next }));
  vi.mocked(putExportSchedule).mockImplementation(async (next) => ({ ...next }));

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <ScheduledBackupPanel />
        <ScheduledExportPanel userId="user-1" />
      </ToastProvider>
    </QueryClientProvider>,
  );
  await screen.findByRole("button", { name: "Save backup schedule" });
  await screen.findByRole("button", { name: "Save export schedule" });
  return view;
}

describe("Settings' two schedule forms (M92 F-M91-4, docs/09 naming contract)", () => {
  it("gives both save controls a distinct accessible name", async () => {
    await renderPair();
    const backup = screen.getByRole("button", { name: "Save backup schedule" });
    const exp = screen.getByRole("button", { name: "Save export schedule" });
    expect(backup).not.toBe(exp);
    // The old shared name is gone from the page entirely.
    expect(screen.queryByRole("button", { name: "Save schedule" })).toBeNull();
  });

  it("keeps every visible label inside its accessible name (WCAG 2.5.3)", async () => {
    const { container } = await renderPair();
    for (const button of container.querySelectorAll<HTMLButtonElement>("button")) {
      const label = button.getAttribute("aria-label");
      if (label == null) continue;
      expect(button.textContent?.trim(), label).toBe(label);
    }
  });

  it("renders no duplicate aria-label across the pair", async () => {
    const { container } = await renderPair();
    const labels = Array.from(container.querySelectorAll("[aria-label]")).map((el) =>
      el.getAttribute("aria-label"),
    );
    expect(labels.length).toBeGreaterThan(0);
    expect(new Set(labels).size, `duplicates in ${labels.join(" | ")}`).toBe(labels.length);
  });

  it("saves the form it belongs to — each button hits its own endpoint", async () => {
    await renderPair();
    fireEvent.click(screen.getByRole("button", { name: "Save backup schedule" }));
    await waitFor(() => expect(putSchedule).toHaveBeenCalledTimes(1));
    expect(putExportSchedule).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Save export schedule" }));
    await waitFor(() => expect(putExportSchedule).toHaveBeenCalledTimes(1));
    expect(putSchedule).toHaveBeenCalledTimes(1);
  });

  // The export panel's lock line is the vault's live state, read through the
  // ONE subscription (`useVaultUnlocked`) rather than a private
  // `vaultchange` listener (docs/33 rule 7c, docs/36). A locked vault exports
  // ciphertext, so the claim has to track lock/unlock instantly — in both
  // directions — and with no network call.
  it("follows the shared vaultchange subscription in both directions", async () => {
    vault.locked = true;
    await renderPair();
    expect(screen.getByText(/Vault locked — ciphertext export/)).toBeTruthy();

    vault.locked = false;
    act(() => {
      window.dispatchEvent(new CustomEvent("vaultchange"));
    });
    await waitFor(() =>
      expect(screen.queryByText(/Vault locked — ciphertext export/)).toBeNull(),
    );

    vault.locked = true;
    act(() => {
      window.dispatchEvent(new CustomEvent("vaultchange"));
    });
    await waitFor(() =>
      expect(screen.getByText(/Vault locked — ciphertext export/)).toBeTruthy(),
    );
  });
});
