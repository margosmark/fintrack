import { useState } from "react";
import type { FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, ShieldCheck, ShieldOff, Trash2, Copy, Check } from "lucide-react";
import {
  ApiError,
  deletePasskey,
  listPasskeys,
  me,
  passkeyRegister,
  passkeyRegisterOptions,
  totpConfirm,
  totpDisable,
  totpSetup,
  totpStatus,
} from "../../api/client";
import type { TotpSetupResponse } from "../../types";
import { createPasskey } from "../../lib/webauthn";
import type { PasskeyCreationOptions } from "../../lib/webauthn";
import { isReadOnlySession } from "../../auth/readOnlySession";
import { Card, CardTitle } from "../ui/Card";
import { Button } from "../ui/Button";
import { ErrorPanel } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { useToast } from "../ui/Toast";
import { AUTH_FIELD } from "../ui/formStyles";

/**
 * Settings → Security (docs/38 "Frontend"): TOTP enrollment (setup → manual
 * entry → confirm → 10 recovery codes ONCE), disable (a TOTP code OR a
 * recovery code), and passkey list / register / delete.
 *
 * Two contracts the copy and the wiring both hold:
 *  - recovery codes are shown exactly once, with the warning next to them —
 *    there is no re-fetch, no persistence, no "show again";
 *  - the demo tenant renders the card DISABLED with the same reference notice
 *    `AccountSection` uses, because every mutating route there answers
 *    `403 demo_account_restricted` (docs/38 decision 10).
 */

const DEMO_NOTICE =
  "Two-factor authentication and passkeys are disabled for the demo account — the card is shown for reference but changes are not saved.";

/** Surface any thrown value as one readable line (ApiError carries server detail). */
function messageOf(err: unknown, fallback: string): string {
  if (err instanceof ApiError || err instanceof Error) return err.message;
  return fallback;
}

