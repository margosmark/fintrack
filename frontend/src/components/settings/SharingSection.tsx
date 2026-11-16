// SharingSection — owner consent card + the partner's read-only row
// (M90, docs/43 §13), mounted from Settings.tsx.
//
// Owner side: renders `GET /sharing` — empty ("Nobody has access"), pending
// (address, expiry countdown, Copy invite link, Cancel) or active (partner
// identity, the account multi-select that `PATCH /sharing/scope`es live,
// Revoke behind a confirm) — plus the collapsed audit list from
// `GET /sharing/audit`. Inviting shows the `email_sent` truth: the 201 body
// is ALWAYS a working invite; mail is best-effort (privacy mode / SMTP).
//
// The raw token exists in exactly two places (docs/43 §5): the create 201
// and the email. "Copy invite link" therefore works for an invite created in
// THIS session (`lastCreated`, held in memory); after a reload the link is
// gone by design and the card says so instead of pretending.
//
// Partner side (`PartnerSharingRow`): one line from `GET /partner/state`
// plus **Leave** (M90 D6 follow-up, docs/43 §5) — the partner's second and
// last write in the system, ending THEIR OWN access. Since M92 an unlinked
// caller gets `200 {linked: false}` — a normal state, not an error — so the
// row renders nothing and the owner-consent card above (the invite CTA)
// carries the next step. That refetch is also what makes this row disappear
// after a leave: in place, no redirect, their own session untouched.
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, History, Link2, Users, X } from "lucide-react";
import { ApiError } from "../../api/client";
import { listAccounts } from "../../api/accounts";
import type { Account } from "../../api/accounts";
import {
  cancelInvite,
  createInvite,
  getPartnerState,
  getSharingState,
  leaveSharing,
  listSharingAudit,
  revokeSharing,
  updateSharingScope,
} from "../../api/sharing";
import type { InviteCreated } from "../../api/sharing";
import { Button } from "../ui/Button";
import { EmptyState, ErrorPanel } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { useToast } from "../ui/Toast";

function messageOf(err: unknown, fallback: string): string {
  if (err instanceof ApiError || err instanceof Error) return err.message;
  return fallback;
}

/** ISO date → `YYYY-MM-DD` (the display form docs/43 §13 pins for "since"). */
function dayOf(iso: string | null): string {
  return iso == null ? "" : iso.slice(0, 10);
}

/** "6d 23h" / "42m" style remaining time until `expiresAt`. */
function remaining(expiresAt: string, nowMs: number): string {
  const ms = new Date(expiresAt).getTime() - nowMs;
  if (ms <= 0) return "expired";
  const minutes = Math.floor(ms / 60_000);
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  return `${Math.max(minutes, 1)}m`;
}

function Checkbox({
  account,
  checked,
  disabled,
  onToggle,
}: {
  account: Account;
  checked: boolean;
  disabled: boolean;
  onToggle: (id: number) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-veil/[0.04]">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={() => onToggle(account.id)}
        className="size-4 accent-accent"
      />
      <span className="truncate">{account.name}</span>
      <span className="ml-auto text-xs text-muted">{account.currency}</span>
    </label>
  );
}

