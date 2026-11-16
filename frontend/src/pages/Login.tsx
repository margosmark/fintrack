import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { LogIn, Rocket, ShieldCheck, KeyRound } from "lucide-react";
import { AuthShell } from "../components/layout/AuthShell";
import { Button } from "../components/ui/Button";
import { ErrorPanel } from "../components/ui/EmptyState";
import { useAuth } from "../auth/AuthContext";
import { postAuthRedirect } from "../auth/postAuthRedirect";
import { ApiError, OIDC_LOGIN_PATH, oidcConfig } from "../api/client";
import { useToast } from "../components/ui/Toast";
import { AUTH_FIELD } from "../components/ui/formStyles";

/**
 * Sign-in page (docs/09 + docs/38 "Frontend").
 *
 * One form, two steps: the password step is unchanged (demo door first, hint
 * intact), and a response that carries a `challenge` swaps the fields for a
 * second-factor step — a 6-digit TOTP code by default, a single-use recovery
 * code behind the toggle. Wrong codes stay on the step and show the server's
 * own `{detail}`. Passwordless passkey sign-in and (when the operator enabled
 * it) OIDC SSO sit beside the password button; the passkey button works with
 * OR without a typed address — blank starts the discoverable ceremony and a
 * typed address keeps the email-first path that also serves non-discoverable
 * credentials (docs/38 §Passkeys).
 */

/** The second-factor step: which credential the single input is asking for. */
type CodeKind = "totp" | "recovery";

/**
 * Shown when a passwordless attempt (no email typed) produces no sign-in:
 * the browser either held no discoverable credential for this origin or the
 * prompt was dismissed, and with an empty address box the two are
 * indistinguishable — so name the two fallbacks instead of doing nothing
 * (docs/38 §Frontend). Typing an address keeps the silent-dismissal contract
 * of the email-first path.
 */
const PASSKEY_FALLBACK_HINT =
  "Enter your email above to sign in with a passkey for a specific account, or use your password.";

/** Surface any thrown value as one readable line (ApiError carries server detail). */
function messageOf(err: unknown, fallback: string): string {
  if (err instanceof ApiError || err instanceof Error) return err.message;
  return fallback;
}

