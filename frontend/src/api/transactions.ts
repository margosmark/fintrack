// transactions — filtered, paginated list + inline corrections (docs/08).
// Filters AND-compose server-side; `category_id=null` selects uncategorized rows.
// M137 (docs/33 §Server-first computing): the fuzzy `q` parameter is BACK —
// the server thresholds and ranks it (`score desc, id desc`), so the search
// view renders a page the server computed. `lib/searchMatch.ts` stays as the
// E2EE/offline fallback.
import { api } from "./client";
import type { Tag } from "./tags";

/** Cadence the detector can fit (backend `RecurringCadence`). */
export type RecurringCadence = "weekly" | "biweekly" | "four_weekly" | "monthly";

/**
 * Mirrors `TransactionPublic`. The backend serializes Decimal fields as JSON
 * strings (verified against :8000), so monetary values stay strings here.
 */
export interface Transaction {
  id: number;
  account_id: number;
  date: string; // ISO date
  posting_date: string | null;
  raw_description: string;
  normalized_payee: string;
  amount_native: string; // signed, transaction currency; debits negative
  currency: string;
  cad_amount: string | null; // null while no cached fx rate resolved (docs/03)
  category_id: number | null;
  transfer_pair_id: number | null;
  pair_account_name: string | null;
  /** M108 P4 (docs/09 §Receipt images): an image is attached — the flag
   *  only. The bytes move on `GET /transactions/{id}/receipt`, never here. */
  has_receipt: boolean;
  tags: Tag[];
  /** M96: rows the prompt card tagged (docs/09 §Recurring prompt). */
  is_recurring: boolean;
  recurring_cadence: RecurringCadence | null;
  /** The GROUP's next expected occurrence, stored on every tagged row. */
  recurring_next_date: string | null;
  /** M106 (docs/02): manual review state. `reviewed_at === null` IS
   *  "unreviewed" — there is no second boolean that could disagree. */
  reviewed_at: string | null;
  reviewed_by: number | null;
  /** E2EE Phase 2 (docs/33): opaque ciphertext + blind index; null = legacy row. */
  payee_cipher: string | null;
  raw_desc_cipher: string | null;
  amount_cipher: string | null;
  cad_amount_cipher: string | null;
  payee_token: string | null;
  /** M107 (docs/44): set on CHILDREN only — the split they belong to. */
  parent_split_id: number | null;
  /** M107: true iff this row has children (a split parent). */
  is_split: boolean;
  /** M107: free text on a row (docs/33 §Server-readable plaintext). */
  note: string | null;
}

/**
 * M107 (docs/44 §4.1): the statement line a split was taken from, with the
 * number of children it has. Parents are NOT in `items` (LEDGER) — they ride
 * alongside so a page can draw group headers without ever being summable.
 */
export type SplitParentPublic = Transaction & { child_count: number };

/** The header shape a grouping component consumes — money may be masked. */
export type SplitParentLike = Omit<SplitParentPublic, "amount_native" | "cad_amount"> & {
  /** `null` under the partner's privacy mode (docs/43 §9). */
  amount_native: string | null;
  cad_amount: string | null;
};

export interface TransactionPage {
  items: Transaction[];
  total: number;
  limit: number;
  offset: number;
  /** M107: parents of the children in `items`, `date desc, id desc`. */
  split_parents: SplitParentPublic[];
  /** M137: rows the request left out because `hide_encrypted` was set (a
   *  locked vault). 0 unless asked — the payload is otherwise unchanged. */
  withheld?: number;
}

/** M107 (docs/44 §4.1): GET/POST/PUT `/transactions/{id}/split` body shape. */
export interface TransactionSplit {
  parent: SplitParentPublic;
  /** Complete group, `id ASC`. */
  children: Transaction[];
  split_checksum: string;
  /** Recomputed on read — integrity without a write. */
  sum_ok: boolean;
}

/** One leg of a split. Money is a Decimal string, never a number (AGENTS §2). */
export interface SplitChildInput {
  amount_native: string;
  category_id?: number | null;
  note?: string | null;
  tag_ids?: number[];
  /** Required per child when `users.encryption_required` (docs/33 F-10). */
  amount_cipher?: string | null;
  cad_amount_cipher?: string | null;
}