export function SharingSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const stateQ = useQuery({ queryKey: ["sharing", "state"], queryFn: getSharingState });
  const accountsQ = useQuery({
    queryKey: ["sharing", "accounts"],
    queryFn: async () => (await listAccounts(200)).items,
  });

  const [auditOpen, setAuditOpen] = useState(false);
  const auditQ = useQuery({
    queryKey: ["sharing", "audit"],
    queryFn: listSharingAudit,
    enabled: auditOpen,
  });

  // Invite form (available while not active — the server refuses a second
  // active link with 409 sharing_partner_exists).
  const [email, setEmail] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [inviting, setInviting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [lastCreated, setLastCreated] = useState<InviteCreated | null>(null);

  // Lifecycle actions (scope PATCH, cancel, revoke).
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);

  const state = stateQ.data ?? null;
  const status = state?.status ?? "none";

  // Expiry countdown ticks only while a pending invite is on screen.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (status !== "pending") return;
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, [status]);

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["sharing"] });
  }

  async function submitInvite() {
    setInviting(true);
    setFormError(null);
    try {
      const invite = await createInvite(email.trim(), selected);
      setLastCreated(invite);
      setEmail("");
      setSelected([]);
      await refresh();
    } catch (err) {
      setFormError(messageOf(err, "Could not create the invitation"));
    } finally {
      setInviting(false);
    }
  }

  async function toggleScope(id: number) {
    if (state == null) return;
    const next = state.account_ids.includes(id)
      ? state.account_ids.filter((existing) => existing !== id)
      : [...state.account_ids, id];
    setActionError(null);
    try {
      await updateSharingScope(next);
      await refresh();
    } catch (err) {
      setActionError(messageOf(err, "Could not update shared accounts"));
    }
  }

  async function cancelPending() {
    if (state?.link_id == null) return;
    setActionError(null);
    try {
      await cancelInvite(state.link_id);
      setLastCreated(null);
      await refresh();
    } catch (err) {
      setActionError(messageOf(err, "Could not cancel the invitation"));
    }
  }

  async function revoke() {
    if (!confirmRevoke) {
      setConfirmRevoke(true);
      return;
    }
    setActionError(null);
    try {
      await revokeSharing();
      setConfirmRevoke(false);
      setLastCreated(null);
      await refresh();
    } catch (err) {
      setActionError(messageOf(err, "Could not revoke access"));
    }
  }

  async function copyLink(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      toast.success("Invitation link copied");
    } catch {
      toast.error("Could not copy — select the link and copy it manually");
    }
  }

  const accounts = accountsQ.data ?? [];
  const isActive = status === "active";
  const isPending = status === "pending";
  const showForm = !isActive;

  return (
    <section aria-labelledby="sharing-heading" className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 id="sharing-heading" className="text-base font-semibold tracking-tight">
            Sharing
          </h2>
          <p className="text-sm text-muted">
            Invite one person to a read-only view of the accounts you choose. They can never
            change your data.
          </p>
        </div>
        <Users className="size-5 shrink-0 text-muted" aria-hidden />
      </div>

      {stateQ.isLoading && <p className="text-sm text-muted">Loading sharing status…</p>}
      {stateQ.isError && <QueryErrorPanel query={stateQ} message={messageOf(stateQ.error, "Could not load sharing")} />}

      {/* "Nobody has access" is true for EVERY state without a live link:
          none, and the ended ones (revoked/expired/cancelled — the spec's
          pending/active cards only render while a link is live). */}
      {!isActive && !isPending && !stateQ.isLoading && !stateQ.isError && (
        <EmptyState
          icon={<Users className="size-6" aria-hidden />}
          title="Nobody has access"
          hint="Create an invitation below when you want to share accounts with someone."
          preview={{ none: "an active sharing link cannot exist on the demo tenant" }}
          action={{
            label: "Create invite",
            onClick: () => void submitInvite(),
            ariaLabel: "Create invite — Nobody has access",
          }}
        />
      )}

      {isPending && state != null && state.expires_at != null && (
        <div className="rounded-xl border border-border bg-veil/[0.03] p-4">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Link2 className="size-4 text-muted" aria-hidden />
            <span className="font-medium">{state.email}</span>
            <span className="text-xs text-muted">
              pending · expires in {remaining(state.expires_at, now)}
            </span>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {lastCreated != null && lastCreated.id === state.link_id ? (
              <>
                <input
                  readOnly
                  value={lastCreated.accept_url}
                  aria-label="Invitation link"
                  className="field-ring w-full min-w-0 flex-1 rounded-lg border border-control bg-surface px-2 py-1.5 text-xs outline-none sm:w-auto"
                  onFocus={(e) => e.currentTarget.select()}
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void copyLink(lastCreated.accept_url)}
                >
                  <Copy className="size-4" aria-hidden />
                  Copy invite link
                </Button>
              </>
            ) : (
              <p className="text-xs text-muted">
                The invite link is shown once, when the invitation is created — copy it right
                after inviting. Re-send by cancelling and re-inviting.
              </p>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => void cancelPending()}
              aria-label="Cancel invitation"
            >
              <X className="size-4" aria-hidden />
              Cancel
            </Button>
          </div>
          {lastCreated != null && lastCreated.id === state.link_id && (
            <p className="mt-2 text-xs text-muted">
              {lastCreated.email_sent
                ? `Invitation emailed to ${lastCreated.email}.`
                : "Invitation link copied — email was not sent (privacy mode / SMTP not configured)."}
            </p>
          )}
        </div>
      )}

      {isActive && state != null && (
        <div className="rounded-xl border border-border bg-veil/[0.03] p-4">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Users className="size-4 text-muted" aria-hidden />
            <span className="font-medium">{state.partner?.name ?? state.partner?.email}</span>
            <span className="text-sm text-muted">{state.partner?.email}</span>
            <span className="text-xs text-muted">since {dayOf(state.accepted_at)}</span>
          </div>
          <p className="mt-3 text-xs font-medium text-muted">Shared accounts</p>
          <div className="mt-1 space-y-0.5" role="group" aria-label="Shared accounts">
            {accounts.map((account) => (
              <Checkbox
                key={account.id}
                account={account}
                checked={state.account_ids.includes(account.id)}
                disabled={false}
                onToggle={(id) => void toggleScope(id)}
              />
            ))}
            {accounts.length === 0 && (
              <p className="text-sm text-muted">No accounts yet.</p>
            )}
          </div>
          <p className="mt-2 text-xs text-muted">
            Unchecking takes effect on their next request — no cache, no wait.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <Button
              type="button"
              variant={confirmRevoke ? "primary" : "outline"}
              size="sm"
              onClick={() => void revoke()}
            >
              {confirmRevoke ? "Confirm revoke" : "Revoke access"}
            </Button>
            {confirmRevoke && (
              <button
                type="button"
                className="text-xs text-muted hover:underline"
                onClick={() => setConfirmRevoke(false)}
              >
                Keep access
              </button>
            )}
          </div>
          <p className="mt-2 text-xs text-muted">
            Revoking is immediate: their next request renders “No shared household”. They are
            not logged out — only the link ends.
          </p>
        </div>
      )}

      {actionError != null && <ErrorPanel message={actionError} />}

      {showForm && (
        <form
          className="space-y-3 rounded-xl border border-border p-4"
          onSubmit={(e) => {
            e.preventDefault();
            void submitInvite();
          }}
        >
          <p className="text-sm font-medium">New invitation</p>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="their@example.com"
            aria-label="Email address"
            className="field-ring w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors placeholder:text-muted hover:border-accent/70"
          />
          <div className="space-y-0.5" role="group" aria-label="Accounts to share">
            {accounts.map((account) => (
              <Checkbox
                key={account.id}
                account={account}
                checked={selected.includes(account.id)}
                disabled={false}
                onToggle={(id) =>
                  setSelected((prev) =>
                    prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
                  )
                }
              />
            ))}
          </div>
          <p className="text-xs text-muted">
            Deny-by-default: share nothing now and add accounts later — they see only what is
            checked here.
          </p>
          {formError != null && <ErrorPanel message={formError} />}
          <Button type="submit" variant="primary" size="md" loading={inviting}>
            Create invite
          </Button>
        </form>
      )}

      <div>
        <button
          type="button"
          className="flex items-center gap-2 text-sm text-muted hover:text-text"
          onClick={() => setAuditOpen((open) => !open)}
          aria-expanded={auditOpen}
        >
          <History className="size-4" aria-hidden />
          {auditOpen ? "Hide activity log" : "Activity log"}
        </button>
        {auditOpen && auditQ.isLoading && (
          <p className="mt-2 text-sm text-muted">Loading…</p>
        )}
        {auditOpen && auditQ.isError && (
          <QueryErrorPanel query={auditQ} message={messageOf(auditQ.error, "Could not load the activity log")} />
        )}
        {auditOpen && auditQ.data != null && (
          <ul className="mt-2 space-y-1 text-xs text-muted">
            {auditQ.data.items.map((row) => (
              <li key={row.id} className="flex flex-wrap gap-x-2">
                <span className="font-mono text-muted">{row.event}</span>
                <span>→ {row.response_code}</span>
                <span>actor {row.actor_user_id ?? "—"}</span>
                <span>{dayOf(row.created_at)}</span>
              </li>
            ))}
            {auditQ.data.items.length === 0 && <li>No activity yet.</li>}
          </ul>
        )}
      </div>
    </section>
  );
}

