// sharing — typed client for household sharing (M90, docs/43 §5/§6).
//
// Owner routes live under `/sharing`; the partner's read projection under
// `/partner` — all five partner routes are GET (pinned by a backend test).
// `401 sharing_not_linked` on a partner DATA path is a DOMAIN state, not an
// expired session: `client.ts`'s AUTH_401_SKIP_PREFIXES lets it reach the
// caller as an ApiError instead of running the refresh → /login dance
// (docs/43 §7). Since M92 `GET /partner/state` no longer 401s for an
// unlinked authenticated caller — it answers `200 {linked: false}`
// (docs/43 §6) — so a 401 there now means no valid session at all.
import { ApiError, api } from "./client";
import type { Account } from "./accounts";
import type { Budget } from "./budgets";
import type { Category } from "./categories";
import type { Transaction } from "./transactions";
import { budgetsProgress, etToday } from "../lib/budgets";
import type { BudgetProgress } from "../lib/budgets";
import { loadFallbackRates } from "./s3Inputs";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface SharingPartnerBrief {
  user_id: number;
  name: string | null;
  email: string;
}

/** 200 from GET /sharing, PATCH /sharing/scope, POST /sharing/revoke and accept. */
export interface SharingState {
  status: "none" | "pending" | "active" | "revoked" | "expired" | "cancelled";
  link_id: number | null;
  email: string | null;
  partner: SharingPartnerBrief | null;
  account_ids: number[];
  expires_at: string | null;
  created_at: string | null;
  accepted_at: string | null;
  revoked_at: string | null;
}

/** 201 from POST /sharing/invites — the ONE body that carries token/accept_url. */
export interface InviteCreated {
  id: number;
  email: string;
  status: string;
  account_ids: number[];
  expires_at: string;
  created_at: string | null;
  token: string;
  accept_url: string;
  email_sent: boolean;
}

export interface InvitePartnerBrief {
  user_id: number;
  name: string | null;
}

/** GET /sharing/invites items — never carries token/accept_url (docs/43 §5). */
export interface InvitePublic {
  id: number;
  email: string;
  status: string;
  account_ids: number[];
  expires_at: string | null;
  created_at: string | null;
  accepted_at: string | null;
  revoked_at: string | null;
  partner: InvitePartnerBrief | null;
}

/** GET /sharing/audit items — the mcp_audit receipt shape (docs/43 §3). */
export interface SharingAuditRow {
  id: number;
  event: string;
  actor_user_id: number | null;
  invite_id: number | null;
  args_hash: string;
  response_code: number;
  duration_ms: number;
  created_at: string | null;
}

export interface PartnerOwnerBrief {
  user_id: number;
  name: string | null;
  email: string;
}

/** The LINKED arm of 200 from GET /partner/state (docs/43 §6, M92). */
export interface PartnerStateLinked {
  linked: true;
  owner: PartnerOwnerBrief;
  account_ids: number[];
  privacy_mode: boolean;
  link_id: number;
  accepted_at: string | null;
}

/**
 * The UNLINKED arm (M92, docs/43 §6): an authenticated caller with no link
 * gets `200 {linked: false}` — a normal first-page state, not an error. The
 * four data routes still answer `401 sharing_not_linked`.
 */
export interface PartnerStateUnlinked {
  linked: false;
}

export type PartnerState = PartnerStateLinked | PartnerStateUnlinked;

/** GET /partner/budgets item — definitions only, progress stripped (D3). */
export interface PartnerBudget {
  id: number;
  user_id: number;
  category_id: number;
  /** The definition's target — `null` while the owner's privacy mode is on. */
  amount: string | null;
  period: string;
  /** M110 rollover setting — structure, never masked. */
  carry: string;
  /** M110: money like `amount` — `null` when masked or when nothing carried. */
  carried_in: string | null;
  category_name: string;
}

/**
 * The money fields privacy mode nulls on the partner seam (docs/43 §9,
 * M90 D4/A). The owner's own `Account` / `Transaction` types keep `string`:
 * the mask is the projection's, not the owner's.
 */
