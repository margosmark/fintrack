// M90 household sharing — the frontend pins docs/43 requires:
//
//  1. "partner resolves with dek=null": the shared page renders the owner's
//     cipher rows in the LOCKED shape even when THIS browser's vault is
//     unlocked (their DEK must never be passed — docs/43 §8);
//  2. `401 sharing_not_linked` renders the in-place empty state, never the
//     refresh → /login dance (docs/43 §7/§13);
//  3. client.ts's AUTH_401_SKIP_PREFIXES lets that domain-state 401 through
//     with NO refresh attempt (the mechanism itself, unmocked — pinned on a
//     DATA route, since /state no longer produces it for an unlinked caller);
//  4. the accept page renders the error envelope verbatim + hint and does
//     not redirect to /login (docs/43 §13);
//  5. (M92) `200 {linked: false}` from GET /partner/state is DATA, not an
//     error: PartnerSharingRow renders nothing and no query errors.
//  6. the confirm/leave control the partner's Settings row carries (M90 D6
//     follow-up, docs/43 §5): armed by the first press, POSTs
//     /sharing/leave, then the row disappears on the refetched
//     `{linked: false}`; and a 404 `sharing_not_linked` envelope renders in
//     place, no redirect;
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../src/api/client";
import type { SharedView } from "../src/api/sharing";

vi.mock("../src/api/sharing", async (importOriginal) => ({
  // Real module underneath (typed client, lib/budgets) with the functions these
  // tests drive swapped for spies.
  ...(await importOriginal<typeof import("../src/api/sharing")>()),
  loadSharedView: vi.fn(),
  acceptInvite: vi.fn(),
  getPartnerState: vi.fn(),
  leaveSharing: vi.fn(),
}));

// The partner's OWN vault reports UNLOCKED with a junk key: if the shared
// page ever resolves through its default argument instead of an explicit
// `null`, decryptField runs with that junk and the row never renders — this
// test then goes red (docs/43 §8: their DEK must never be passed, even
// unlocked).
vi.mock("../src/lib/vault", () => ({
  isUnlocked: () => true,
  getDEK: () => ({}) as CryptoKey,
  getIndexKey: () => ({}) as CryptoKey,
}));

import { acceptInvite, getPartnerState, leaveSharing, loadSharedView } from "../src/api/sharing";
import type { PartnerState, SharingState } from "../src/api/sharing";
import { SharedHousehold } from "../src/pages/SharedHousehold";
import { SharingAccept } from "../src/pages/SharingAccept";
import { PartnerSharingRow } from "../src/components/settings/SharingSection";
import { ToastProvider } from "../src/components/ui/Toast";

const cipherRow = {
  id: 1,
  account_id: 1,
  date: "2026-10-01",
  posting_date: null,
  raw_description: "SECRET-RAW-DESCRIPTION",
  normalized_payee: "SECRET-PAYEE",
  amount_native: "-10.00",
  currency: "CAD",
  cad_amount: "-10.00",
  category_id: null,
  transfer_pair_id: null,
  pair_account_name: null,
  tags: [],
  payee_cipher: "c2l2ZXItdmF1bHQ",
  raw_desc_cipher: "c2l2ZXItdmF1bHQ",
  amount_cipher: "c2l2ZXItdmF1bHQ",
  cad_amount_cipher: null,
  payee_token: "blindex-token",
};

const plainRow = {
  ...cipherRow,
  id: 2,
  raw_description: "Plain Coffee Shop",
  normalized_payee: "Plain Coffee",
  amount_native: "-4.50",
  payee_cipher: null,
  raw_desc_cipher: null,
  amount_cipher: null,
  payee_token: null,
};

const view: SharedView = {
  state: {
    linked: true,
    owner: { user_id: 7, name: "Owner", email: "owner@example.com" },
    account_ids: [1],
    privacy_mode: false,
    link_id: 1,
    accepted_at: "2026-10-02T15:00:00Z",
  },
  accounts: [
    {
      id: 1,
      institution_id: null,
      name: "Shared Chequing",
      type: "chequing",
      currency: "CAD",
      taxable: true,
      llm_autopilot: false,
      cash_balance: "100.00",
      investment_value: "0.00",
      total_value: "100.00",
      starting_balance: "0.00",
      name_cipher: null,
    },
  ],
  transactions: [cipherRow, plainRow],
  budgetProgress: [
    {
      id: 9,
      user_id: 7,
      category_id: 1,
      amount: "600.00",
      period: "monthly",
      category_name: "groceries",
      spent: "62.12",
      remaining: "537.88",
      pct_used: 10.35,
    },
  ],
};