export interface TransactionFilters {
  accountId?: number;
  /** A category id, or `"uncategorized"` which sends `category_id=null`. */
  categoryId?: number | "uncategorized";
  /** Inclusive ISO dates. */
  from?: string;
  to?: string;
  /** Case-insensitive contains over normalized_payee OR raw_description. */
  payee?: string;
  amountMin?: number;
  amountMax?: number;
  /** Sign convention: debits negative. `paid` = amount_native < 0. */
  direction?: "paid" | "received";
  /** restricts to transactions carrying the tag. */
  tagId?: number;
  /** M104: multi-tag filter — a row matches only if it carries EVERY id
   * (backend `?tag_ids=` repeated, AND-composed; docs/08 §Transactions). */
  tagIds?: number[];
  /** M106: `false` IS the "Needs review" queue, `true` its inverse. */
  reviewed?: boolean;
  /** M137 (docs/33 §Server-first computing): fuzzy payee text. Thresholds
   *  the scope (0.3) and ranks it (`score desc, id desc`) server-side. */
  q?: string;
}

export interface TransactionCreate {
  account_id: number;
  date: string;
  posting_date?: string | null;
  raw_description: string;
  normalized_payee?: string | null;
  amount_native: string;
  currency?: string | null;
  category_id?: number | null;
  payee_cipher?: string | null;
  raw_desc_cipher?: string | null;
  amount_cipher?: string | null;
  cad_amount_cipher?: string | null;
  payee_token?: string | null;
}

export interface TransactionPatch {
  account_id?: number;
  date?: string;
  posting_date?: string | null;
  raw_description?: string;
  normalized_payee?: string | null;
  amount_native?: string;
  currency?: string | null;
  category_id?: number | null;
  payee_cipher?: string | null;
  raw_desc_cipher?: string | null;
  amount_cipher?: string | null;
  cad_amount_cipher?: string | null;
  payee_token?: string | null;
}

/**
 * The ONE mapping from `TransactionFilters` to the server's filter contract
 * (M125, docs/08 §Transactions).
 *
 * `buildQuery` turns it into `GET /transactions`' query string and
 * `buildBulkFilter` into the `POST /transactions/bulk/*` body, so a filter the
 * page builds can never mean two different things depending on which route it
 * travels to. A new filter field belongs HERE, once — both routes pick it up.
 *
 * Values are `string | number | boolean | string[]`: query strings take the
 * strings, JSON bodies take the typed values (`category_id` is the string
 * `"null"` on the wire either way — uncategorized).
 */
function filterEntries(
  filters: TransactionFilters,
): Array<[string, string | number | boolean | number[]]> {
  const entries: Array<[string, string | number | boolean | number[]]> = [];
  if (filters.accountId !== undefined) entries.push(["account_id", filters.accountId]);
  if (filters.categoryId !== undefined) {
    entries.push([
      "category_id",
      filters.categoryId === "uncategorized" ? "null" : filters.categoryId,
    ]);
  }
  if (filters.from !== undefined) entries.push(["from", filters.from]);
  if (filters.to !== undefined) entries.push(["to", filters.to]);
  if (filters.payee !== undefined) entries.push(["payee", filters.payee]);
  if (filters.amountMin !== undefined) entries.push(["amount_min", filters.amountMin]);
  if (filters.amountMax !== undefined) entries.push(["amount_max", filters.amountMax]);
  if (filters.direction !== undefined) entries.push(["direction", filters.direction]);
  if (filters.tagId !== undefined) entries.push(["tag_id", filters.tagId]);
  if (filters.tagIds !== undefined) entries.push(["tag_ids", filters.tagIds]);
  if (filters.reviewed !== undefined) entries.push(["reviewed", filters.reviewed]);
  if (filters.q !== undefined) entries.push(["q", filters.q]);
  return entries;
}

function buildQuery(
  filters: TransactionFilters,
  paging: { limit?: number; offset?: number },
): string {
  const params = new URLSearchParams();
  for (const [key, value] of filterEntries(filters)) {
    if (Array.isArray(value)) {
      // `tag_ids` repeats — the AND-composed multi-tag form (docs/08).
      for (const item of value) params.append(key, String(item));
    } else {
      params.set(key, String(value));
    }
  }
  if (paging.limit !== undefined) params.set("limit", String(paging.limit));
  if (paging.offset !== undefined) params.set("offset", String(paging.offset));
  const qs = params.toString();
  return qs === "" ? "" : `?${qs}`;
}

