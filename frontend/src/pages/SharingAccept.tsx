// SharingAccept — the invitee's landing page (M90, docs/43 §13).
//
// The token comes from the QUERY STRING only (never localStorage): the route
// sits behind RequireAuth, so a signed-out visit round-trips through /login
// with `state.from = /sharing/accept?token=…` — and Login/Register forward
// that state to each other, so the token survives sign-in AND register
// (the recipient can create the addressed account first, then land back
// here).
//
// Errors render the envelope VERBATIM — `{detail, code}` — plus a short
// human hint, and NEVER a redirect to /login: `401 sharing_invite_*` answers
// the accept attempt itself (client.ts AUTH_401_SKIP_PATHS keeps it out of
// the refresh dance). After a successful accept: /shared.
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import { MailOpen } from "lucide-react";
import { ApiError } from "../api/client";
import { acceptInvite } from "../api/sharing";
import { Button } from "../components/ui/Button";
import { ErrorPanel } from "../components/ui/EmptyState";
import { fadeUp, staggerContainer } from "../components/ui/motion";

/** Extra human sentence per envelope code — shown BESIDE the verbatim detail. */
const HINTS: Record<string, string> = {
  sharing_invite_expired: "Ask for a new invitation link.",
  sharing_invite_invalid:
    "Sign in with the address the invitation was sent to, or ask for a new link.",
  sharing_invite_used: "This link was already used — ask for a new invitation.",
  sharing_invite_cancelled: "The owner cancelled this invitation.",
  sharing_partner_occupied: "You already share another household.",
  rate_limited: "Too many attempts — try again in a minute.",
};

interface Envelope {
  detail: string;
  code: string | null;
}

export function SharingAccept() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token");
  const [busy, setBusy] = useState(false);
  const [envelope, setEnvelope] = useState<Envelope | null>(null);

  async function confirm() {
    if (token == null) return;
    setBusy(true);
    setEnvelope(null);
    try {
      await acceptInvite(token);
      navigate("/shared", { replace: true });
    } catch (err) {
      setEnvelope(
        err instanceof ApiError
          ? { detail: err.message, code: err.code }
          : { detail: "Could not accept the invitation", code: null },
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <motion.section
      variants={staggerContainer(0.06)}
      initial="hidden"
      animate="show"
      className="mx-auto max-w-lg space-y-6"
    >
      <motion.div variants={fadeUp}>
        <h1 className="text-xl font-semibold tracking-tight">You&apos;ve been invited</h1>
      </motion.div>

      <motion.div variants={fadeUp} className="space-y-4 rounded-xl border border-border p-5">
        <div className="flex items-start gap-3">
          <MailOpen className="mt-0.5 size-5 shrink-0 text-muted" aria-hidden />
          <div className="space-y-2 text-sm text-muted">
            <p>
              Someone wants to share their household finances with this account — a read-only
              view of the accounts they select.
            </p>
            <ul className="list-disc space-y-1 pl-5">
              <li>You will see balances, transactions, categories and budget definitions.</li>
              <li>They choose which accounts — nothing else of theirs is visible.</li>
              <li>You can never change their data; they can revoke access at any time.</li>
              <li>Your own accounts and settings are untouched.</li>
            </ul>
          </div>
        </div>

        {token == null ? (
          <ErrorPanel message="This invitation link is missing its token — ask for a new one." />
        ) : (
          <Button
            type="button"
            variant="primary"
            size="lg"
            className="w-full"
            loading={busy}
            onClick={() => void confirm()}
          >
            Confirm and view shared accounts
          </Button>
        )}

        {envelope != null && (
          <div className="space-y-2">
            {/* The envelope, verbatim (docs/43 §7) — detail AND code. */}
            <ErrorPanel message={envelope.detail} />
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
              {envelope.code != null && (
                <code className="rounded bg-veil px-1.5 py-0.5 font-mono">{envelope.code}</code>
              )}
              {envelope.code != null && HINTS[envelope.code] != null && (
                <span>{HINTS[envelope.code]}</span>
              )}
            </div>
          </div>
        )}
      </motion.div>

      <motion.p variants={fadeUp} className="text-xs text-muted">
        You&apos;ll see whose household it is — and exactly what they shared — on the next
        screen.
      </motion.p>
    </motion.section>
  );
}