function renderPage(element: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    createElement(QueryClientProvider, { client, children: element }),
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("partner resolves with dek=null (docs/43 §8)", () => {
  it("renders the locked shape for cipher rows while this vault is unlocked", async () => {
    vi.mocked(loadSharedView).mockResolvedValue(view);
    renderPage(createElement(SharedHousehold));

    // Cipher rows resolve to the lock marker — never the plaintext, never a
    // decrypt attempt with the partner's own (unlocked) vault.
    expect(await screen.findAllByText("🔒")).not.toHaveLength(0);
    expect(screen.queryByText("SECRET-PAYEE")).toBeNull();
    expect(screen.queryByText("SECRET-RAW-DESCRIPTION")).toBeNull();

    // Legacy plaintext rows render their plaintext (the locked-shape rule is
    // per-field, docs/33 A16).
    expect(screen.getByText("Plain Coffee")).toBeTruthy();

    // Scope banner + the D3 budget label (progress from these rows only).
    expect(screen.getByText(/figures exclude everything else/)).toBeTruthy();
    // Owner identity comes from GET /partner/state (docs/43 §6/§13).
    expect(screen.getByText(/Shared with you by/)).toBeTruthy();
    expect(screen.getByText("shared accounts only")).toBeTruthy();
    expect(screen.getByText(/groceries/)).toBeTruthy();
  });

  it("renders the access-removed empty state on 401 sharing_not_linked, without a redirect", async () => {
    vi.mocked(loadSharedView).mockRejectedValue(
      new ApiError(401, "No shared household is linked to this account", "sharing_not_linked"),
    );
    renderPage(createElement(SharedHousehold));

    expect(
      await screen.findByText("No shared household — access was removed"),
    ).toBeTruthy();
    // In place: the partner's session is valid — no bounce to /login.
    expect(window.location.pathname).not.toBe("/login");
    expect(screen.queryByText("Welcome back")).toBeNull();
  });
});

describe("client 401 handling (docs/43 §7)", () => {
  // M92: /partner/state no longer 401s for an unlinked caller (it answers
  // 200 {linked: false}), so the skip mechanism is pinned on a DATA route,
  // which still does — the /partner/ prefix covers both.
  it("AUTH_401_SKIP_PREFIXES lets /partner/* 401 through with no refresh attempt", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: "No shared household is linked to this account",
          code: "sharing_not_linked",
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(api("/partner/accounts")).rejects.toMatchObject({
        name: "ApiError",
        status: 401,
        code: "sharing_not_linked",
      });
      // ONE call: no POST /auth/refresh, no replay, no hard redirect.
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/api/v1/partner/accounts");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("SharingAccept (docs/43 §13)", () => {
  it("renders the error envelope verbatim with its hint and stays on the page", async () => {
    vi.mocked(acceptInvite).mockRejectedValue(
      new ApiError(401, "This invitation has expired", "sharing_invite_expired"),
    );
    renderPage(
      createElement(MemoryRouter, {
        initialEntries: ["/sharing/accept?token=the-token"],
        children: createElement(SharingAccept),
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));

    // The envelope, verbatim: detail AND code, plus the human hint.
    expect(await screen.findByText("This invitation has expired")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByText("sharing_invite_expired")).toBeTruthy();
    });
    expect(screen.getByText("Ask for a new invitation link.")).toBeTruthy();
    // No navigation happened — still on the accept screen.
    expect(screen.getByRole("button", { name: /confirm/i })).toBeTruthy();
    expect(window.location.pathname).not.toBe("/login");
  });
});

// --- M92 F-M91-5: /partner/state's unlinked arm is data, not an error -------

describe("PartnerSharingRow (M92, docs/43 §6/§13)", () => {
  it("renders nothing for 200 {linked: false} — a normal state, not a failure", async () => {
    vi.mocked(getPartnerState).mockResolvedValue({ linked: false });
    const { container } = renderPage(createElement(ToastProvider, { children: createElement(PartnerSharingRow) }));
    await waitFor(() => expect(vi.mocked(getPartnerState)).toHaveBeenCalledTimes(1));
    // Nothing to show, and no bounce to /login — the invite CTA above (the
    // SharingSection card, always rendered on Settings) is the next step.
    // The only node present is ToastProvider's own (empty) notification
    // region: the row itself renders nothing.
    expect(container.textContent).toBe("");
    expect(window.location.pathname).not.toBe("/login");
  });

  it("renders the read-only line for 200 {linked: true}", async () => {
    vi.mocked(getPartnerState).mockResolvedValue({
      linked: true,
      owner: { user_id: 7, name: "Owner", email: "owner@example.com" },
      account_ids: [1, 2],
      privacy_mode: false,
      link_id: 4,
      accepted_at: "2026-10-02T15:00:00Z",
    });
    const { container } = renderPage(createElement(ToastProvider, { children: createElement(PartnerSharingRow) }));
    expect(await screen.findByText(/You can see/)).toBeTruthy();
    expect(container.textContent).toMatch(/Owner/);
    expect(container.textContent).toMatch(/2 shared accounts/);
    expect(container.textContent).toMatch(/since 2026-10-02/);
  });
});

describe("partner leave (M90 D6 follow-up, docs/43 §5)", () => {
  const linked: PartnerState = {
    linked: true,
    owner: { user_id: 7, name: "Owner", email: "owner@example.com" },
    account_ids: [1],
    privacy_mode: false,
    link_id: 1,
    accepted_at: "2026-10-02T15:00:00Z",
  };
  const revoked: SharingState = {
    status: "revoked",
    link_id: 1,
    email: "partner@example.com",
    partner: { user_id: 8, name: "Partner", email: "partner@example.com" },
    account_ids: [1],
    expires_at: null,
    created_at: "2026-10-02T14:00:00Z",
    accepted_at: "2026-10-02T15:00:00Z",
    revoked_at: "2026-10-03T09:00:00Z",
  };
  function renderRow() {
    return renderPage(createElement(ToastProvider, { children: createElement(PartnerSharingRow) }));
  }

  it("arms on the first press, then POSTs and the row disappears", async () => {
    vi.mocked(getPartnerState)
      .mockResolvedValueOnce(linked)
      .mockResolvedValue({ linked: false });
    vi.mocked(leaveSharing).mockResolvedValue(revoked);
    renderRow();
    expect(await screen.findByText(/You can see/)).toBeTruthy();

    // Press 1 arms the confirm — no request yet (the owner's Revoke pattern).
    fireEvent.click(screen.getByRole("button", { name: "Leave" }));
    expect(screen.getByRole("button", { name: "Confirm leave" })).toBeTruthy();
    expect(leaveSharing).not.toHaveBeenCalled();
    expect(window.location.pathname).not.toBe("/login");

    // "Stay" backs out of the armed state.
    fireEvent.click(screen.getByRole("button", { name: "Stay" }));
    expect(screen.getByRole("button", { name: "Leave" })).toBeTruthy();
    expect(leaveSharing).not.toHaveBeenCalled();

    // Press 1 again + confirm → exactly one POST, then the row renders null
    // once the refetched GET /partner/state answers `200 {linked: false}`.
    fireEvent.click(screen.getByRole("button", { name: "Leave" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm leave" }));
    await waitFor(() => expect(leaveSharing).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("You left the shared household")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText(/You can see/)).toBeNull());
    // Their own session is fine: nothing bounced them to /login.
    expect(window.location.pathname).not.toBe("/login");
    expect(screen.queryByText("Welcome back")).toBeNull();
  });

  it("renders a 404 sharing_not_linked envelope in place instead of redirecting", async () => {
    vi.mocked(getPartnerState).mockResolvedValue(linked);
    vi.mocked(leaveSharing).mockRejectedValue(
      new ApiError(404, "No shared household is linked to this account", "sharing_not_linked"),
    );
    renderRow();
    expect(await screen.findByText(/You can see/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Leave" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm leave" }));

    expect(await screen.findByText("No shared household is linked to this account")).toBeTruthy();
    // The row stays (the state query never moved) and there is no bounce.
    expect(screen.getByText(/You can see/)).toBeTruthy();
    expect(window.location.pathname).not.toBe("/login");
  });
});

describe("partner privacy mode (docs/43 §9, M90 D4/A)", () => {
  /** The privacy-on payload: every money field is JSON `null`, progress [] . */
  const privacyView: SharedView = {
    ...view,
    state: { ...view.state, privacy_mode: true },
    accounts: view.accounts.map((account) => ({
      ...account,
      cash_balance: null,
      investment_value: null,
      total_value: null,
      starting_balance: null,
    })),
    transactions: view.transactions.map((tx) => ({
      ...tx,
      amount_native: null,
      cad_amount: null,
    })),
    budgetProgress: [],
  };

  it("labels the hidden figures instead of rendering blanks", async () => {
    vi.mocked(loadSharedView).mockResolvedValue(privacyView);
    renderPage(createElement(SharedHousehold));

    // The caption docs/09 pins sits where the numbers would be.
    expect(await screen.findByText(/Amounts hidden/)).toBeTruthy();
    expect(screen.getAllByText(/privacy mode on/).length).toBeGreaterThanOrEqual(1);

    // The account card shows 🔒 with a title saying why (never a blank
    // figure cell), and every feed row resolves to 🔒: the cipher row from
    // its cipher, the plaintext row from the null amount. A cipher payee
    // adds its own 🔒, so count at least one per money slot.
    expect(screen.getByTitle("Hidden by the owner’s privacy mode")).toBeTruthy();
    const locks = await screen.findAllByText("🔒");
    expect(locks.length).toBeGreaterThanOrEqual(1 + privacyView.transactions.length);

    // No budget bars are drawn from figures that do not exist, and the
    // section says so; the D3 label stays because the scope is unchanged.
    expect(screen.queryByText(/groceries/)).toBeNull();
    expect(screen.getByText(/Budget amounts are hidden/)).toBeTruthy();
    expect(screen.getByText("shared accounts only")).toBeTruthy();

    // Identity still shows: owner, account name, payee — and no figure.
    expect(screen.getByText(/Shared with you by/)).toBeTruthy();
    expect(screen.getByText("Shared Chequing")).toBeTruthy();
    expect(screen.getByText("Plain Coffee")).toBeTruthy();
    expect(screen.queryByText(/100\.00/)).toBeNull();
    expect(screen.queryByText(/-4\.50/)).toBeNull();
  });
});
