# 26 — Spending Alerts

Proactive notifications when spending approaches or exceeds budget limits.

## Context

Budget tracking (M25) shows progress bars, but users don't get notified unless they check the dashboard. Spending alerts push notifications when spending crosses thresholds, catching overspending before it's too late.

## Decisions

1. **Threshold-based, not AI.** Simple percentage thresholds (50%, 80%, 100%) on budget categories. No machine learning — keep it predictable and debuggable.

2. **Global thresholds.** Fixed thresholds at 80% (warning) and 100% (critical) for all budgets. Per-budget customization is a future enhancement.

3. **In-app notifications, not email.** Alerts appear in the notification bell and on the Dashboard. Email integration is a future enhancement.

4. **Daily dedup.** Each alert fires at most once per day per budget to avoid spam.

5. **Computed on access.** Alerts are checked when the user visits the budgets/alerts endpoint. No background job needed.

6. **Window follows the budget period (M47, docs/29).** Monthly budgets are
   evaluated over the current calendar month; yearly budgets over the current
   calendar year — a yearly budget is never compared against one month of
   spending. Spending sums use the shared committed-only staging filter
   (`app/services/txn_filters.py`, docs/29 F1).

7. **Alert history rows clamp `actual_pct` at 999.99 (M47).**
   `spending_alerts.actual_pct` is `Numeric(5,2)`; a >10x overspend would
   overflow the column as a bare 500, so the stored history row is clamped
   (the budget's own displayed `pct_used` stays exact).

8. **One number, two surfaces (M61 #3 / M65).** `_check_alerts` reads spend
   through `app/services/budgets.py::budget_spent` — the exact helper
   `GET /budgets` uses (signed reporting-currency net spend, floor 0,
   committed rows only, unresolved-fx excluded). It previously summed
   `abs(amount_native)` over its own `<= today` window, which counted refunds
   as spending, billed a CAD budget with raw USD face value, and disagreed with
   the card it was reporting on (201.94% on the Dashboard next to 100% on the
   Budgets card). The inbox `body` prints WHOLE percents, ties up, matching
   `Math.round(pct_used)` in the card label (docs/35 §1).

   **Rounding (resolved).** The shared helper's `spent` — and the `carried_in`
   it feeds — is quantized `ROUND_HALF_EVEN`, stated explicitly through the one
   canonical money helper `app.tax.acb.cents` (docs/02 Conventions) rather than
   left to the process-wide decimal context as it was. That is preserved, not
   corrected: the browser mirror `frontend/src/lib/s3Engine.ts::budgetsProgress`
   documents this exact mode and
   `frontend/tests/fixtures/s3-vectors.json` is a golden captured from
   `spent_between` over the live stack (docs/33 §S3 parity), so the alert
   generator and the Budgets card continue to print one number, and the client
   stays byte-identical. Changing the mode requires the mirror and the golden
   to move together; `tests/test_money_rounding_single_source.py` pins it.

## Schema

```sql
-- Extend budgets table
ALTER TABLE budgets ADD COLUMN alert_threshold_pct NUMERIC(5,2) DEFAULT 80.00;
ALTER TABLE budgets ADD COLUMN critical_threshold_pct NUMERIC(5,2) DEFAULT 100.00;

-- New table for alert history
CREATE TABLE spending_alerts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    budget_id INTEGER NOT NULL REFERENCES budgets(id),
    alert_type VARCHAR(20) NOT NULL,  -- warning | critical
    threshold_pct NUMERIC(5,2) NOT NULL,
    actual_pct NUMERIC(5,2) NOT NULL,
    spent NUMERIC(12,2) NOT NULL,
    budget_amount NUMERIC(12,2) NOT NULL,
    acknowledged BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
```

## API

- `GET /budgets/alerts` — return unacknowledged alerts for the current period
- `POST /budgets/alerts/{id}/acknowledge` — mark alert as read
- `PATCH /budgets/{id}` — update alert thresholds (extends existing endpoint)

## Frontend

- **Dashboard** — alert banner at top when critical alerts exist
- **Budgets page** — threshold sliders in budget edit form
- **Notification bell (AppShell)** — M51 (docs/35) shipped it: the badge shows the INBOX `unread_count` (`GET /notifications/inbox`), not `spending_alerts.acknowledged`. Read state lives in `notification_inbox_reads` and is independent of `acknowledged`, which still gates only `GET /budgets/alerts` (that endpoint has no frontend consumer).

## Acceptance criteria

- [ ] Alert triggers at configured thresholds
- [ ] Daily dedup prevents spam
- [ ] Alerts persist across page loads until acknowledged
- [ ] Threshold customization works per budget
- [ ] Dashboard shows critical alert banner
- [ ] Notification bell shows unread count

**M51 (docs/35) delivered both UI items:** the Dashboard banner renders unread
`budget_threshold` / `severity=critical` inbox items (so it obeys the
`budget_threshold` toggle server-side), and the AppShell bell badge shows the
inbox `unread_count`. Their read state is `notification_inbox_reads`, not
`spending_alerts.acknowledged`.

## Priority: medium
## Effort: 2 days
## Dependencies: M25 (budgets), M31 (notifications)
