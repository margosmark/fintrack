// tags — CRUD (docs/08); user-created labels for transactions.
import { api } from "./client";

export interface Tag {
  id: number;
  user_id: number;
  name: string;
}

/** `GET /tags` item (M104): usage the management surface shows before a
 * delete can be refused with 409 `tag_in_use`. */
export interface TagWithUsage extends Tag {
  transaction_count: number;
  rule_count: number;
}

export interface TagPage {
  items: TagWithUsage[];
  total: number;
  limit: number;
  offset: number;
}

export interface TagPayload {
  name: string;
}

export async function listTags(limit = 200, offset = 0): Promise<TagPage> {
  return api<TagPage>(`/tags?limit=${limit}&offset=${offset}`);
}

export function createTag(payload: TagPayload): Promise<Tag> {
  return api<Tag>("/tags", { method: "POST", body: payload });
}

export function updateTag(tagId: number, patch: Partial<TagPayload>): Promise<Tag> {
  return api<Tag>(`/tags/${tagId}`, { method: "PATCH", body: patch });
}

export function deleteTag(tagId: number): Promise<void> {
  return api<void>(`/tags/${tagId}`, { method: "DELETE" }).then(() => undefined);
}

export interface TagMergePayload {
  source_ids: number[];
  target_id: number;
}

export interface TagMergeResult {
  target: Tag;
  merged_tag_ids: number[];
}

/** Fold `source_ids` into `target_id` (M104): re-points the junction rows at
 * the survivor and deletes the sources — the fix for a duplicated typo tag,
 * which a rename cannot be (`duplicate_tag`). */
export function mergeTags(payload: TagMergePayload): Promise<TagMergeResult> {
  return api<TagMergeResult>("/tags/merge", { method: "POST", body: payload });
}