/** The page's filter as the JSON body a `POST /transactions/bulk/*` takes. */
export function buildBulkFilter(filters: TransactionFilters): BulkFilterBody {
  const body: Record<string, unknown> = {};
  for (const [key, value] of filterEntries(filters)) body[key] = value;
  return body as BulkFilterBody;
}

export async function listTransactions(
  filters: TransactionFilters = {},
  paging: { limit?: number; offset?: number } = {},
  // M137: a locked-vault SEARCH asks for only the rows it could display.
  // The register itself never sets it — a locked row renders its 🔒 marker
  // in place (docs/33 §Mixed-mode display), it is not hidden from the list.
  opts: { hideEncrypted?: boolean } = {},
): Promise<TransactionPage> {
  const query = buildQuery(filters, paging);
  const hide = opts.hideEncrypted ? `${query === "" ? "?" : "&"}hide_encrypted=true` : "";
  return api<TransactionPage>(`/transactions${query}${hide}`);
}

/**
 * M137 (docs/04 §Similar examples, docs/33 §Server-first computing): the
 * ranked payee examples, RE-mounted server-side — the popover is a thin read
 * of `GET /transactions/similar` instead of a full-ledger download scored in
 * the browser. `encrypted` marks rows whose payee is ciphertext (plaintext
 * on the server, so they always matched — a locked client is what hides them).
 */
export interface SimilarTransaction {
  id: number;
  account_id: number;
  date: string;
  raw_description: string;
  normalized_payee: string;
  amount_native: string;
  currency: string;
  cad_amount: string | null;
  category_id: number | null;
  category_name: string | null;
  score: number;
  encrypted?: boolean;
}

export interface SimilarPage {
  query: string;
  items: SimilarTransaction[];
  /** Matches this request kept; `total + withheld` = every match. */
  total?: number;
  /** Matches left out only because `hide_encrypted` was set. */
  withheld?: number;
}

export interface SimilarQuery {
  q: string;
  /** The row asking for its own family never appears in it (M119). */
  excludeId?: number;
  categorizedFirst?: boolean;
  /** Locked-vault clients ask for what they could display. */
  hideEncrypted?: boolean;
  limit?: number;
}

export function getSimilarTransactions(query: SimilarQuery): Promise<SimilarPage> {
  const params = new URLSearchParams({ q: query.q });
  if (query.excludeId !== undefined) params.set("exclude_id", String(query.excludeId));
  if (query.categorizedFirst !== undefined) {
    params.set("categorized_first", String(query.categorizedFirst));
  }
  if (query.hideEncrypted) params.set("hide_encrypted", "true");
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  return api<SimilarPage>(`/transactions/similar?${params.toString()}`);
}

/**
 * Every row matching `filters`, in list order — the substrate the search view
 * scores locally (docs/33 S5). Pages through the endpoint until `total` is
 * covered: a client-side scorer may never cap its input, or it would rank a
 * subset and silently disagree with the population the server used to rank.
 */
export async function fetchAllTransactions(
  filters: TransactionFilters = {},
  pageSize = 200,
): Promise<Transaction[]> {
  return (await fetchAllTransactionPages(filters, pageSize)).items;
}

/**
 * Same paginate-to-total walk, but keeps every page's `split_parents` — the
 * search view groups the same way the register does (docs/44 §6.3), and a
 * parent is not in `items`, so dropping the envelope would drop the headers.
 */
export async function fetchAllTransactionPages(
  filters: TransactionFilters = {},
  pageSize = 200,
): Promise<{ items: Transaction[]; split_parents: SplitParentPublic[] }> {
  const items: Transaction[] = [];
  const parents = new Map<number, SplitParentPublic>();
  let offset = 0;
  for (;;) {
    const page = await listTransactions(filters, { limit: pageSize, offset });
    items.push(...page.items);
    // Defensive: an older/edge payload without the field still renders rows.
    for (const parent of page.split_parents ?? []) parents.set(parent.id, parent);
    if (page.items.length === 0 || items.length >= page.total) {
      return { items, split_parents: [...parents.values()] };
    }
    offset += page.items.length;
  }
}

// --- M107 splits (docs/44) — POST/PUT/GET/DELETE /transactions/{id}/split ----

