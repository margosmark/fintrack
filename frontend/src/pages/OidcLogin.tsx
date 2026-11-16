import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ShieldCheck, Clock } from "lucide-react";
import { AuthShell } from "../components/layout/AuthShell";
import { Button } from "../components/ui/Button";
import { ErrorPanel, EmptyState } from "../components/ui/EmptyState";
import { ApiError, oidcPreview } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { postAuthRedirect } from "../auth/postAuthRedirect";
import type { OidcPreview } from "../types";

/**
 * OIDC landing page — route `/login/oidc` (docs/38 "OIDC SSO").
 *
 * The backend callback never puts a token in the URL: it sets the short-lived
 * httpOnly `oidc_pending` cookie and 302s here. This page reads that cookie
 * through `GET /auth/oidc/preview`, then either enters the app outright
 * (already-linked identity) or asks for the ONE explicit click that creates
 * the link — an IdP can never create a fin_tracker account by itself
 * (docs/38 decision 7).
 *
 * `?error=<code>` is a browser-navigation failure (not an envelope), mapped
 * here to human copy.
 */

/** Redirect error codes the backend can append (docs/38 "Redirect error codes"). */
const REDIRECT_ERRORS: Record<string, string> = {
  oidc_unknown_account:
    "No fin_tracker account matches that sign-in email — sign in with your password once to link it.",
  oidc_state_mismatch:
    "That sign-in response did not match this browser session — start again from the login page.",
  oidc_provider_error:
    "The identity provider reported an error — start again from the login page.",
  oidc_token_invalid:
    "The identity provider's response could not be verified — start again from the login page.",
  oidc_email_required:
    "Your identity provider did not share an email address — enable the email claim and try again.",
};

/** Unmapped codes still deserve a sentence instead of a bare code string. */
function redirectMessage(code: string): string {
  return (
    REDIRECT_ERRORS[code] ?? "Sign-in failed — start again from the login page."
  );
}

type Phase = "loading" | "confirm" | "expired" | "failed";

export function OidcLogin() {
  const { oidcConfirm } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  // Same destination rule as the login page: a deep link requested before the
  // redirect survives the trip (docs/09 / M68).
  const next = postAuthRedirect(location.state);
  const [params] = useSearchParams();
  const redirectError = params.get("error");

  const [phase, setPhase] = useState<Phase>(redirectError === null ? "loading" : "confirm");
  const [preview, setPreview] = useState<OidcPreview | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // A `?error=` redirect never carried a pending cookie worth reading.
    if (redirectError !== null) return;
    let cancelled = false;
    setPhase("loading");
    oidcPreview()
      .then(async (data) => {
        if (cancelled) return;
        if (data.linked) {
          // Familiar one-click SSO (docs/38 decision 7): no confirmation card
          // for an identity that is already linked.
          await confirmSession();
          return;
        }
        setPreview(data);
        setPhase("confirm");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.code === "oidc_pending_expired") {
          setPhase("expired");
          return;
        }
        setFailure(err instanceof Error ? err.message : "Could not read the sign-in request");
        setPhase("failed");
      });
    return () => {
      cancelled = true;
    };
  }, [redirectError]);

  async function confirmSession(): Promise<void> {
    setBusy(true);
    setFailure(null);
    try {
      // Stores the access token and applies the user exactly like login does,
      // so the query cache clears on identity change (docs/38 "Frontend").
      await oidcConfirm();
      navigate(next, { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.code === "oidc_pending_expired") {
        setPhase("expired");
        return;
      }
      setFailure(err instanceof Error ? err.message : "Could not complete sign-in");
      setPhase("failed");
    } finally {
      setBusy(false);
    }
  }

  if (redirectError !== null) {
    return (
      <AuthShell>
        <div className="space-y-4" data-testid="oidc-error">
          <h1 className="text-lg font-semibold tracking-tight">Sign-in did not complete</h1>
          <ErrorPanel message={redirectMessage(redirectError)} />
          <Link
            to="/login"
            className="inline-flex h-11 w-full items-center justify-center rounded-lg border border-control text-sm font-medium text-text transition-colors hover:border-accent/70 hover:text-accent"
          >
            Back to sign in
          </Link>
        </div>
      </AuthShell>
    );
  }

  if (phase === "loading") {
    return (
      <AuthShell>
        <div className="space-y-4">
          <h1 className="text-lg font-semibold tracking-tight">Completing sign-in</h1>
          <p className="text-sm text-muted">Checking your sign-in request…</p>
        </div>
      </AuthShell>
    );
  }

  if (phase === "expired") {
    return (
      <AuthShell>
        <div className="space-y-4" data-testid="oidc-expired">
          <EmptyState
            icon={<Clock />}
            title="Your sign-in link expired"
            hint="Single-use sign-in requests last 10 minutes. Start again from the login page."
            preview={{
              none: "an authentication hand-off status, not a data surface",
            }}
            action={{
              label: "Back to sign in",
              to: "/login",
              ariaLabel: "Back to sign in — Your sign-in link expired",
            }}
          />
          <Link
            to="/login"
            className="inline-flex h-11 w-full items-center justify-center rounded-lg border border-control text-sm font-medium text-text transition-colors hover:border-accent/70 hover:text-accent"
          >
            Back to sign in
          </Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <div className="space-y-4">
        <h1 className="text-lg font-semibold tracking-tight">Confirm your sign-in</h1>
        {failure !== null && <ErrorPanel message={failure} />}
        {phase === "confirm" && preview !== null && (
          <>
            <p className="text-sm text-muted">
              Continue as <span className="font-medium text-text">{preview.email}</span>
            </p>
            <p className="text-xs leading-relaxed text-muted">
              Link this provider to your fin_tracker account — from now on this button signs you
              in with one click. Nothing is created until you continue.
            </p>
            <Button
              type="button"
              variant="primary"
              size="lg"
              loading={busy}
              className="w-full"
              onClick={() => void confirmSession()}
            >
              <ShieldCheck className="size-4" aria-hidden />
              Continue
            </Button>
          </>
        )}
        <Link
          to="/login"
          className="block text-center text-xs text-muted hover:text-accent hover:underline"
        >
          Use a password instead
        </Link>
      </div>
    </AuthShell>
  );
}
