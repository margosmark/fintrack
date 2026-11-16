// M74 — the OIDC landing page (docs/38 "OIDC SSO", decision 8).
//
// The backend callback never puts a token in the URL: it sets the httpOnly
// `oidc_pending` cookie and 302s the browser to /login/oidc (optionally with
// ?error=<code>). This page is therefore the ONLY place that reads that cookie
// — and it must:
//   * map a redirect error code to human copy (the unknown-account case has a
//     pinned sentence), without touching the network at all;
//   * on an unlinked identity, show the confirmation card and POST confirm
//     only on the explicit Continue click (an IdP can never create an account
//     by itself — decision 7);
//   * enter the app with the same post-auth redirect the login page uses.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../src/api/client";
import type { User } from "../src/types";

const mocks = vi.hoisted(() => ({
  restoreSession: vi.fn(),
  oidcPreview: vi.fn(),
  oidcConfirm: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, ...mocks };
});

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { OidcLogin } from "../src/pages/OidcLogin";

const USER: User = {
  id: "user-7",
  email: "alice@example.com",
  privacy_mode: false,
  currency_tracker_enabled: false,
  llm_provider: "none",
  llm_base_url: null,
  llm_model: "",
  encryption_required: false,
};

const PAIR = {
  access_token: "token-7",
  token_type: "bearer" as const,
  user: USER,
};

function renderOidc(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <AuthProvider>
            <Routes>
              <Route path="/login/oidc" element={<OidcLogin />} />
              <Route path="/login" element={<div data-testid="login-page" />} />
              <Route path="/dashboard" element={<div data-testid="app" />} />
            </Routes>
          </AuthProvider>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.restoreSession.mockResolvedValue(null);
});

describe("OidcLogin — redirect error mapping", () => {
  it("renders the pinned unknown-account sentence without reading the cookie", async () => {
    renderOidc("/login/oidc?error=oidc_unknown_account");

    expect(
      await screen.findByText(
        "No fin_tracker account matches that sign-in email — sign in with your password once to link it.",
      ),
    ).toBeTruthy();
    // Recovery affordance back to the password door (a link — no navigation
    // happens until the user clicks it).
    expect(screen.getByRole("link", { name: "Back to sign in" })).toBeTruthy();
    // The error came from a browser navigation, not from the pending cookie:
    // nothing here may hit preview/confirm.
    expect(mocks.oidcPreview).not.toHaveBeenCalled();
    expect(mocks.oidcConfirm).not.toHaveBeenCalled();
  });

  it("maps the other redirect codes to readable copy", async () => {
    renderOidc("/login/oidc?error=oidc_state_mismatch");
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("did not match this browser session");
    expect(mocks.oidcPreview).not.toHaveBeenCalled();
  });
});

describe("OidcLogin — unlinked identity", () => {
  it("shows the confirmation card and enters the app on Continue", async () => {
    mocks.oidcPreview.mockResolvedValue({ email: "alice@example.com", linked: false });
    mocks.oidcConfirm.mockResolvedValue(PAIR);

    renderOidc("/login/oidc");

    expect(await screen.findByText("Continue as")).toBeTruthy();
    expect(screen.getByText("alice@example.com")).toBeTruthy();
    expect(mocks.oidcConfirm).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Continue/ }));

    await waitFor(() => expect(mocks.oidcConfirm).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("app")).toBeTruthy();
  });

  it("renders an expired state when the pending cookie is gone", async () => {
    mocks.oidcPreview.mockRejectedValue(
      new ApiError(401, "Sign-in request expired", "oidc_pending_expired"),
    );

    renderOidc("/login/oidc");

    expect(await screen.findByTestId("oidc-expired")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to sign in" })).toBeTruthy();
    expect(mocks.oidcConfirm).not.toHaveBeenCalled();
  });
});
