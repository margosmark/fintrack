import { apiForm } from "./client";
// Net-worth file import (docs/58 §B): preview → commit. Both routes are
// multipart, so like the statement upload this module posts its own FormData
// through apiForm()'s refresh path on a 401. Preview writes nothing; commit
// applies the same plan in one transaction.

/** One account the file would touch. */
export interface NetWorthAccountPlan {
  name: string;
  source_type: string | null;
  mapped_type: string | null;
  action: "create" | "reuse" | "unmapped";
  existing_id: number | null;
  file_balance: string;
  current_balance: string | null;
  currency: string;
  reason: string | null;
  notes: string[];
}

/** Something the file carries that was NOT imported — never silent. */
export interface NetWorthSkipItem {
  kind: string;
  name: string;
  reason: string;
}

export interface NetWorthWarning {
  code: string;
  detail: string;
}

export interface NetWorthCounts {
  accounts: number;
  accounts_create: number;
  accounts_reuse: number;
  accounts_unmapped: number;
  balance_rows_collapsed: number;
  progress_snapshots_skipped: number;
  assets_skipped: number;
}

export interface NetWorthPlan {
  source_format: "projectionlab_json" | "balance_csv";
  file_name: string;
  currency: string;
  counts: NetWorthCounts;
  accounts: NetWorthAccountPlan[];
  skipped: NetWorthSkipItem[];
  warnings: NetWorthWarning[];
  can_commit: boolean;
}

export interface NetWorthCommitResult extends NetWorthPlan {
  created: number;
  reused: number;
}

export interface NetWorthOptions {
  currency?: string;
  /** Label for a single-account file whose CSV has no Account column. */
  accountName?: string;
}

function buildForm(file: File, options: NetWorthOptions): FormData {
  const form = new FormData();
  form.append("file", file);
  if (options.currency) form.append("currency", options.currency);
  if (options.accountName) form.append("account_name", options.accountName);
  return form;
}

/** Read-only: returns the full plan and writes nothing. */
export function previewNetWorth(
  file: File,
  options: NetWorthOptions = {},
): Promise<NetWorthPlan> {
  return apiForm<NetWorthPlan>("/imports/networth/preview", buildForm(file, options));
}

/** Maps AND applies in one transaction: existing accounts are never overwritten. */
export function commitNetWorth(
  file: File,
  options: NetWorthOptions = {},
): Promise<NetWorthCommitResult> {
  return apiForm<NetWorthCommitResult>("/imports/networth/commit", buildForm(file, options));
}