/**
 * The partner's Settings row (docs/43 §13): one line — "You can see
 * <owner>'s data — 2 shared accounts, since 2026-10-02" — plus **Leave**
 * (M90 D6 follow-up, docs/43 §5), behind the same two-step confirm the
 * owner's "Revoke access" uses. `POST /sharing/leave` ends THEIR OWN access
 * and nothing on the owner's side; the row then disappears, because the
 * refetched `GET /partner/state` answers `200 {linked: false}` (M92, docs/43
 * §6) and `!linked` renders null — in place, no redirect, their own session
 * untouched.
 *
 * Renders nothing when no link exists, and when an actual error (network,
 * session) is returned rather than failing the whole Settings page.
 */
export function PartnerSharingRow() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const stateQ = useQuery({
    queryKey: ["partner", "state"],
    queryFn: getPartnerState,
    retry: false,
  });
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);

  async function leave() {
    if (!confirmLeave) {
      setConfirmLeave(true);
      return;
    }
    setLeaveError(null);
    try {
      await leaveSharing();
      setConfirmLeave(false);
      toast.success("You left the shared household");
      // The refetch answers 200 {linked: false} → this row renders null.
      await queryClient.invalidateQueries({ queryKey: ["partner"] });
    } catch (err) {
      setLeaveError(messageOf(err, "Could not leave the shared household"));
      setConfirmLeave(false);
    }
  }

  const state = stateQ.data;
  // `200 {linked: false}` is the normal first-page state (M92, docs/43 §6).
  if (stateQ.isError || state == null || !state.linked) return null;
  const { owner, account_ids, accepted_at } = state;
  const count = account_ids.length;
  return (
    <div className="space-y-2 rounded-xl border border-border bg-veil/[0.03] px-4 py-3 text-sm">
      <div className="flex flex-wrap items-center gap-3">
        <Users className="size-4 shrink-0 text-muted" aria-hidden />
        <p>
          You can see <span className="font-medium">{owner.name ?? owner.email}</span>
          {"'"}s data — {count} shared account{count === 1 ? "" : "s"}, since {dayOf(accepted_at)}
        </p>
        <div className="ml-auto flex items-center gap-2">
          <Button
            type="button"
            variant={confirmLeave ? "primary" : "outline"}
            size="sm"
            onClick={() => void leave()}
          >
            {confirmLeave ? "Confirm leave" : "Leave"}
          </Button>
          {confirmLeave && (
            <button
              type="button"
              className="text-xs text-muted hover:underline"
              onClick={() => setConfirmLeave(false)}
            >
              Stay
            </button>
          )}
        </div>
      </div>
      {confirmLeave && (
        <p className="text-xs text-muted">
          Leaving ends your access immediately — these accounts disappear from your view on your
          next request. Nothing changes on the owner&rsquo;s side.
        </p>
      )}
      {leaveError != null && <ErrorPanel message={leaveError} />}
    </div>
  );
}
