import { api } from "./client";

// acb (docs/02, docs/06): non-registered ACB pools + full rebuild.
// Pools are derived state; writes go through the engine — POST /acb/rebuild
// exists for repairs/backfills only.

export interface AcbPool {
  id: number;
  account_id: number;
  security_id: number;
  symbol: string;
  /** Decimal serialized as string. */
  quantity: string;
  total_acb_cad: string;
  updated_at: string;
}

export interface AcbPoolPage {
  items: AcbPool[];
  total: number;
  limit: number;
  offset: number;
}

export async function listAcbPools(filters: {
  account_id?: number | null;
  security_id?: number | null;
  limit?: number;
  offset?: number;
} = {}): Promise<AcbPoolPage> {
  const params = new URLSearchParams();
  if (filters.limit !== undefined) params.set("limit", String(filters.limit));
  if (filters.offset !== undefined) params.set("offset", String(filters.offset));
  if (filters.account_id != null) params.set("account_id", String(filters.account_id));
  if (filters.security_id != null) params.set("security_id", String(filters.security_id));
  const qs = params.toString();
  return api<AcbPoolPage>(`/acb${qs ? `?${qs}` : ""}`);
}

/** Rebuild ALL of the user's non-registered pools from trades (docs/02 invariant). */
export async function rebuildAcb(): Promise<void> {
  await api<null>("/acb/rebuild", { method: "POST" });
}