/**
 * Create the split: 201 `TransactionSplit`. The server refuses a sum that
 * misses the checksum with `422 split_sum_mismatch` (its `{detail, code}`
 * envelope — the editor prints `detail` verbatim plus the code).
 */
export function createTransactionSplit(
  transactionId: number,
  children: SplitChildInput[],
): Promise<TransactionSplit> {
  return api<TransactionSplit>(`/transactions/${transactionId}/split`, {
    method: "POST",
    body: { children },
  });
}

/** Replace every child — 200 `TransactionSplit` (the Re-split path). */
export function updateTransactionSplit(
  transactionId: number,
  children: SplitChildInput[],
): Promise<TransactionSplit> {
  return api<TransactionSplit>(`/transactions/${transactionId}/split`, {
    method: "PUT",
    body: { children },
  });
}

/**
 * The drilldown a group header opens — always fresh, always recomputing
 * `sum_ok`, and always the COMPLETE group even when a filter hid children.
 */
export function getTransactionSplit(transactionId: number): Promise<TransactionSplit> {
  return api<TransactionSplit>(`/transactions/${transactionId}/split`);
}

/** Merge back — 200 `TransactionPublic`; the children are gone (docs/44 §5). */
export function mergeTransactionSplit(transactionId: number): Promise<Transaction> {
  return api<Transaction>(`/transactions/${transactionId}/split`, { method: "DELETE" });
}

export async function updateTransaction(
  transactionId: number,
  patch: TransactionPatch,
): Promise<Transaction> {
  return api<Transaction>(`/transactions/${transactionId}`, { method: "PATCH", body: patch });
}
export async function createTransaction(input: TransactionCreate): Promise<Transaction> {
  return api<Transaction>("/transactions", { method: "POST", body: input });
}

export async function deleteTransaction(transactionId: number): Promise<void> {
  await api<void>(`/transactions/${transactionId}`, { method: "DELETE" });
}

export async function detectTransferPair(transactionId: number): Promise<Transaction> {
  return api<Transaction>(`/transactions/${transactionId}/detect-transfer`, { method: "POST" });
}

export async function linkTransfer(transactionId: number, pairId: number): Promise<Transaction> {
  return api<Transaction>(`/transactions/${transactionId}/link-transfer`, {
    method: "POST",
    body: { pair_id: pairId },
  });
}

export async function unlinkTransfer(transactionId: number): Promise<Transaction> {
  return api<Transaction>(`/transactions/${transactionId}/unlink-transfer`, { method: "POST" });
}

export interface TransferSuggestion {
  source_id: number;
  source_account_id: number;
  source_account_name: string | null;
  source_amount: string;
  source_date: string;
  source_payee: string;
  candidate_id: number;
  candidate_account_id: number;
  candidate_account_name: string | null;
  candidate_amount: string;
  candidate_date: string;
  candidate_payee: string;
}

export interface TransferSuggestionPage {
  items: TransferSuggestion[];
}

export async function getTransferSuggestions(
  accountId?: number,
): Promise<TransferSuggestionPage> {
  const params = accountId !== undefined ? `?account_id=${accountId}` : "";
  return api<TransferSuggestionPage>(`/transactions/transfer-suggestions${params}`);
}

export async function acceptTransferSuggestion(
  sourceId: number,
  candidateId: number,
): Promise<Transaction> {
  return api<Transaction>("/transactions/accept-transfer-suggestion", {
    method: "POST",
    body: { source_id: sourceId, candidate_id: candidateId },
  });
}
// --- Transaction tags ----------------------------------------------------------

export async function getTransactionTags(transactionId: number): Promise<{ items: Tag[] }> {
  return api<{ items: Tag[] }>(`/transactions/${transactionId}/tags`);
}

export async function setTransactionTags(transactionId: number, tagIds: number[]): Promise<{ items: Tag[] }> {
  return api<{ items: Tag[] }>(`/transactions/${transactionId}/tags`, {
    method: "PUT",
    body: { tag_ids: tagIds },
  });
}

export async function addTransactionTag(transactionId: number, tagId: number): Promise<{ items: Tag[] }> {
  return api<{ items: Tag[] }>(`/transactions/${transactionId}/tags`, {
    method: "POST",
    body: { tag_id: tagId },
  });
}

export async function removeTransactionTag(transactionId: number, tagId: number): Promise<void> {
  await api<void>(`/transactions/${transactionId}/tags/${tagId}`, { method: "DELETE" });
}

