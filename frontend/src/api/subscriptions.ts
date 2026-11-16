// subscriptions — detected recurring transactions.
//
// M137 P1-2 (docs/33 §Server-first computing): `GET /subscriptions` is
// MOUNTED again and this is a thin read. M48-S3 deleted the route and had the
// browser detect subscriptions over a FULL-LEDGER download; the detection
// itself never moved — `services/subscriptions.py` kept running behind
// `bill_reminders`' cash-flow calendar — so the route is the same engine
// exposed again, not a rewrite.
//
// M160 (P2-5) retired the `subscriptionsReport` client twin with
// `s3Engine` — the golden `subscriptions.detect` in
// tests/fixtures/s3-vectors.json now pins the ROUTE, replayed by
// `backend/tests/test_m160_replay_s3.py`.
import { api } from "./client";

export interface Subscription {
  payee: string;
  frequency: string;
  avg_amount: string;
  annual_cost: string;
  occurrences: number;
  last_date: string;
  next_expected: string | null;
}

export interface SubscriptionsResponse {
  subscriptions: Subscription[];
  total_monthly: string;
  total_annual: string;
}

export function listSubscriptions(): Promise<SubscriptionsResponse> {
  return api<SubscriptionsResponse>("/subscriptions");
}
