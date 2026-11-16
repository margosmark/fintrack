// notifications — user alert settings + the M51 in-app inbox (docs/35).
import { api } from "./client";

export interface NotificationSetting {
  id: number;
  user_id: number;
  alert_type: string;
  enabled: boolean;
  config: Record<string, unknown> | null;
}

/** The only three kinds the inbox unions — the M31 legacy alert_types have no
 *  generator and can never appear here (docs/35). */
export type InboxKind = "budget_threshold" | "bill_due" | "tax_loss";
export type InboxSeverity = "critical" | "warning" | "info";

export interface NotificationInboxItem {
  id: string;
  kind: InboxKind;
  severity: InboxSeverity;
  title: string;
  body: string;
  /** In-app route to open (e.g. "/budgets"). */
  link: string;
  /** Decimal-string or null (budget: spent; bill: amount; tax: unrealized loss). */
  amount_cad: string | null;
  /** ISO-8601 UTC — the sort key. */
  occurred_at: string;
  read: boolean;
  read_at: string | null;
}

export interface NotificationInboxPage {
  items: NotificationInboxItem[];
  /** Items in the WHOLE gated inbox (ignores limit/cursor). */
  total: number;
  /** read=false items across the WHOLE gated inbox — drives the AppShell badge. */
  unread_count: number;
  limit: number;
  /** Echo of the request cursor. */
  cursor: string | null;
  /** Opaque cursor for the next page; null on the last page. */
  next_cursor: string | null;
}

export function listNotificationSettings(): Promise<NotificationSetting[]> {
  return api<NotificationSetting[]>("/notifications");
}

export function updateNotificationSetting(
  id: number,
  patch: { enabled?: boolean; config?: Record<string, unknown> },
): Promise<NotificationSetting> {
  return api<NotificationSetting>(`/notifications/${id}`, { method: "PATCH", body: patch });
}

export function getNotificationInbox(limit = 50): Promise<NotificationInboxPage> {
  return api<NotificationInboxPage>(`/notifications/inbox?limit=${limit}`);
}

export function markNotificationRead(
  id: string,
): Promise<{ id: string; read: boolean; read_at: string }> {
  return api(`/notifications/inbox/${encodeURIComponent(id)}/read`, { method: "POST" });
}

export function markAllNotificationsRead(): Promise<{ marked: number; unread_count: number }> {
  return api("/notifications/inbox/read-all", { method: "POST" });
}

/** React Query key shared by the AppShell badge and the Dashboard banner so a
 *  single fetch serves both. */
export const INBOX_QUERY_KEY = ["notifications", "inbox"] as const;

/** AppShell badge text: empty at zero (badge hidden), capped at "9+". */
export function unreadBadgeLabel(count: number): string {
  if (count <= 0) return "";
  return count > 9 ? "9+" : String(count);
}

/** Dashboard critical-alert banner selection (docs/35): unread
 *  `budget_threshold` items at `critical` severity. The server already omits
 *  every `budget_threshold` row when that toggle is OFF, so an empty inbox
 *  page yields no banner — the gate is server-side, this only narrows. */
export function criticalBudgetItems(
  page: NotificationInboxPage | undefined,
): NotificationInboxItem[] {
  return (page?.items ?? []).filter(
    (item) => item.kind === "budget_threshold" && item.severity === "critical" && !item.read,
  );
}