// --- Recurring-transactions detection (M96, docs/09 §Recurring prompt) --------

/**
 * Which surface is asking (M138, docs/09 §Bills you probably have).
 *
 * Both the M96 prompt card and the dashboard's subscriptions card read the
 * same endpoint but dismiss INDEPENDENTLY: a `subscriptions` answer must never
 * silence the prompt for the same merchant, and the reverse. The source picks
 * which `recurring_suppressions` keys the route subtracts.
 */
export type RecurringSource = "recurring_prompt" | "subscriptions";

export interface RecurringCandidate {
  group_key: string;
  /**
   * The key THIS surface suppresses under (server-authoritative — the caller
   * never re-derives the merchant normalization). Equals `group_key` for
   * `recurring_prompt`; for `subscriptions` it is the per-payee key that ends
   * `|subscriptions`.
   */
  dismiss_key: string;
  payee: string;
  cadence: RecurringCadence;
  occurrence_count: number;
  transaction_ids: number[];
  monthly_total: string;
  first_date: string;
  last_date: string;
  next_expected_date: string;
}

export interface RecurringCandidatePage {
  /** Already filtered server-side: declined groups and tagged merchants are gone. */
  groups: RecurringCandidate[];
  combined_monthly_total: string;
  /** Groups this scan found but hid because they were declined earlier. */
  suppressed_count: number;
  window_start: string;
}

export interface RecurringAcceptResult {
  updated: number;
  group_key: string;
  cadence: RecurringCadence;
  next_expected_date: string | null;
}

export function getRecurringCandidates(
  source?: RecurringSource,
): Promise<RecurringCandidatePage> {
  // The server's default is `recurring_prompt`, so the M96 card's request is
  // byte-identical to what it has always sent; only the subscriptions card
  // adds the parameter.
  const query = source === undefined ? "" : `?source=${encodeURIComponent(source)}`;
  return api<RecurringCandidatePage>(`/transactions/recurring/candidates${query}`);
}

export function acceptRecurringGroup(payload: {
  group_key: string;
  transaction_ids: number[];
  cadence: RecurringCadence;
}): Promise<RecurringAcceptResult> {
  return api<RecurringAcceptResult>("/transactions/recurring/accept", {
    method: "POST",
    body: payload,
  });
}

/** Decline for good — one idempotent POST, no unsuppress (docs/09). */
export function suppressRecurringGroup(groupKey: string): Promise<void> {
  return api<void>("/transactions/recurring/suppress", {
    method: "POST",
    body: { group_key: groupKey },
  }).then(() => undefined);
}


// --- M106: review state + the per-transaction activity log (docs/02, docs/09) ---

/** The closed set the backend writes (docs/02 §transaction_events). */
export type TransactionEventKind =
  | "created"
  | "edited"
  | "categorized"
  | "tagged"
  | "reviewed"
  | "split"
  | "deleted";

/** One receipt from the append-only log. */
export interface TransactionEvent {
  id: number;
  tx_id: number;
  event_kind: TransactionEventKind;
  actor: number | null;
  /** "you" for your own receipt, the actor's name otherwise. */
  actor_label: string;
  at: string | null;
  /** sha256 over the canonical payload — re-checkable, not decorative. */
  payload_hash: string;
  /** The rendered drawer line: "you set category Groceries". */
  summary: string;
  payload: Record<string, unknown>;
}

export interface TransactionEventPage {
  items: TransactionEvent[];
  total: number;
}

/** Newest first — the drawer reads top-down from the last receipt written. */
export function listTransactionEvents(transactionId: number): Promise<TransactionEventPage> {
  return api<TransactionEventPage>(`/transactions/${transactionId}/events`);
}

/**
 * Mark one row reviewed / unreviewed.
 *
 * The row toggle AND the bulk toolbar drive this one single-row route
 * (docs/09 §Bulk selection toolbar: N rows are N sequential requests), so a
 * bulk run leaves N receipts — exactly like N clicks would.
 */
export function setTransactionReviewed(
  transactionId: number,
  reviewed: boolean,
): Promise<Transaction> {
  return api<Transaction>(`/transactions/${transactionId}/review`, {
    method: "POST",
    body: { reviewed },
  });
}

