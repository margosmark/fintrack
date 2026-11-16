// reconciliation — compare imports against bank statements.
import { api } from "./client";

export interface ReconciliationItem {
  id: number;
  account_id: number;
  transaction_id: number | null;
  statement_date: string;
  statement_amount: string;
  statement_description: string;
  status: string;
}

export interface ReconciliationResponse {
  items: ReconciliationItem[];
  summary: {
    total: number;
    matched: number;
    unmatched: number;
    matched_sum: string;
    unmatched_sum: string;
  };
}

export interface StatementEntry {
  date: string;
  /** Decimal on the backend (docs/29); serialized as its raw string. */
  amount: string;
  description?: string;
}

export function getReconciliation(accountId: number): Promise<ReconciliationResponse> {
  return api<ReconciliationResponse>(`/accounts/${accountId}/reconciliation`);
}

export function uploadStatement(accountId: number, items: StatementEntry[]): Promise<{ count: number }> {
  return api(`/accounts/${accountId}/reconciliation/upload`, { method: "POST", body: { items } });
}

export function matchItem(accountId: number, itemId: number, transactionId: number): Promise<ReconciliationItem> {
  return api(`/accounts/${accountId}/reconciliation/match`, {
    method: "POST",
    body: { item_id: itemId, transaction_id: transactionId },
  });
}
