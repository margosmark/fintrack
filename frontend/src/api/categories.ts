// categories — CRUD (docs/08); tree via self-referencing parent_id (docs/02).
import { api } from "./client";

export interface Category {
  id: number;
  user_id: number;
  name: string;
  parent_id: number | null;
  is_system: boolean;
}

export interface CategoryPage {
  items: Category[];
  total: number;
  limit: number;
  offset: number;
}

export interface CategoryPayload {
  name: string;
  parent_id?: number | null;
}

export async function listCategories(limit = 50, offset = 0): Promise<CategoryPage> {
  return api<CategoryPage>(`/categories?limit=${limit}&offset=${offset}`);
}

export function createCategory(payload: CategoryPayload): Promise<Category> {
  return api<Category>("/categories", { method: "POST", body: payload });
}

export function updateCategory(
  categoryId: number,
  patch: Partial<CategoryPayload>,
): Promise<Category> {
  return api<Category>(`/categories/${categoryId}`, { method: "PATCH", body: patch });
}

export function deleteCategory(categoryId: number): Promise<void> {
  return api<void>(`/categories/${categoryId}`, { method: "DELETE" }).then(() => undefined);
}
