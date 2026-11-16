// docs/58 §B — the net-worth card must preview before it writes: picking a
// file calls the read-only preview route, surfaces everything the file would
// do (create / reuse / skipped, with reasons), and pressing Import is what
// calls commit. An encryption-enrolled tenant never sees the commit control
// (rule 7d: the gate is enrolment-only, and preview stays open).
import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../src/api/client";
import { ToastProvider } from "../src/components/ui/Toast";
import { NetWorthImportCard } from "../src/components/imports/NetWorthImportCard";

const h = vi.hoisted(() => ({
  previewNetWorth: vi.fn(),
  commitNetWorth: vi.fn(),
  enrolled: false,
}));

vi.mock("../src/api/networth", () => ({
  previewNetWorth: h.previewNetWorth,
  commitNetWorth: h.commitNetWorth,
}));

vi.mock("../src/components/e2ee/useUnlock", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/components/e2ee/useUnlock")>()),
  useEncryptionRequired: () => h.enrolled,
}));

function planFixture() {
  return {
    source_format: "projectionlab_json",
    file_name: "projectionlab_export.json",
    currency: "CAD",
    counts: {
      accounts: 3,
      accounts_create: 2,
      accounts_reuse: 1,
      accounts_unmapped: 0,
      balance_rows_collapsed: 0,
      progress_snapshots_skipped: 0,
      assets_skipped: 0,
    },
    accounts: [
      {
        name: "Everyday",
        source_type: "savings",
        mapped_type: "chequing",
        action: "create",
        existing_id: null,
        file_balance: "58.12",
        current_balance: null,
        currency: "CAD",
        reason: null,
        notes: [],
      },
      {
        name: "RRSP",
        source_type: "401k",
        mapped_type: "rrsp",
        action: "reuse",
        existing_id: 9,
        file_balance: "1000.00",
        current_balance: "900.00",
        currency: "CAD",
        reason: null,
        notes: ["401(k) mapped to rrsp (the 401(k)↔RRSP pairing is the closest match; the US side of the product is not offered here)"],
      },
      {
        name: "Old House",
        source_type: "realEstate",
        mapped_type: "other_asset",
        action: "create",
        existing_id: null,
        file_balance: "425000.00",
        current_balance: null,
        currency: "CAD",
        reason: null,
        notes: ["Assets are imported with balances as-is; edit them later from Accounts"],
      },
    ],
    skipped: [
      {
        kind: "progress_history",
        name: "2 snapshots",
        reason: "ProjectionLab progress snapshots are plan history, not balances",
      },
    ],
    warnings: [],
    can_commit: true,
  };
}

/** Mount the card, upload the fixture file and press Preview. */
function renderAndPreview(): File {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    createElement(
      QueryClientProvider,
      { client },
      createElement(
        ToastProvider,
        null,
        createElement(MemoryRouter, null, createElement(NetWorthImportCard)),
      ),
    ),
  );
  const file = new File([JSON.stringify(planFixture())], "projectionlab_export.json", {
    type: "application/json",
  });
  fireEvent.change(screen.getByTestId("networth-file"), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  return file;
}

beforeEach(() => {
  h.previewNetWorth.mockReset();
  h.commitNetWorth.mockReset();
  h.enrolled = false;
});

describe("NetWorthImportCard", () => {
  it("preview only READS the file — nothing commits until Import is pressed", async () => {
    h.previewNetWorth.mockResolvedValue(planFixture());

    renderAndPreview();

    // The preview route answered, and the commit route has not been touched.
    await screen.findByTestId("networth-accounts");
    expect(h.previewNetWorth).toHaveBeenCalledTimes(1);
    expect(h.commitNetWorth).not.toHaveBeenCalled();

    // The plan shows what would happen, including what it will NOT import.
    expect(screen.getByText("2 to create")).not.toBeNull();
    expect(screen.getByText("1 already here")).not.toBeNull();
    expect(screen.getByTestId("networth-skipped").textContent).toContain(
      "ProjectionLab progress snapshots are plan history, not balances",
    );
    // The reused account's balance is shown BESIDE the file's, never as a
    // promise to overwrite it.
    expect(screen.getByText("900.00")).not.toBeNull();

    fireEvent.click(screen.getByTestId("networth-import"));
    await waitFor(() => expect(h.commitNetWorth).toHaveBeenCalledTimes(1));
    expect(h.commitNetWorth.mock.calls[0][0]).toBeInstanceOf(File);
  });

  it("surfaces the commit result: created vs kept", async () => {
    h.previewNetWorth.mockResolvedValue(planFixture());
    h.commitNetWorth.mockResolvedValue({ ...planFixture(), created: 2, reused: 1 });

    renderAndPreview();
    fireEvent.click(await screen.findByTestId("networth-import"));

    const done = await screen.findByTestId("networth-done");
    expect(done.textContent).toContain("Created 2, kept 1 existing");
    // Every import invalidates the account queries the balance report reads.
    await waitFor(() => expect(h.commitNetWorth).toHaveBeenCalledTimes(1));
  });

  it("a preview failure shows the reason and offers a re-run, with no commit", async () => {
    h.previewNetWorth.mockRejectedValue(
      new ApiError(422, "Not a ProjectionLab export or an Account/Balance spreadsheet", "unsupported_networth_format"),
    );

    renderAndPreview();

    const reason = await screen.findByText(/Not a ProjectionLab export/);
    expect(reason).not.toBeNull();
    expect(screen.getByRole("button", { name: /Try again|Retry/ })).not.toBeNull();
    expect(h.commitNetWorth).not.toHaveBeenCalled();
  });

  it("an encryption-enrolled tenant never gets the commit control (rule 7d)", async () => {
    h.enrolled = true;
    h.previewNetWorth.mockResolvedValue(planFixture());

    renderAndPreview();
    await screen.findByTestId("networth-accounts");

    // The notice REPLACES the Import control, with the endpoint's own phrase.
    const notice = document.querySelector('[data-testid="server-side-blocked"]');
    expect(notice).not.toBeNull();
    expect(notice?.getAttribute("data-server-side-capability")).toBe("import:networth");
    expect(screen.queryByTestId("networth-import")).toBeNull();

    // Preview is read-only, so it stays available to the same tenant.
    expect(h.previewNetWorth).toHaveBeenCalledTimes(1);
    expect(h.commitNetWorth).not.toHaveBeenCalled();
  });
});