export function Login() {
  const { login, completeTotpLogin, completeRecoveryLogin, passkeyLogin, demoLogin } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  // One destination for every sign-in button: the path RequireAuth stashed on
  // the way in, or the dashboard (M68 — the demo button used to hard-code
  // `/dashboard` and drop deep links).
  const next = postAuthRedirect(location.state);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Second-factor step state. The challenge token lives only as long as the
  // step does (5 min server-side); a wrong code never resets it.
  const [challengeToken, setChallengeToken] = useState<string | null>(null);
  const [codeKind, setCodeKind] = useState<CodeKind>("totp");
  const [code, setCode] = useState("");

  // SSO is opt-in per operator (OIDC_ENABLED, docs/38 Configuration): a config
  // that fails to load simply hides the button — no error path on the door.
  const oidc = useQuery({
    queryKey: ["oidc-config"],
    queryFn: oidcConfig,
    retry: false,
    staleTime: 60_000,
  });
  const ssoEnabled = oidc.data?.enabled === true;

  const toast = useToast();

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (challengeToken !== null) {
        if (codeKind === "recovery") {
          await completeRecoveryLogin(challengeToken, code.trim());
        } else {
          await completeTotpLogin(challengeToken, code.trim());
        }
        navigate(next, { replace: true });
        return;
      }
      const result = await login(email, password);
      if (result.status === "totp") {
        // Password accepted, second factor pending — no session yet, so stay
        // on this page and ask for the code (docs/38 decision 4).
        setChallengeToken(result.challengeToken);
        setCodeKind("totp");
        setCode("");
        return;
      }
      navigate(next, { replace: true });
    } catch (err) {
      // Wrong/expired/replayed code lands here with the server's detail and
      // stays on the step — the challenge token is still good.
      setError(messageOf(err, "Login failed"));
    } finally {
      setBusy(false);
    }
  }

  async function handlePasskey() {
    setError(null);
    setBusy(true);
    const emailValue = email.trim();
    // No address typed = the discoverable ceremony (docs/38): the browser
    // offers every credential it holds for this origin and the server
    // resolves the account from the assertion.
    const passwordless = emailValue.length === 0;
    try {
      // Resolves false only when the user dismissed the prompt: with an
      // address typed that stays silent (no message, no navigation — just
      // back to the form). Passwordless has no other visible outcome when
      // nothing was offered, so it says which fallbacks exist instead.
      const signedIn = await passkeyLogin(emailValue);
      if (signedIn) {
        navigate(next, { replace: true });
      } else if (passwordless) {
        setError(PASSKEY_FALLBACK_HINT);
      }
    } catch (err) {
      const detail = messageOf(err, "Passkey sign-in failed");
      // The server's refusal is generic by design (no account enumeration);
      // with no address typed, point at the two ways to continue.
      setError(passwordless ? `${detail} ${PASSKEY_FALLBACK_HINT}` : detail);
    } finally {
      setBusy(false);
    }
  }

  function handleSso() {
    // A 302 chain, not an XHR: the browser must follow it (docs/38). The
    // anchor below owns the navigation; this is only for callers that must
    // trigger it programmatically (keyboard shortcuts, tests).
    window.location.assign(OIDC_LOGIN_PATH);
  }

  async function handleDemo() {
    setError(null);
    setBusy(true);
    try {
      await demoLogin();
      navigate(next, { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.code === "demo_disabled") {
        toast.error("Demo is disabled on this server");
      } else {
        setError(messageOf(err, "Could not start the demo"));
      }
    } finally {
      setBusy(false);
    }
  }

  function leaveCodeStep() {
    setChallengeToken(null);
    setCode("");
    setError(null);
  }

  const onCodeStep = challengeToken !== null;
  const recovery = codeKind === "recovery";

  return (
    <AuthShell>
      <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
        <h1 className="text-lg font-semibold tracking-tight">Welcome back</h1>
        {error !== null && <ErrorPanel message={error} />}

        {!onCodeStep && (
          <>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="Email"
              required
              autoComplete="email"
              className={AUTH_FIELD}
            />
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
              required
              autoComplete="current-password"
              className={AUTH_FIELD}
            />
            <Button type="submit" variant="primary" size="lg" loading={busy} className="w-full">
              <LogIn className="size-4" aria-hidden />
              Log in
            </Button>
            <Button
              type="button"
              variant="outline"
              size="lg"
              loading={busy}
              className="w-full"
              onClick={() => void handlePasskey()}
            >
              <KeyRound className="size-4" aria-hidden />
              Sign in with a passkey
            </Button>
            {ssoEnabled && (
              <a
                href={OIDC_LOGIN_PATH}
                onClick={(e) => {
                  e.preventDefault();
                  handleSso();
                }}
                className="relative inline-flex h-11 w-full select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border border-control bg-transparent text-sm font-medium text-text transition-colors duration-200 hover:border-accent/70 hover:bg-veil/[0.04] hover:text-accent"
              >
                <ShieldCheck className="size-4" aria-hidden />
                Sign in with SSO
              </a>
            )}
          </>
        )}

        {onCodeStep && (
          <div className="space-y-3">
            <p className="text-sm text-muted">
              {recovery
                ? "Enter one of the recovery codes you saved when you turned on two-factor authentication."
                : "Enter the 6-digit code from your authenticator app."}
            </p>
            <label htmlFor="second-factor-code" className="block text-xs font-medium text-muted">
              {recovery ? "Recovery code" : "Authentication code"}
            </label>
            <input
              id="second-factor-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder={recovery ? "XXXXX-XXXXX" : "123456"}
              autoComplete="one-time-code"
              autoFocus
              className={AUTH_FIELD}
            />
            <Button type="submit" variant="primary" size="lg" loading={busy} className="w-full">
              <ShieldCheck className="size-4" aria-hidden />
              Verify and sign in
            </Button>
            <div className="flex items-center justify-between gap-2 text-xs">
              <button
                type="button"
                className="text-accent hover:underline"
                onClick={() => {
                  setCodeKind(recovery ? "totp" : "recovery");
                  setCode("");
                  setError(null);
                }}
              >
                {recovery ? "Use a 6-digit code instead" : "Use a recovery code instead"}
              </button>
              <button type="button" className="text-muted hover:underline" onClick={leaveCodeStep}>
                Back
              </button>
            </div>
          </div>
        )}

        <div className="flex items-center gap-3 text-xs text-muted" role="separator">
          <span className="h-px flex-1 bg-border" />
          or
          <span className="h-px flex-1 bg-border" />
        </div>
        <Button
          type="button"
          variant="ghost"
          size="lg"
          loading={busy}
          className="w-full"
          onClick={() => void handleDemo()}
        >
          <Rocket className="size-4" aria-hidden />
          Explore the demo
        </Button>
        <p className="text-center text-xs text-muted">
          demo@fintracker.local · demo-password
        </p>

        <p className="text-sm text-muted">
          No account?{" "}
          <Link
            to="/register"
            state={location.state}
            className="font-medium text-accent hover:underline"
          >
            Register
          </Link>
        </p>
      </form>
    </AuthShell>
  );
}