type AccountMoney = "cash_balance" | "investment_value" | "total_value" | "starting_balance";
type TransactionMoney = "amount_native" | "cad_amount";

/** `/partner/accounts` item — `AccountPublic` with its four money fields nullable. */
export type PartnerAccount = Omit<Account, AccountMoney> & {
  [K in AccountMoney]: Account[K] | null;
};

/** `/partner/transactions` item — `TransactionPublic` with its money nullable. */
export type PartnerTransaction = Omit<Transaction, TransactionMoney> & {
  [K in TransactionMoney]: Transaction[K] | null;
};

/**
 * M107 (docs/44 §4.1/§9): the partner's split header — the same
 * `SplitParentPublic` with its two money fields masked by privacy mode, so
 * the group header can print 🔒 instead of a number the owner hid.
 */
export type PartnerSplitParent = PartnerTransaction & { child_count: number };

/** `PartnerTransactionPage` (docs/44 §4.1) — LEDGER `items` + the headers. */
export interface PartnerTransactionPage {
  items: PartnerTransaction[];
  total: number;
  limit: number;
  offset: number;
  split_parents: PartnerSplitParent[];
}

// --- owner (router app/api/sharing.py) -------------------------------------

export function getSharingState(): Promise<SharingState> {
  return api<SharingState>("/sharing");
}

export function listInvites(): Promise<Page<InvitePublic>> {
  return api<Page<InvitePublic>>("/sharing/invites");
}

export function listSharingAudit(): Promise<Page<SharingAuditRow>> {
  return api<Page<SharingAuditRow>>("/sharing/audit");
}

/** 201 — the response is the only place the raw token ever appears. */
export function createInvite(email: string, accountIds: number[]): Promise<InviteCreated> {
  return api<InviteCreated>("/sharing/invites", {
    method: "POST",
    body: { email, account_ids: accountIds },
  });
}

export function cancelInvite(inviteId: number): Promise<void> {
  return api<void>(`/sharing/invites/${inviteId}`, { method: "DELETE" }).then(() => undefined);
}

export function updateSharingScope(accountIds: number[]): Promise<SharingState> {
  return api<SharingState>("/sharing/scope", {
    method: "PATCH",
    body: { account_ids: accountIds },
  });
}

export function revokeSharing(): Promise<SharingState> {
  return api<SharingState>("/sharing/revoke", { method: "POST" });
}

/** The invitee's accept — lives on the sharing router, not /partner. */
export function acceptInvite(token: string): Promise<SharingState> {
  return api<SharingState>("/sharing/invites/accept", {
    method: "POST",
    body: { token },
  });
}

/**
 * The partner ends their OWN access — M90 D6 follow-up (docs/43 §5). Same
 * router as accept (never `/partner/`, which stays GET-only), and it answers
 * 404 `sharing_not_linked` rather than 401 so a no-link caller never runs the
 * refresh → /login dance on a valid session.
 */
export function leaveSharing(): Promise<SharingState> {
  return api<SharingState>("/sharing/leave", { method: "POST" });
}

// --- partner (GET-only projection, docs/43 §6) -----------------------------

export function getPartnerState(): Promise<PartnerState> {
  return api<PartnerState>("/partner/state");
}

export function listPartnerAccounts(): Promise<Page<PartnerAccount>> {
  return api<Page<PartnerAccount>>("/partner/accounts");
}

export function listPartnerCategories(): Promise<Page<Category>> {
  return api<Page<Category>>("/partner/categories");
}

export function listPartnerBudgets(): Promise<PartnerBudget[]> {
  return api<PartnerBudget[]>("/partner/budgets");
}

export function listPartnerTransactions(
  limit: number,
  offset: number,
): Promise<PartnerTransactionPage> {
  return api<PartnerTransactionPage>(
    `/partner/transactions?limit=${limit}&offset=${offset}`,
  );
}

/**
 * Every partner-visible row, paged to `total` under the server's `le=200`
 * cap — the same paginate-to-total pattern the owner's own loaders use
 * (docs/43 §13): a single page would silently truncate the feed.
 */
export async function fetchAllPartnerTransactions(): Promise<PartnerTransaction[]> {
  return (await fetchAllPartnerTransactionPages()).items;
}

