import { Link } from "react-router-dom";
import { BookOpen, PlayCircle } from "lucide-react";

import { Card, CardTitle } from "../components/ui/Card";
import { requestTour } from "../components/tour/tourState";
import { GUIDES } from "./helpGuides";

/**
 * Help hub: the three quickstart tracks (M89 L2) above the flat guide list.
 *
 * The tracks are the three real onboarding paths — look around the demo,
 * bring your own data, or hand it to an agent — and every step is a working
 * link, so the page is executable rather than descriptive.
 *
 * Track 1 deliberately routes AROUND the demo write guard (M92 F-M91-1,
 * docs/20): the demo tenant answers `403 demo_account_restricted` to
 * `POST /imports/upload`, so the import step points at the `first-import`
 * walkthrough instead of `/imports` — the teach moment stays inside the demo
 * without promising a live upload that cannot happen.
 */

interface TrackStep {
  label: string;
  /** In-app route (client-side Link). */
  to?: string;
  /** Same-origin resource (downloadable sample). */
  href?: string;
  hint?: string;
}

interface Track {
  n: string;
  title: string;
  blurb: string;
  steps: TrackStep[];
}

const TRACKS: Track[] = [
  {
    n: "1",
    title: "Try it in 2 minutes",
    blurb: "Nothing to set up — the demo household is already loaded.",
    steps: [
      {
        label: "Look around the dashboard",
        to: "/dashboard",
        hint: "net worth, month net, spending insights — all seeded",
      },
      {
        label: "Watch how an import lands",
        to: "/help/first-import",
        hint: "upload → preview → commit, screenshot by screenshot. The demo can't upload — it is read-only; take the sample file with you for when you sign up",
        href: "/help/import-samples/sample.ofx",
      },
      {
        label: "Browse the transactions feed",
        to: "/transactions",
        hint: "where imported rows land — the demo's history, already categorised",
      },
    ],
  },
  {
    n: "2",
    title: "Own your data",
    blurb: "For your real money: your account, your history, your backup.",
    steps: [
      { label: "Create your account", to: "/register" },
      { label: "Import your real history", to: "/imports" },
      {
        label: "Turn on export & backup",
        to: "/settings",
        hint: "scheduled export and encrypted backup, under Privacy & data",
      },
    ],
  },
  {
    n: "3",
    title: "Drive it from your agent",
    blurb: "Claude, Cursor or your own script, under a token you issued.",
    steps: [
      { label: "Mint a scoped token", to: "/help/agent-integration" },
      { label: "Point your client at it", to: "/help/configuration" },
      { label: "Read what it did", to: "/help/agent-tour" },
    ],
  },
];

export function Help() {
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      <h1 className="text-xl font-semibold tracking-tight">Help &amp; guides</h1>
      <p className="text-sm text-muted">
        Short, practical guides with screenshots from the demo. Pick a topic —
        every guide shows you where to click and what to expect.
      </p>

      <section aria-labelledby="quickstart-heading" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="quickstart-heading" className="text-lg font-semibold">
            Start here — three ways in
          </h2>
          <button
            type="button"
            onClick={requestTour}
            className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-control px-3 text-sm font-medium text-text transition-colors hover:bg-veil/10"
          >
            <PlayCircle className="size-4 text-accent" aria-hidden />
            Take the tour
          </button>
        </div>
        <p className="text-sm text-muted">
          Three tracks, matching the three reasons people are here. Do one end
          to end — or take the 90-second interface tour first.
        </p>
        <ol className="grid gap-3 sm:grid-cols-3">
          {TRACKS.map((track) => (
            <li key={track.n} className="h-full">
              <Card hover className="h-full p-4">
                <CardTitle>
                  <span className="block space-y-1">
                    <span className="block text-xs font-semibold uppercase tracking-widest text-accent whitespace-nowrap">
                      Track {track.n}
                    </span>
                    <span className="block">{track.title}</span>
                  </span>
                </CardTitle>
                <p className="text-sm text-muted">{track.blurb}</p>
                <ol className="mt-3 space-y-2">
                  {track.steps.map((step, i) => (
                    <li key={step.label} className="text-sm">
                      <span className="mr-1.5 text-muted">{i + 1}.</span>
                      {step.to ? (
                        <Link
                          to={step.to}
                          className="text-accent underline-offset-2 hover:underline"
                        >
                          {step.label}
                        </Link>
                      ) : (
                        <span className="text-text">{step.label}</span>
                      )}
                      {step.href && (
                        <>
                          {" "}
                          <a
                            href={step.href}
                            download
                            className="text-muted underline-offset-2 hover:text-text hover:underline"
                          >
                            (sample file)
                          </a>
                        </>
                      )}
                      {step.hint && (
                        <span className="block text-xs text-muted">
                          {step.hint}
                        </span>
                      )}
                    </li>
                  ))}
                </ol>
              </Card>
            </li>
          ))}
        </ol>
      </section>

      <h2 className="pt-2 text-lg font-semibold">All guides</h2>
      <div className="grid gap-3 sm:grid-cols-2">
        {GUIDES.map((g) => (
          <Link
            key={g.slug}
            to={`/help/${g.slug}`}
            className="outline-none focus-visible:ring-2 focus-visible:ring-accent rounded-xl"
          >
            <Card hover className="h-full p-4">
              <CardTitle>
                <span className="flex items-center gap-2">
                  <BookOpen className="size-4 text-accent" aria-hidden />
                  {g.title}
                </span>
              </CardTitle>
              <p className="text-sm text-muted">{g.blurb}</p>
              <p className="mt-1 text-xs text-muted">
                {g.steps.length} steps · {g.steps.filter((s) => s.image).length}{" "}
                screenshots
              </p>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