/**
 * How many rows `reviewed=false` would return in the CURRENT filter scope.
 *
 * Its own request on purpose: `GET /transactions` carries a 4-query budget
 * (docs/34) and the header hint needs the number while the queue filter is
 * off. Same query params as the list, so the chip's number is the list's.
 */
export function getUnreviewedCount(filters: TransactionFilters = {}): Promise<number> {
  const query = buildQuery(filters, {});
  return api<{ count: number }>(`/transactions/unreviewed-count${query}`).then(
    (body) => body.count,
  );
}


// --- M125: bulk over a MATCHED set (docs/08 §Bulk over a matched set) ---------
//
// M96's page-select path still drives the single-row routes one id at a time.
// These five routes are the OTHER path: ONE request whose body carries the
// page's filter plus the ids the user unchecked out of it — never the matched
// set's ids. `buildBulkFilter` is the same mapping `buildQuery` uses, so the
// rows a bulk run selects are the rows the page rendered.

/** The page's filter as a JSON body — mirrors `GET /transactions`' params. */
export interface BulkFilterBody {
  account_id?: number;
  /** An id, or the string `"null"` for "uncategorized". Absent = no filter. */
  category_id?: number | "null";
  from?: string;
  to?: string;
  payee?: string;
  amount_min?: number;
  amount_max?: number;
  direction?: "paid" | "received";
  tag_id?: number;
  tag_ids?: number[];
  payee_token?: string;
  reviewed?: boolean;
}

/** One row the server refused, named (M125). */
export interface BulkSkippedRow {
  id: number;
  label: string;
  code: string | null;
  detail: string;
}

export interface BulkResult {
  /** The SERVER's count of the selection — the number the counter printed. */
  matched: number;
  /** Rows processed without a refusal. */
  updated: number;
  skipped: BulkSkippedRow[];
}

/** Shared body of every bulk route: the filter + the unchecked ids. */
interface BulkSelectionBody {
  filter: BulkFilterBody;
  exclude_ids: number[];
}

function bulkPost(
  action: "recategorize" | "payee" | "review" | "tags" | "delete",
  body: BulkSelectionBody & Record<string, unknown>,
): Promise<BulkResult> {
  return api<BulkResult>(`/transactions/bulk/${action}`, { method: "POST", body });
}

export function bulkRecategorize(
  filters: TransactionFilters,
  excludeIds: readonly number[],
  categoryId: number | null,
): Promise<BulkResult> {
  return bulkPost("recategorize", {
    filter: buildBulkFilter(filters),
    exclude_ids: [...excludeIds],
    category_id: categoryId,
  });
}

/**
 * One payee for the whole set. `payee_cipher` / `payee_token` depend only on
 * the new payee, so ONE `encryptPayee` covers every row; the rows' own
 * amount/raw-description ciphers are left alone (a rename moves no money).
 */
export function bulkSetPayee(
  filters: TransactionFilters,
  excludeIds: readonly number[],
  normalizedPayee: string,
  cipher?: { payee_cipher: string; payee_token: string },
): Promise<BulkResult> {
  return bulkPost("payee", {
    filter: buildBulkFilter(filters),
    exclude_ids: [...excludeIds],
    normalized_payee: normalizedPayee,
    ...(cipher ?? {}),
  });
}

export function bulkSetReviewed(
  filters: TransactionFilters,
  excludeIds: readonly number[],
  reviewed: boolean,
): Promise<BulkResult> {
  return bulkPost("review", {
    filter: buildBulkFilter(filters),
    exclude_ids: [...excludeIds],
    reviewed,
  });
}

export function bulkAddTag(
  filters: TransactionFilters,
  excludeIds: readonly number[],
  tagId: number,
): Promise<BulkResult> {
  return bulkPost("tags", {
    filter: buildBulkFilter(filters),
    exclude_ids: [...excludeIds],
    tag_id: tagId,
  });
}

/**
 * `expectedCount` is what the confirmation told the user — the server refuses
 * the run (`409 bulk_count_mismatch`) if the filter no longer matches that
 * many, so a set that moved between the read and the click is never destroyed.
 */
export function bulkDelete(
  filters: TransactionFilters,
  excludeIds: readonly number[],
  expectedCount: number,
): Promise<BulkResult> {
  return bulkPost("delete", {
    filter: buildBulkFilter(filters),
    exclude_ids: [...excludeIds],
    expected_count: expectedCount,
  });
}

