import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { listNotificationSettings, updateNotificationSetting } from "../../api/notifications";
import type { NotificationSetting } from "../../api/notifications";
import { Card, CardTitle } from "../ui/Card";
import { Toggle } from "../ui/Toggle";
import { SkeletonRows } from "../ui/Skeleton";
import { useToast } from "../ui/Toast";

const LABELS: Record<string, string> = {
  large_transaction: "Large transaction alerts",
  low_balance: "Low balance warnings",
  dividend_received: "Dividend received notifications",
  price_alert: "Watchlist price alerts",
  monthly_summary: "Monthly spending summary",
  // M51 generator-backed types (docs/35) — these actually fire.
  budget_threshold: "Budget threshold alerts",
  bill_due: "Bill due reminders",
  tax_loss: "Tax-loss opportunities",
};

const DESCRIPTIONS: Record<string, string> = {
  large_transaction: "Get notified when a transaction exceeds your threshold",
  low_balance: "Alert when account balance drops below a target",
  dividend_received: "Notify when dividends are recorded",
  price_alert: "Alert when watchlist securities hit a target price",
  monthly_summary: "Receive a summary of spending at month end",
  budget_threshold: "Alert when spending crosses a budget's 80% / 100% threshold",
  bill_due: "Alert when a bill enters its reminder window or goes overdue",
  tax_loss: "Surface unrealized losses in your taxable accounts",
};

/** The five M31 types have no generator — say so in the UI, not just in docs
 *  (docs/35: "no hiding"). */
const NO_GENERATOR_TYPES = new Set([
  "large_transaction",
  "low_balance",
  "dividend_received",
  "price_alert",
  "monthly_summary",
]);

export function NotificationsSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data: settings, isLoading } = useQuery({
    queryKey: ["notification-settings"],
    queryFn: listNotificationSettings,
  });

  const toggle = useMutation({
    mutationFn: (s: NotificationSetting) =>
      updateNotificationSetting(s.id, { enabled: !s.enabled }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["notification-settings"] });
      toast.success("Updated", "Notification setting saved.");
    },
  });

  if (isLoading) return <SkeletonRows rows={3} />;

  return (
    <Card className="p-4">
      <CardTitle>Notifications</CardTitle>
      <p className="text-sm text-muted mb-3">Configure which alerts you want to receive.</p>
      <div className="space-y-3">
        {settings?.map((s) => (
          <div key={s.id} className="flex items-center justify-between py-2 border-b border-border/50 last:border-0">
            <div>
              <div className="text-sm font-medium text-text">
                {LABELS[s.alert_type] || s.alert_type}
                {NO_GENERATOR_TYPES.has(s.alert_type) && (
                  <span className="ml-2 align-middle text-[10px] font-normal uppercase tracking-wider text-muted">
                    no generator yet
                  </span>
                )}
              </div>
              <div className="text-xs text-muted">{DESCRIPTIONS[s.alert_type] || ""}</div>
            </div>
            <Toggle
              checked={s.enabled}
              onChange={() => toggle.mutate(s)}
            />
          </div>
        ))}
      </div>
      <p className="mt-3 text-xs text-muted">
        Large transaction, low balance, dividend received, price alert and monthly summary have no
        generator yet — toggling them stores your preference but nothing fires. Budget threshold,
        bill due and tax loss are backed by live generators and feed the bell in the sidebar plus
        the Dashboard alert banner.
      </p>
    </Card>
  );
}
