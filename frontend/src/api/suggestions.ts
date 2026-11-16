// suggestions — LLM categorization queue + pipeline trigger (docs/04, docs/08).
import { api } from "./client";

export type SuggestionStatus = "suggested" | "accepted" | "rejected";

/** Mirrors `SuggestionPublic`; payee/category_name are joined server-side. */
export interface Suggestion {
  id: number;
  transaction_id: number;
  payee: string;
  proposed_category_id: number;
  category_name: string;
  confidence: number;
  status: SuggestionStatus;
  model_used: string;
}

export interface SuggestionPage {
  items: Suggestion[];
  total: number;
  limit: number;
  offset: number;
}

export async function listSuggestions(
  status: SuggestionStatus = "suggested",
  limit = 50,
  offset = 0,
): Promise<SuggestionPage> {
  return api<SuggestionPage>(
    `/suggestions?status=${status}&limit=${limit}&offset=${offset}`,
  );
}

/** Applies the proposed category and writes a learned-map entry (docs/04). */
export function acceptSuggestion(suggestionId: number): Promise<Suggestion> {
  return api<Suggestion>(`/suggestions/${suggestionId}/accept`, { method: "POST" });
}

export function rejectSuggestion(suggestionId: number): Promise<Suggestion> {
  return api<Suggestion>(`/suggestions/${suggestionId}/reject`, { method: "POST" });
}

export interface RunOptions {
  from?: string;
  to?: string;
  transaction_ids?: number[];
}

/** Kicks the categorization pipeline (docs/04): rules + learned map apply
 *  inline; uncategorized remainder is batched to the LLM in the background. */
export async function runSuggestions(
  opts?: RunOptions,
): Promise<{ queued: number }> {
  const hasOpts = opts && Object.keys(opts).length > 0;
  return api<{ queued: number }>("/suggestions/run", {
    method: "POST",
    ...(hasOpts ? { body: JSON.stringify(opts) } : {}),
  });
}
