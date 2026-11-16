// NotFound (M68, docs/09 "Routes"): the branded catch-all for every URL the
// route table does not match.
//
// Without it, React Router's data router renders its own error boundary —
// the developer-facing "Unexpected Application Error!" — which is neither
// branded nor recoverable. This page keeps the AppShell chrome (it is a child
// of the guarded shell route) and offers both recovery moves: go somewhere
// useful, or step back to where you came from.
import { ArrowLeft, Compass } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "../components/ui/Button";

export function NotFound() {
  const navigate = useNavigate();
  return (
    <section
      data-testid="not-found"
      className="flex min-h-[60vh] flex-col items-center justify-center gap-4 px-4 text-center"
    >
      <span
        aria-hidden
        className="bg-gradient-to-br from-accent to-accent-soft bg-clip-text text-6xl font-bold tracking-tighter text-transparent"
      >
        404
      </span>
      <div className="flex size-12 items-center justify-center rounded-xl bg-veil/[0.06] text-muted">
        <Compass className="size-6" aria-hidden />
      </div>
      <div className="space-y-1.5">
        <h1 className="text-xl font-semibold tracking-tight">Page not found</h1>
        <p className="max-w-md text-sm text-muted">
          That page doesn’t exist — the link may be out of date, or the address
          was typed wrong. Nothing was changed.
        </p>
      </div>
      <div className="flex flex-wrap items-center justify-center gap-2">
        <Button variant="primary" onClick={() => navigate("/dashboard")}>
          Go to Dashboard
        </Button>
        <Button variant="outline" onClick={() => navigate(-1)}>
          <ArrowLeft className="size-4" aria-hidden />
          Back
        </Button>
      </div>
    </section>
  );
}
