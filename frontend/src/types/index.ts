// Mirrors of the backend API payload shapes (docs/08; M1 contract).
// The `user` object NEVER contains hashed_password / llm_api_key — those are
// write-only or never serialized server-side.

// Type-only (erased at runtime): the report tab ids live with the tab strip so
// the ⌘K palette and the Reports page share one array (M95 P1).
import type { ReportTab } from "../lib/reportTabs";

/** M97 (docs/09 §Dashboard layout presets): which of three FIXED orders the
 *  dashboard renders its existing cards in. `overview` is the first-visit
 *  default; there is no fourth value — no editor, no drag-and-drop, no custom
 *  card set (docs/10 §M97 rejected scope). */
export type DashboardPreset = "overview" | "import_first" | "budget_first";

/** M108 P2 (docs/09 §Saved report views, docs/02 §users): one saved Reports
 *  preset. `tab` is a `REPORT_TABS` id (docs/12's seven families) and `state`
 *  holds ONLY the fields the Reports page restores — a closed set the server
 *  validates on write, so an invented key is a 422, never a dead preset. */
export interface SavedReportView {
  /** Client-generated token; rename/delete address a preset by it. */
  id: string;
  title: string;
  /**
   * The surface this restores: one of the seven report tabs, or `transactions`
   * for an M123 view. It doubles as the list filter — each control only ever
   * shows the rows that restore to itself.
   */
  tab: ReportTab | "transactions";
  /**
   * Which `state` contract this row follows. Optional here because rows written
   * before M123 carry no `kind`; the server always echoes `reports` for them.
   */
  kind?: "reports" | "transactions";
  /**
   * Report keys for `kind="reports"`, the four search params for
   * `kind="transactions"`. Typed as the intersection so either set reads
   * without a type guard — the server enforces the closed key-set per kind.
   */
  state: SavedReportViewState & SavedTransactionViewState;
}

/** M108 report presets (docs/09 §Saved report views). */
export interface SavedReportViewState {
  from?: string;
  to?: string;
  granularity?: "day" | "week" | "month" | "year";
  /** Chart variant on the Spending tab (`bars` | `treemap`). */
  view?: string;
  year?: number;
  sort_key?: string;
  sort_desc?: boolean;
  /** M108 P3: the Owner dimension's setting on the tabs that offer it. */
  group_by?: "category" | "tag" | "owner";
}

/**
 * M123 (docs/09 §Create from search): a transaction view's `state` is the
 * four URL-addressable `/transactions` params verbatim, so restoring is a
 * navigation and the view is a bookmark.
 */
export interface SavedTransactionViewState {
  /** The fuzzy search term (`?q=`), NOT the payee filter. */
  q?: string;
  /** A category id, or the literal `"uncategorized"`. */
  category?: string;
  account?: string;
  /** Comma-separated tag ids. */
  tag?: string;
}

export interface User {
  id: number;
  email: string;
  name?: string | null;
  privacy_mode: boolean;
  currency_tracker_enabled: boolean;
  llm_provider: string;
  llm_base_url: string | null;
  llm_model: string;
  /** E2EE Phase 2 (docs/33): server rejects plaintext-only writes when true. */
  encryption_required: boolean;
  /** Server-side layout preset (M97) — always present, defaults to `overview`. */
  dashboard_preset: DashboardPreset;
  /** M108 P2 — always present; `[]` means "none saved", never absent. */
  saved_report_views: SavedReportView[];
  /** M174 — the user's OWN tags that mark a transaction as recurring intent;
   *  always present, `[]` means the feature is off (never absent). */
  recurring_intent_tag_ids: number[];
}

export interface TokenPair {
  access_token: string;
  token_type: "bearer";
}

/** Response of POST /auth/login and POST /auth/refresh (POST /auth/register no
 *  longer returns one — see RegisterAccepted).
 *  The refresh token is NOT in the body (audit M1) — it arrives only as an
 *  httpOnly cookie; see docs/08 auth delivery notes. */
export interface AuthResponse extends TokenPair {
  user: User;
}

/**
 * Response of POST /auth/register (M87, docs/29 F15).
 *
 * The SAME body for a free and for an already-taken address, and never a
 * token: an account-existence oracle is exactly what a `201 + session` vs
 * `409 duplicate_email` split used to hand out. A free address does get the
 * account created — sign in with the password just chosen.
 */
export interface RegisterAccepted {
  status: "accepted";
  detail: string;
}

// --- M74 auth hardening (docs/38): second factor + SSO payload mirrors -------

/**
 * The OTHER arm of the `POST /auth/login` union (docs/38 "Login + challenge"):
 * 200 with `{challenge:"totp", ...}` instead of a token pair when the account
 * has an active TOTP factor. Discriminated from `AuthResponse` by the
 * `challenge` field — a token pair never carries one.
 */
export interface LoginChallenge {
  challenge: "totp";
  /** 5-minute signed challenge JWT; posts to /auth/login/totp or /recovery. */
  challenge_token: string;
  expires_in: number;
}

/** GET /auth/totp/status */
export interface TotpStatus {
  enabled: boolean;
  pending: boolean;
}

/** POST /auth/totp/setup */
export interface TotpSetupResponse {
  secret: string;
  otpauth_url: string;
}

/** POST /auth/totp/confirm — the ONLY time these 10 codes are ever shown. */
export interface TotpConfirmResponse {
  recovery_codes: string[];
}

/** One row of GET /auth/passkeys `items`. */
export interface PasskeyInfo {
  id: number;
  name: string;
  created_at: string | null;
  last_used_at: string | null;
  /** Comma-joined transports hint, e.g. `internal,hybrid`. */
  transports: string | null;
}

/** GET /auth/passkeys */
export interface PasskeyPage {
  items: PasskeyInfo[];
  total: number;
}

/** POST /auth/passkeys/options and POST /auth/passkeys/login/options. */
export interface PasskeyOptionsResponse {
  challenge_token: string;
  /** `PublicKeyCredential…Options` as JSON (base64url challenge/ids). */
  options: Record<string, unknown>;
}

/** GET /auth/oidc/config — always 200 so the login page needs no error path. */
export interface OidcConfigResponse {
  enabled: boolean;
}

/** GET /auth/oidc/preview — the `oidc_pending` cookie's contents. */
export interface OidcPreview {
  email: string;
  linked: boolean;
}
