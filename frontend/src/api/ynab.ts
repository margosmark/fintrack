import { apiForm } from "./client";
// YNAB migration (docs/38): preview → commit. Both routes are multipart, so
// like the statement upload this module posts its own FormData through
// apiForm()'s refresh path on a 401.

/** A source row (or the whole file, `index: null`) the migration refused. */
export interface YnabIssue {
  code: string;
  reason: string;
  index: number | null;
  detail: string | null;
}

export type YnabAction = "create" | "reuse" | "unmapped" | "skipped";

export interface YnabAccountPlan {
  ynab_id: string;
  name: string;
  ynab_type: string;
  mapped_type: string | null;
  action: YnabAction;
  existing_id: number | null;
  currency: string;
  closed: boolean;
  on_budget: boolean;
  type_assumed: boolean;
  reason: string | null;
}

export interface YnabGroupPlan {
  ynab_id: string;
  name: string;
  action: YnabAction;
  existing_id: number | null;
  tag_name: string | null;
  tag_action: YnabAction | null;
  hidden: boolean;
}

export interface YnabCategoryPlan {
  ynab_id: string;
  name: string;
  group: string | null;
  group_ynab_id: string | null;
  action: YnabAction;
  existing_id: number | null;
}

export interface YnabTargetPlan {
  category_ynab_id: string;
  category_name: string;
  goal_type: string;
  kind: "budget" | "goal" | "unsupported";
  amount: string | null;
  period: string | null;
  target_date: string | null;
  action: YnabAction;
  reason: string | null;
}

/** One importable row: exactly what the commit will write. */
export interface YnabRowPlan {
  index: number;
  date: string;
  amount: string;
  currency: string;
  account: string | null;
  payee: string;
  description: string;
  category: string | null;
  group: string | null;
  tag: string | null;
  cleared: string | null;
  transfer: boolean;
  cad_amount: string | null;
  duplicate: boolean | null;
}

export interface YnabTransactionPlan {
  total: number;
  importable: number;
  duplicates: number;
  duplicates_pending: number;
  transfer_pairs: number;
  rows: YnabRowPlan[];
  rows_truncated: boolean;
  unmappable: YnabIssue[];
  warnings: YnabIssue[];
  warnings_truncated: boolean;
}

export interface YnabCounts {
  accounts_create: number;
  accounts_reuse: number;
  accounts_unmapped: number;
  groups_create: number;
  groups_reuse: number;
  categories_create: number;
  categories_reuse: number;
  tags_create: number;
  tags_reuse: number;
  budgets_create: number;
  budgets_reuse: number;
  goals_create: number;
  goals_reuse: number;
  targets_unsupported: number;
}

export interface YnabMigrationPlan {
  source_format: "json" | "csv";
  budget_name: string;
  currency: string;
  date_format: string | null;
  accounts: YnabAccountPlan[];
  category_groups: YnabGroupPlan[];
  categories: YnabCategoryPlan[];
  targets: YnabTargetPlan[];
  transactions: YnabTransactionPlan;
  notes: YnabIssue[];
  counts: YnabCounts;
}

export interface YnabBatchResult {
  batch_id: number;
  account_id: number;
  account_name: string;
  imported: number;
  duplicates_skipped: number;
  unresolved: number;
}

export interface YnabCommitResult {
  plan: YnabMigrationPlan;
  batches: YnabBatchResult[];
  net_new: number;
  duplicates_skipped: number;
}

/** Optional overrides — all four matter only for a register CSV. */
export interface YnabOptions {
  /** Account that takes CSV rows carrying no Account column. */
  accountId?: number | null;
  /** Account type assumed for CSV rows (a CSV register has no type). */
  accountType?: string | null;
  currency?: string | null;
  /** strptime pattern pinning CSV date order, e.g. "%d/%m/%Y". */
  dateFormat?: string | null;
}

function toForm(file: File, options: YnabOptions): FormData {
  const form = new FormData();
  form.append("file", file);
  if (options.accountId) form.append("account_id", String(options.accountId));
  if (options.accountType) form.append("account_type", options.accountType);
  if (options.currency) form.append("currency", options.currency);
  if (options.dateFormat) form.append("date_format", options.dateFormat);
  return form;
}

/** Full mapping of the export — writes nothing. */
export function previewYnab(file: File, options: YnabOptions = {}): Promise<YnabMigrationPlan> {
  return apiForm<YnabMigrationPlan>("/imports/ynab/preview", toForm(file, options));
}

/** Applies the plan: accounts, categories, tags, targets, then staged batches. */
export function commitYnab(file: File, options: YnabOptions = {}): Promise<YnabCommitResult> {
  return apiForm<YnabCommitResult>("/imports/ynab/commit", toForm(file, options));
}
