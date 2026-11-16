// @vitest-environment node
// M51 in-app inbox client (docs/35): endpoint shapes, the badge/banner
// selectors, and the {detail, code} envelope on the demo-guard rejection.
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import { ApiError } from "../src/api/client";
import {
  criticalBudgetItems,
  getNotificationInbox,
  markAllNotificationsRead,
  markNotificationRead,
  unreadBadgeLabel,
  type NotificationInboxItem,
  type NotificationInboxPage,
} from "../src/api/notifications";

/** Minimal Response shape covering exactly what client.ts reads. */
function fakeResponse(opts: { status?: number; body?: unknown }): Response {
  const status = opts.status ?? 200;
  const headers = new Map<string, string>([["content-type", "application/json"]]);
  const body = opts.body;
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: status === 200 ? "OK" : "ERROR",
    headers: { get: (k: string) => headers.get(k.toLowerCase()) ?? null },
    json: async () => (typeof body === "string" ? JSON.parse(body) : body),
  } as unknown as Response;
}

function item(overrides: Partial<NotificationInboxItem> = {}): NotificationInboxItem {
  return {
    id: "budget_threshold:1",
    kind: "budget_threshold",
    severity: "critical",
    title: "Budget alert: Dining",
    body: "Spent 337.00 of 200.00 CAD",
    link: "/budgets",
    amount_cad: "337.00",
    occurred_at: "2026-09-28T14:00:00+00:00",
    read: false,
    read_at: null,
    ...overrides,
  };
}

function page(items: NotificationInboxItem[]): NotificationInboxPage {
  return {
    items,
    total: items.length,
    unread_count: items.filter((i) => !i.read).length,
    limit: 50,
    cursor: null,
    next_cursor: null,
  };
}

describe("inbox endpoints", () => {
  let fetchMock: Mock;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(fakeResponse({ status: 200, body: {} }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("window", { location: { assign: vi.fn() } });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GET /notifications/inbox?limit=50", async () => {
    await getNotificationInbox();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/notifications/inbox?limit=50");
    expect(init.method).toBe("GET");
  });

  it("keeps the composite item id in ONE path segment", async () => {
    await markNotificationRead("budget_threshold:42");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/notifications/inbox/budget_threshold%3A42/read");
    expect(init.method).toBe("POST");
  });

  it("POST /notifications/inbox/read-all", async () => {
    await markAllNotificationsRead();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/notifications/inbox/read-all");
    expect(init.method).toBe("POST");
  });

  it("surfaces the demo guard as {detail, code} (docs/35)", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({
        status: 403,
        body: {
          detail: "The demo account is read-only; changes are not allowed",
          code: "demo_account_restricted",
        },
      }),
    );
    let caught: ApiError | null = null;
    try {
      await markAllNotificationsRead();
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught?.status).toBe(403);
    expect(caught?.code).toBe("demo_account_restricted");
  });

  it("propagates invalid_cursor as {detail, code}, never a bare 500", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({ status: 422, body: { detail: "Invalid cursor", code: "invalid_cursor" } }),
    );
    let caught: ApiError | null = null;
    try {
      await getNotificationInbox();
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.code).toBe("invalid_cursor");
  });
});

describe("unreadBadgeLabel (AppShell)", () => {
  it("hides the badge at zero", () => {
    expect(unreadBadgeLabel(0)).toBe("");
    expect(unreadBadgeLabel(-3)).toBe("");
  });

  it("renders the count and caps at 9+", () => {
    expect(unreadBadgeLabel(1)).toBe("1");
    expect(unreadBadgeLabel(9)).toBe("9");
    expect(unreadBadgeLabel(10)).toBe("9+");
    expect(unreadBadgeLabel(250)).toBe("9+");
  });
});

describe("criticalBudgetItems (Dashboard banner)", () => {
  it("keeps only unread critical budget_threshold items", () => {
    const items = [
      item(),
      item({ id: "budget_threshold:2", severity: "warning" }),
      item({ id: "budget_threshold:3", read: true, read_at: "2026-09-28T15:00:00+00:00" }),
      item({ id: "bill_due:9", kind: "bill_due" }),
      item({ id: "tax_loss:4:2", kind: "tax_loss", severity: "info" }),
    ];
    expect(criticalBudgetItems(page(items)).map((i) => i.id)).toEqual(["budget_threshold:1"]);
  });

  it("renders no banner when the server gated the type away (toggle OFF)", () => {
    // The server never returns budget_threshold items with the toggle OFF, so
    // the client needs no settings read to honour it (docs/35).
    expect(criticalBudgetItems(page([item({ id: "bill_due:9", kind: "bill_due" })]))).toEqual([]);
    expect(criticalBudgetItems(undefined)).toEqual([]);
    expect(criticalBudgetItems(page([]))).toEqual([]);
  });
});
