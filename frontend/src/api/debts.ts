// debts — debt tracking with payoff projections.
import { api } from "./client";

export interface DebtPublic {
  id: number;
  user_id: number;
  account_id: number;
  name: string;
  debt_type: string;
  original_amount: string | null;
  interest_rate: string | null;
  minimum_payment: string | null;
  payment_frequency: string | null;
  start_date: string | null;
  end_date: string | null;
  notes: string | null;
}

export interface DebtPayload {
  name: string;
  account_id: number;
  debt_type: string;
  original_amount?: number;
  interest_rate?: number;
  minimum_payment?: number;
  payment_frequency?: string;
}

export interface DebtProjection {
  debt_id: number;
  name: string;
  debt_type: string;
  balance: string;
  interest_rate: string | null;
  minimum_payment: string | null;
  months: number | null;
  total_interest: string | null;
  total_paid: string | null;
}

export function listDebts(): Promise<DebtPublic[]> {
  return api<DebtPublic[]>("/debts");
}

export function createDebt(payload: DebtPayload): Promise<DebtPublic> {
  return api<DebtPublic>("/debts", { method: "POST", body: payload });
}

export function updateDebt(id: number, patch: Partial<DebtPayload>): Promise<DebtPublic> {
  return api<DebtPublic>(`/debts/${id}`, { method: "PATCH", body: patch });
}

export function deleteDebt(id: number): Promise<void> {
  return api<void>(`/debts/${id}`, { method: "DELETE" }).then(() => undefined);
}

export function debtProjection(): Promise<{ debts: DebtProjection[]; total_debt: string; total_interest: string }> {
  return api("/debts/projection");
}
