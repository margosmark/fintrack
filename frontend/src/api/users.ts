import { api } from "./client";
import type { AuthResponse, DashboardPreset, SavedReportView, User } from "../types";

// users/me partial update (docs/08): llm_api_key is WRITE-ONLY — accepted on
// this PATCH, never returned by any read (docs/02).

export type LlmProvider = "ollama" | "openai_compatible";

export interface UserUpdateInput {
  privacy_mode?: boolean;
  currency_tracker_enabled?: boolean;
  llm_provider?: LlmProvider;
  llm_base_url?: string | null;
  llm_model?: string | null;
  /** Write-only: send only when the user typed a new key. */
  llm_api_key?: string;
  /** E2EE Phase 2 (docs/33): require ciphertext on new writes. */
  encryption_required?: boolean;
  /** M97 dashboard layout preset; an unknown value is a 422 validation_error. */
  dashboard_preset?: DashboardPreset;
  /** M108 P2: the WHOLE list — rename and delete are client-side list edits,
   *  so one PATCH is the only write. An unknown tab / state key / a duplicate
   *  id is a 422 validation_error (docs/09 §Saved report views). */
  saved_report_views?: SavedReportView[];
  /** M174: the WHOLE list of tag ids that mark recurring intent — one PATCH,
   *  `[]` turns the feature off. An id the caller does not own is a
   *  422 `invalid_recurring_intent_tag_ids` (docs/09 §Tags that mark recurring). */
  recurring_intent_tag_ids?: number[];
}

export async function updateMe(patch: UserUpdateInput): Promise<User> {
  return api<User>("/users/me", { method: "PATCH", body: patch });
}

export interface LlmTestInput {
  llm_provider?: LlmProvider;
  llm_base_url?: string | null;
  llm_model?: string | null;
  /** Candidate key; only send when testing a freshly typed value. */
  llm_api_key?: string;
}

export async function testLlm(
  input: LlmTestInput,
): Promise<{ ok: boolean; model: string; provider: string }> {
  return api<{ ok: boolean; model: string; provider: string }>("/users/me/llm-test", {
    method: "POST",
    body: input,
  });
}


export interface ProfileUpdateInput {
  name?: string;
  email?: string;
  current_password?: string;
}
export interface ChangePasswordInput {
  current_password: string;
  new_password: string;
}
export interface DeleteAccountInput {
  current_password: string;
}

export async function updateProfile(input: ProfileUpdateInput): Promise<User> {
  return api<User>("/users/me/profile", { method: "PATCH", body: input });
}
export async function changePassword(input: ChangePasswordInput): Promise<AuthResponse> {
  return api<AuthResponse>("/users/me/change-password", { method: "POST", body: input });
}
export async function deleteAccount(input: DeleteAccountInput): Promise<void> {
  return api<void>("/users/me", { method: "DELETE", body: input });
}