export function SecuritySection() {
  const queryClient = useQueryClient();
  const toast = useToast();

  const meQuery = useQuery({ queryKey: ["me"], queryFn: me, retry: false });
  const isDemo = isReadOnlySession(meQuery.data);

  const statusQuery = useQuery({
    queryKey: ["totp-status"],
    queryFn: totpStatus,
    retry: false,
  });
  const passkeysQuery = useQuery({
    queryKey: ["passkeys"],
    queryFn: listPasskeys,
    retry: false,
  });

  // Enrollment state: `setup` holds the PENDING secret the server stashed.
  const [setup, setSetup] = useState<TotpSetupResponse | null>(null);
  const [confirmCode, setConfirmCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [showDisable, setShowDisable] = useState(false);
  const [disableCode, setDisableCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  async function handleSetup() {
    setError(null);
    setBusy(true);
    try {
      const data = await totpSetup();
      setSetup(data);
      setConfirmCode("");
      setRecoveryCodes(null);
    } catch (err) {
      setError(messageOf(err, "Could not start two-factor setup"));
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = await totpConfirm(confirmCode.trim());
      setRecoveryCodes(result.recovery_codes);
      setSetup(null);
      setConfirmCode("");
      setShowDisable(false);
      await queryClient.invalidateQueries({ queryKey: ["totp-status"] });
      toast.success("Two-factor enabled", "Store your recovery codes now — they are shown once.");
    } catch (err) {
      setError(messageOf(err, "That code was not accepted"));
    } finally {
      setBusy(false);
    }
  }

  async function handleDisable(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await totpDisable(disableCode.trim());
      setDisableCode("");
      setShowDisable(false);
      setSetup(null);
      setRecoveryCodes(null);
      await queryClient.invalidateQueries({ queryKey: ["totp-status"] });
      toast.success("Two-factor disabled", "Your account is back to password-only sign-in.");
    } catch (err) {
      setError(messageOf(err, "That code was not accepted"));
    } finally {
      setBusy(false);
    }
  }

  async function copyRecoveryCodes() {
    const text = (recoveryCodes ?? []).join("\n");
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(true);
      toast.success("Recovery codes copied");
    } catch {
      // Insecure context / permission denied: the codes stay selectable on
      // screen, so this is a lost convenience, not a lost account.
      toast.info("Copy blocked", "Select the codes and copy them manually.");
    }
  }

  async function handleRegister() {
    setError(null);
    setBusy(true);
    try {
      const { challenge_token, options } = await passkeyRegisterOptions();
      const credential = await createPasskey(options as unknown as PasskeyCreationOptions);
      // User dismissed the authenticator prompt: silent, nothing to report.
      if (credential === null) return;
      await passkeyRegister(challenge_token, credential);
      await queryClient.invalidateQueries({ queryKey: ["passkeys"] });
      toast.success("Passkey added", "You can now sign in with it.");
    } catch (err) {
      setError(messageOf(err, "Could not register a passkey"));
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(id: number) {
    setError(null);
    try {
      await deletePasskey(id);
      await queryClient.invalidateQueries({ queryKey: ["passkeys"] });
      toast.success("Passkey removed");
    } catch (err) {
      setError(messageOf(err, "Could not remove the passkey"));
    }
  }

  if (statusQuery.isPending || meQuery.isPending) {
    return (
      <Card className="max-w-xl p-4">
        <CardTitle>Security</CardTitle>
        <SkeletonRows rows={3} />
      </Card>
    );
  }

  const status = statusQuery.data;
  const enabled = status?.enabled === true;
  const pending = status?.pending === true;

  return (
    <Card hover className="max-w-xl space-y-5 p-4">
      <CardTitle>Security</CardTitle>

      {isDemo && <p className="text-xs leading-relaxed text-muted">{DEMO_NOTICE}</p>}
      {error !== null && <ErrorPanel message={error} />}
      {statusQuery.isError && status === undefined && (
        <QueryErrorPanel query={statusQuery} message={statusQuery.error?.message ?? "Could not load two-factor status"} />
      )}

      {/* --- TOTP ---------------------------------------------------------- */}
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium text-text">Two-factor authentication</p>
            <p className="text-xs text-muted">
              {enabled
                ? "On — a 6-digit code from your authenticator app is required at sign-in."
                : pending
                  ? "Setup started — confirm a code to turn it on."
                  : "Off — sign in currently needs only your password."}
            </p>
          </div>
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${
              enabled
                ? "bg-gain/15 text-gain"
                : pending
                  ? "bg-accent/15 text-accent"
                  : "bg-veil/[0.06] text-muted"
            }`}
          >
            {enabled ? "On" : pending ? "Pending" : "Off"}
          </span>
        </div>

        {recoveryCodes !== null && (
          <div
            className="space-y-2 rounded-xl border border-accent/30 bg-accent/[0.06] p-3"
            data-testid="recovery-codes"
          >
            <p className="text-xs font-semibold text-accent">
              Store these recovery codes now — they are shown once and cannot be retrieved again.
            </p>
            <ul className="grid grid-cols-2 gap-1 font-mono text-xs text-text">
              {recoveryCodes.map((code) => (
                <li key={code}>{code}</li>
              ))}
            </ul>
            <div className="flex gap-2">
              <Button type="button" size="sm" variant="outline" onClick={() => void copyRecoveryCodes()}>
                {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
                {copied ? "Copied" : "Copy all"}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setRecoveryCodes(null)}>
                I have saved them
              </Button>
            </div>
          </div>
        )}

        {enabled && !showDisable && (
          <Button
            type="button"
            variant="danger"
            size="sm"
            disabled={isDemo}
            onClick={() => {
              setShowDisable(true);
              setDisableCode("");
            }}
          >
            <ShieldOff className="size-4" aria-hidden />
            Turn off two-factor authentication
          </Button>
        )}

        {enabled && showDisable && (
          <form onSubmit={(e) => void handleDisable(e)} className="space-y-2">
            <label htmlFor="totp-disable-code" className="block text-xs font-medium text-muted">
              Enter a current 6-digit code or a recovery code to confirm
            </label>
            <input
              id="totp-disable-code"
              className={AUTH_FIELD}
              value={disableCode}
              onChange={(e) => setDisableCode(e.target.value)}
              placeholder="123456 or XXXXX-XXXXX"
              autoComplete="one-time-code"
              disabled={isDemo}
            />
            <div className="flex gap-2">
              <Button
                type="submit"
                variant="danger"
                size="sm"
                loading={busy}
                disabled={isDemo || disableCode.trim().length === 0}
              >
                Turn off
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setShowDisable(false)}>
                Cancel
              </Button>
            </div>
          </form>
        )}

        {!enabled && setup === null && (
          <Button
            type="button"
            variant="primary"
            size="sm"
            loading={busy}
            disabled={isDemo}
            onClick={() => void handleSetup()}
          >
            <ShieldCheck className="size-4" aria-hidden />
            {pending ? "Continue two-factor setup" : "Set up two-factor authentication"}
          </Button>
        )}

        {!enabled && setup !== null && (
          <div className="space-y-3 rounded-xl border border-border/60 p-3">
            <p className="text-xs leading-relaxed text-muted">
              Add this account to your authenticator app by hand (no camera needed): enter the key
              below, then type the 6-digit code it shows.
            </p>
            <div className="space-y-1">
              <label htmlFor="totp-secret" className="block text-xs font-medium text-muted">
                Secret key
              </label>
              <input
                id="totp-secret"
                readOnly
                value={setup.secret}
                onFocus={(e) => e.target.select()}
                className={`${AUTH_FIELD} font-mono`}
              />
              <p className="break-all font-mono text-[10px] text-muted">{setup.otpauth_url}</p>
            </div>
            <form onSubmit={(e) => void handleConfirm(e)} className="space-y-2">
              <label htmlFor="totp-confirm-code" className="block text-xs font-medium text-muted">
                Authentication code
              </label>
              <input
                id="totp-confirm-code"
                className={AUTH_FIELD}
                value={confirmCode}
                onChange={(e) => setConfirmCode(e.target.value)}
                placeholder="123456"
                autoComplete="one-time-code"
                disabled={isDemo}
              />
              <div className="flex gap-2">
                <Button
                  type="submit"
                  variant="primary"
                  size="sm"
                  loading={busy}
                  disabled={isDemo || confirmCode.trim().length === 0}
                >
                  Confirm and activate
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => setSetup(null)}>
                  Cancel
                </Button>
              </div>
            </form>
          </div>
        )}
      </div>

      {/* --- Passkeys ------------------------------------------------------ */}
      <div className="space-y-3 border-t border-border/60 pt-4">
        <div>
          <p className="text-sm font-medium text-text">Passkeys</p>
          <p className="text-xs text-muted">
            Sign in with the fingerprint, face or screen lock of this device — no password, no code.
          </p>
        </div>

        {passkeysQuery.isPending && <SkeletonRows rows={2} />}
        {passkeysQuery.isError && passkeysQuery.data === undefined && (
          <QueryErrorPanel query={passkeysQuery} message={passkeysQuery.error?.message ?? "Could not load passkeys"} />
        )}

        {passkeysQuery.data !== undefined && passkeysQuery.data.items.length === 0 && (
          <p className="text-xs text-muted">No passkeys registered yet.</p>
        )}

        {passkeysQuery.data !== undefined && passkeysQuery.data.items.length > 0 && (
          <ul className="space-y-2">
            {passkeysQuery.data.items.map((passkey) => (
              <li
                key={passkey.id}
                className="flex items-center justify-between gap-3 rounded-lg border border-border/60 px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-text">{passkey.name}</p>
                  <p className="text-xs text-muted">
                    Added {passkey.created_at === null ? "—" : passkey.created_at.slice(0, 10)}
                    {passkey.transports !== null && ` · ${passkey.transports}`}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="danger"
                  size="sm"
                  aria-label={`Remove ${passkey.name}`}
                  disabled={isDemo}
                  onClick={() => void handleDelete(passkey.id)}
                >
                  <Trash2 className="size-4" aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        )}

        <Button
          type="button"
          variant="outline"
          size="sm"
          loading={busy}
          disabled={isDemo}
          onClick={() => void handleRegister()}
        >
          <Plus className="size-4" aria-hidden />
          Add a passkey
        </Button>
        <p className="flex items-center gap-1.5 text-[11px] text-muted">
          <KeyRound className="size-3.5" aria-hidden />
          Up to 10 passkeys per account.
        </p>
      </div>
    </Card>
  );
}