/**
 * Same paginate-to-total walk, keeping every page's `split_parents` — the
 * `/shared` feed groups its rows exactly like the owner's register does
 * (docs/44 §6.3) and a parent is never in `items`.
 */
async function fetchAllPartnerTransactionPages(): Promise<{
  items: PartnerTransaction[];
  split_parents: PartnerSplitParent[];
}> {
  const items: PartnerTransaction[] = [];
  const parents = new Map<number, PartnerSplitParent>();
  let offset = 0;
  for (;;) {
    const page = await listPartnerTransactions(200, offset);
    items.push(...page.items);
    for (const parent of page.split_parents ?? []) parents.set(parent.id, parent);
    offset += page.items.length;
    if (page.items.length === 0 || offset >= page.total) break;
  }
  return { items, split_parents: [...parents.values()] };
}

/** Everything `/shared` renders, in one fetch cycle (docs/43 §13). */
export interface SharedView {
  /** Always the LINKED arm — an unlinked caller never gets this far. */
  state: PartnerStateLinked;
  /** Money is `null` while `state.privacy_mode` is on (docs/43 §9). */
  accounts: PartnerAccount[];
  /** `amount_native` / `cad_amount` are `null` while privacy mode is on (docs/43 §9). */
  transactions: PartnerTransaction[];
  /** M107 (docs/44 D11/§9): group headers — masked exactly like the rows. */
  split_parents: PartnerSplitParent[];
  /**
   * `budgetsProgress` (`lib/budgets.ts`) over PARTNER rows only — the D3
   * discard-and-recompute: the server serves budget definitions without
   * `spent` (those figures would read hidden accounts), and the client
   * derives progress from rows it actually holds. Label the UI "shared
   * accounts only". Always `[]` under privacy mode: the figures it would
   * sum are the very ones the owner hid, so there is nothing to derive
   * and the UI says so instead of showing an empty bar.
   */
  budgetProgress: BudgetProgress[];
}

export async function loadSharedView(): Promise<SharedView> {
  const [state, accountsPage, txPage] = await Promise.all([
    getPartnerState(),
    listPartnerAccounts(),
    fetchAllPartnerTransactionPages(),
  ]);
  const transactions = txPage.items;
  const split_parents = txPage.split_parents;
  // M92 (docs/43 §6): /partner/state answers `200 {linked: false}` to an
  // unlinked caller instead of 401ing. The data routes still 401 and reject
  // the Promise.all first in practice — this guard narrows the type AND keeps
  // the unlinked verdict independent of which call rejects, so the shared
  // page's "access was removed" empty state has exactly one trigger.
  if (!state.linked) {
    throw new ApiError(401, "No shared household is linked to this account", "sharing_not_linked");
  }
  const [budgets, categoriesPage] = await Promise.all([
    // The OWNER's privacy mode (docs/43 §9): under it every money field in
    // the rows above is JSON `null`, so there is nothing to sum — fetching
    // definitions we cannot use and deriving progress over hidden figures
    // would be theatre. The page labels the gap instead of showing a bar.
    state.privacy_mode ? Promise.resolve<PartnerBudget[]>([]) : listPartnerBudgets(),
    listPartnerCategories(),
  ]);
  if (state.privacy_mode) {
    return { state, accounts: accountsPage.items, transactions, split_parents, budgetProgress: [] };
  }
  const today = etToday();
  // Privacy mode is off on this branch, so every amount is a real string:
  // the casts name what the runtime just guaranteed (docs/43 §9).
  const rates = await loadFallbackRates(transactions as Transaction[], today);
  const budgetProgress = budgetsProgress({
    // `budgets` is `PartnerBudget[]` (money nullable by type) but this branch
    // is privacy-off, where `amount` is a real string — same guarantee the
    // `transactions` cast below rests on (docs/43 §9).
    budgets: budgets as Budget[],
    categories: categoriesPage.items,
    transactions: transactions as Transaction[],
    rates,
    today,
  });
  return { state, accounts: accountsPage.items, transactions, split_parents, budgetProgress };
}
