// goals — financial savings targets.
import { api } from "./client";

export interface GoalWithProgress {
  id: number;
  user_id: number;
  name: string;
  target_amount: string;
  target_date: string | null;
  account_id: number | null;
  current_amount_override: string | null;
  current_amount: string;
  progress_pct: number;
  monthly_needed: string | null;
  status: string;
}

export interface GoalPayload {
  name: string;
  target_amount: number;
  target_date?: string;
  current_amount_override?: number;
  account_id?: number;
}

export function listGoals(): Promise<GoalWithProgress[]> {
  return api<GoalWithProgress[]>("/goals");
}

export function createGoal(payload: GoalPayload): Promise<GoalWithProgress> {
  return api<GoalWithProgress>("/goals", { method: "POST", body: payload });
}

export function updateGoal(id: number, patch: Partial<GoalPayload>): Promise<GoalWithProgress> {
  return api<GoalWithProgress>(`/goals/${id}`, { method: "PATCH", body: patch });
}

export function deleteGoal(id: number): Promise<void> {
  return api<void>(`/goals/${id}`, { method: "DELETE" }).then(() => undefined);
}
