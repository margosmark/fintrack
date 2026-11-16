// tenantScopedStorage — F-6 pins (security review of the M185 candidate).
//
// Two mechanisms, one claim: browser storage that carries row identifiers or
// account-keyed UI state resolves DIFFERENTLY for two tenants sharing one
// browser (the F-0 class, without F-0's payload).
//
//   1. the scope helpers themselves (`lib/tenantStorage.ts`): user id first,
//      email fallback, a "signed-out" bucket that can never collide with a
//      signed-in tenant;
//   2. a REAL two-tenant behaviour: tenant A dismisses the pending-import
//      banner for account 1 — tenant B, in the same tab, still sees theirs,
//      because the flag's key carries the tenant scope (the pre-fix key was
//      account-id only, so A's dismissal silently hid B's banner).
//
// The other scoped keys are pinned where they behave: the positions lens key
// (m146PositionsBySecurity) and the review-hint key (m106ReviewQueue) both
// assert their SCOPED strings, and the dividend-dismiss key is pinned
// against the real page in demoLockRollout.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { PendingImportBanner } from "../src/components/imports/PendingImportBanner";
import { AuthUserScopeContext } from "../src/auth/authUser";
import type { User } from "../src/types";
import { scopedKey, tenantScope } from "../src/lib/tenantStorage";

// jsdom has no matchMedia (framer-motion's reduced-motion probe calls it).
if (window.matchMedia === undefined) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

/** A minimal `User` for the scope provider — only id/email are read. */
const tenant = (id: string, email: string): User =>
  ({
    id,
    email,
    privacy_mode: false,
    currency_tracker_enabled: false,
    llm_provider: "openai",
    llm_base_url: null,
    llm_model: "gpt-4o-mini",
    encryption_required: false,
    dashboard_preset: "overview",
    saved_report_views: [],
    recurring_intent_tag_ids: [],
  }) as User;

/** Render the banner under an explicit tenant (the app's AuthProvider does
 *  the same through `AuthUserScopeContext`). */
function renderAs(user: User | null, accountId = 1) {
  return render(
    <MemoryRouter>
      <AuthUserScopeContext.Provider value={{ user }}>
        <PendingImportBanner accountId={accountId} count={1} label="TFSA" />
      </AuthUserScopeContext.Provider>
    </MemoryRouter>,
  );
}

const BANNER = "pending-import-banner";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  localStorage.clear();
});

describe("tenantScope / scopedKey (lib/tenantStorage.ts)", () => {
  it("prefers the server's user id, falls back to the email, never to nothing", () => {
    expect(tenantScope({ id: "7", email: "a@b.c" })).toBe("7");
    expect(tenantScope({ email: "a@b.c" })).toBe("a@b.c");
    expect(tenantScope(null)).toBe("signed-out");
    expect(tenantScope(undefined)).toBe("signed-out");
    // An empty string is "no value", not a scope — two empty-id tenants must
    // not share a bucket that looks scoped.
    expect(tenantScope({ id: "", email: "" })).toBe("signed-out");
  });

  it("appends the scope to the base key and only the scope", () => {
    expect(scopedKey("fintrack:positions_view", { id: "7", email: "a@b.c" })).toBe(
      "fintrack:positions_view.7",
    );
    expect(scopedKey("fintrack.m106.unreviewed-hint.dismissed", null)).toBe(
      "fintrack.m106.unreviewed-hint.dismissed.signed-out",
    );
  });
});

describe("PendingImportBanner — two tenants, one browser (F-6)", () => {
  it("A's dismissal never hides B's banner for the SAME account id", async () => {
    sessionStorage.clear();

    // Tenant A dismisses the banner for account 1.
    const alice = tenant("1", "alice@example.com");
    renderAs(alice);
    expect(screen.getByTestId(BANNER)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Dismiss the uncommitted-import banner/ }));
    cleanup();

    // Tenant B, same tab, same account id: the flag is A's, not B's.
    const bob = tenant("2", "bob@example.com");
    renderAs(bob);
    expect(screen.getByTestId(BANNER)).toBeTruthy();
    cleanup();

    // A's own dismissal still stands for A.
    renderAs(alice);
    expect(screen.queryByTestId(BANNER)).toBeNull();

    // The flag lives under A's scoped key, never the bare account key.
    expect(sessionStorage.getItem("fintrack.m113.pending-import-banner.1.1.dismissed")).toBe(
      "1",
    );
    expect(sessionStorage.getItem("fintrack.m113.pending-import-banner.2.1.dismissed")).toBeNull();
    expect(sessionStorage.getItem("fintrack.m113.pending-import-banner.1.dismissed")).toBeNull();
  });

  it("an unresolved session lands in the signed-out bucket, out of every tenant's way", () => {
    renderAs(null);
    expect(screen.getByTestId(BANNER)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Dismiss the uncommitted-import banner/ }));
    expect(
      sessionStorage.getItem("fintrack.m113.pending-import-banner.signed-out.1.dismissed"),
    ).toBe("1");
  });
});
