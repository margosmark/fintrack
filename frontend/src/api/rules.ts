// rules — CRUD (docs/08); match types mirror backend RuleMatchType (docs/02).
import { api } from "./client";
import type { Tag } from "./tags";

export type RuleMatchType =
  | "regex"
  | "exact_payee"
  | "contains"
  | "amount_equals"
  | "amount_gt"
  | "amount_lt"
  | "amount_between";

export interface Rule {
  id: number;
  user_id: number;
  match_type: RuleMatchType;
  pattern: string;
  priority: number;
  category_id: number;
  enabled: boolean;
  tag_action: string;
  stop_on_match: boolean;
  tags: Tag[];
}

export interface RulePage {
  items: Rule[];
  total: number;
  limit: number;
  offset: number;
}

export interface RulePayload {
  match_type: RuleMatchType;
  pattern: string;
  priority: number;
  category_id: number;
  enabled?: boolean;
  tag_ids?: number[];
  tag_action?: string;
  stop_on_match?: boolean;
}

export async function listRules(limit = 50, offset = 0): Promise<RulePage> {
  return api<RulePage>(`/rules?limit=${limit}&offset=${offset}`);
}

export function createRule(payload: RulePayload): Promise<Rule> {
  return api<Rule>("/rules", { method: "POST", body: payload });
}

export function updateRule(ruleId: number, patch: Partial<RulePayload>): Promise<Rule> {
  return api<Rule>(`/rules/${ruleId}`, { method: "PATCH", body: patch });
}

export function deleteRule(ruleId: number): Promise<void> {
  return api<void>(`/rules/${ruleId}`, { method: "DELETE" }).then(() => undefined);
}

export type RuleApplyMode = "all" | "range" | "forward";

export interface RulePreviewItem {
  id: number;
  date: string;
  payee: string;
  amount_native: string;
  category_id: number | null;
  category_name: string | null;
}

export interface RulePreview {
  matched_count: number;
  assigned_skipped: number;
  items: RulePreviewItem[];
}

export interface RuleApplyResult {
  rule_id: number;
  mode: string;
  matched_count: number;
  applied_count: number;
  note: string | null;
}

// M137 P1-2 (docs/33 §Server-first computing): `POST /rules/preview` is
// MOUNTED again and this is a thin read. The verdict is the SERVER's answer,
// from the same engine `POST /rules/{id}/apply` writes with, so a preview and
// the apply it precedes can never disagree about what matched.
//
// M160 (P2-5) retired the `previewRule` client twin with `s3Engine` — the
// server route is the only path now, and the captured server JSON it used to
// be golden-tested against became the server-route replay pin
// (`backend/tests/test_m160_replay_s3.py`). The 422 `{detail, code}`
// envelopes are unchanged, so `apiErrorMessage` renders the same text.
export function previewRule(body: {
  match_type: RuleMatchType;
  pattern: string;
  from?: string;
  to?: string;
  only_unassigned?: boolean;
  limit?: number;
}): Promise<RulePreview> {
  return api<RulePreview>("/rules/preview", { method: "POST", body });
}

export function applyRule(
  ruleId: number,
  body: { mode: RuleApplyMode; from?: string; to?: string; overwrite?: boolean },
): Promise<RuleApplyResult> {
  return api<RuleApplyResult>(`/rules/${ruleId}/apply`, { method: "POST", body });
}
