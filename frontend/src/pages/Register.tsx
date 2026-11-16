import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useLocation } from "react-router-dom";
import { UserRoundPlus } from "lucide-react";
import { AuthShell } from "../components/layout/AuthShell";
import { Button } from "../components/ui/Button";
import { ErrorPanel } from "../components/ui/EmptyState";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../api/client";
import { AUTH_FIELD } from "../components/ui/formStyles";

/**
 * M87 (docs/29 F15): registration is no longer a sign-in. The endpoint answers
 * the SAME body whether or not the address was taken — a `201 + session` vs
 * `409 duplicate_email` split told any visitor which accounts exist — so this
 * page has exactly one outcome to render: "accepted, now sign in". For a free
 * address the account and its password already exist; for a taken one nothing
 * changed and the existing password still applies.
 */
const ACCEPTED_DETAIL =
  "If this address was free, an account now exists with the password you just chose. If it was already in use, nothing changed. Either way, sign in to continue.";

export function Register() {
  const { register } = useAuth();
  // M90 (docs/43 §13): deep links arrive here as route state from /login
  // (e.g. /sharing/accept?token=…) — forward it back to /login so the
  // invite token survives the sign-in hop AFTER registration.
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (password !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    try {
      await register(email, password);
      setAccepted(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Registration failed");
    } finally {
      setBusy(false);
    }
  }

  if (accepted) {
    return (
      <AuthShell>
        <div className="space-y-4">
          <h1 className="text-lg font-semibold tracking-tight">Registration accepted</h1>
          <p className="text-sm text-muted">{ACCEPTED_DETAIL}</p>
          <Link to="/login" state={location.state} className="block">
            <Button type="button" variant="primary" size="lg" className="w-full">
              Sign in
            </Button>
          </Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
        <h1 className="text-lg font-semibold tracking-tight">Create your account</h1>
        {error !== null && <ErrorPanel message={error} />}
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
          autoComplete="new-password"
          className={AUTH_FIELD}
        />
        <input
          type="password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          placeholder="Confirm password"
          required
          autoComplete="new-password"
          className={AUTH_FIELD}
        />
        <Button type="submit" variant="primary" size="lg" loading={busy} className="w-full">
          <UserRoundPlus className="size-4" aria-hidden />
          Create account
        </Button>
        <p className="text-sm text-muted">
          Already registered?{" "}
          <Link to="/login" state={location.state} className="font-medium text-accent hover:underline">
            Log in
          </Link>
        </p>
      </form>
    </AuthShell>
  );
}
