import { api } from "./client";

// Plain-English assistant (M188, docs/57). The model proposes, the server
// validates and writes; this module is only the HTTP surface — one shared
// `api()` client, no key, no upstream URL anywhere in the bundle.

/** The CLOSED mutation registry (docs/57 §2) — grows only via docs/57 first. */
export type ProposalKind =
  | "account.update"
  | "transaction.update"
  | "forecast_scenario.update";

export type ProposalStatus = "pending" | "applied" | "dismissed" | "undone";

export interface Proposal {
  id: number;
  question: string;
  answer: string;
  kind: ProposalKind;
  target: string;
  target_label: string;
  summary: string;
  /** Changed fields only — the Now vs With-change delta the card renders. */
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  status: ProposalStatus;
  created_at: string;
  applied_at: string | null;
  undone_at: string | null;
  apply_audit_id: number | null;
  undo_audit_id: number | null;
}

export interface AskResponse {
  answer: string;
  proposal: Proposal | null;
}

export interface ProposalList {
  items: Proposal[];
}

/** POST /assistant/ask — one turn: answer + optional pending proposal. */
export function askAssistant(question: string): Promise<AskResponse> {
  return api<AskResponse>("/assistant/ask", {
    method: "POST",
    body: { question },
  });
}

/** POST /assistant/proposals/{id}/apply — pending → applied (409 on drift). */
export function applyProposal(id: number): Promise<Proposal> {
  return api<Proposal>(`/assistant/proposals/${id}/apply`, { method: "POST" });
}

/** POST /assistant/proposals/{id}/dismiss — pending → dismissed, no write. */
export function dismissProposal(id: number): Promise<Proposal> {
  return api<Proposal>(`/assistant/proposals/${id}/dismiss`, { method: "POST" });
}

/** POST /assistant/proposals/{id}/undo — applied → undone (round trip). */
export function undoProposal(id: number): Promise<Proposal> {
  return api<Proposal>(`/assistant/proposals/${id}/undo`, { method: "POST" });
}

/** GET /assistant/history?limit=50 — applied/undone, newest first (docs/57 §4). */
export function listHistory(limit = 50): Promise<ProposalList> {
  return api<ProposalList>(`/assistant/history?limit=${limit}`);
}

/** GET /assistant/proposals?status=… — the open list (default pending). */
export function listProposals(status: ProposalStatus = "pending"): Promise<ProposalList> {
  return api<ProposalList>(`/assistant/proposals?status=${status}`);
}
