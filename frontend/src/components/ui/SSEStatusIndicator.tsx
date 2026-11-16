import type { StreamStatus } from "../../hooks/usePriceStream";

const STATUS_CONFIG: Record<StreamStatus, { color: string; label: string }> = {
  connected: { color: "bg-gain", label: "Stream connected" },
  connecting: { color: "bg-warn", label: "Connecting\u2026" },
  error: { color: "bg-danger", label: "Connection error" },
  disconnected: { color: "bg-muted", label: "Offline" },
};

export function SSEStatusIndicator({
  status,
  lastEventAt,
  pricesStale = false,
}: {
  status: StreamStatus;
  lastEventAt: Date | null;
  /** m61 #7 / M65: true when any displayed price row is older than 24h
   *  (price_age_days > 0 — day-granular ages; a >24h age can never be dated
   *  today). The indicator then reports "prices stale" instead of a
   *  misleading neutral "Stream connected"; a red "Connection error" still
   *  wins so the actionable transport failure stays visible. */
  pricesStale?: boolean;
}) {
  const stale = pricesStale && status !== "error";
  const { color, label } = stale
    ? { color: "bg-warn", label: "prices stale" }
    : STATUS_CONFIG[status];
  return (
    <div
      className="flex items-center gap-1.5 text-xs"
      title={lastEventAt ? `Last stream message: ${lastEventAt.toLocaleTimeString()}` : "No stream message"}
    >
      <div
        className={`size-2 rounded-full ${color} ${status === "connected" ? "animate-pulse" : ""}`}
      />
      <span className="text-muted">{label}</span>
    </div>
  );
}
