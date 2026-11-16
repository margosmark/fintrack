// vault — wrapped-DEK sync across devices (E2EE Phase 4, docs/33).
// The server stores an opaque, passphrase-locked blob per device; these calls
// move it around and never carry key material the server could open.
import { api } from "./client";

export interface VaultEnvelope {
  id: number;
  device_id: string;
  label: string | null;
  /** Opaque base64url bundle; `null` exactly when `revoked_at` is set. */
  wrapped_blob: string | null;
  revoked_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface VaultEnvelopeList {
  /** Active rows only, oldest first. */
  items: VaultEnvelope[];
  /** True when THIS device once had a row and none is active any more. */
  revoked_own_device: boolean;
  active_count: number;
}

export interface VaultEnvelopeCreate {
  device_id: string;
  label?: string;
  wrapped_blob: string;
}

export interface VaultEnvelopeUpdate {
  wrapped_blob: string;
  label?: string | null;
}

export function listEnvelopes(deviceId: string): Promise<VaultEnvelopeList> {
  return api<VaultEnvelopeList>(`/vault/envelopes?device_id=${encodeURIComponent(deviceId)}`);
}

export function createEnvelope(payload: VaultEnvelopeCreate): Promise<VaultEnvelope> {
  return api<VaultEnvelope>("/vault/envelopes", { method: "POST", body: payload });
}

export function replaceEnvelope(
  id: number,
  payload: VaultEnvelopeUpdate,
): Promise<VaultEnvelope> {
  return api<VaultEnvelope>(`/vault/envelopes/${id}`, { method: "PUT", body: payload });
}

export function revokeEnvelope(id: number): Promise<VaultEnvelope> {
  return api<VaultEnvelope>(`/vault/envelopes/${id}/revoke`, { method: "POST" });
}
