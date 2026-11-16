// Route-loading surfaces (M49, docs/09 "Route loading & code-splitting").
//
// Every page module is a React.lazy dynamic import, so the FIRST visit to a
// route paints RouteFallback while the chunk streams in — never a blank frame,
// and never a spinner or "Loading…" text (the Skeleton ui-contract: loading
// states are shimmer placeholders). A rejected chunk (stale index.html after a
// deploy, so the hashed asset 404s) surfaces as RouteLoadError instead of a
// white screen.

import { Component } from "react";
import type { ReactNode } from "react";
import { ErrorPanel } from "./EmptyState";
import { Button } from "./Button";
import { Skeleton, SkeletonCard } from "./Skeleton";

/** Shimmer stand-in for the page that is still loading: title + card grid. */
export function RouteFallback() {
  return (
    <div
      role="status"
      aria-label="Loading page"
      data-testid="route-fallback"
      className="space-y-4"
    >
      <Skeleton className="h-8 w-56" />
      <div className="grid gap-4 md:grid-cols-2">
        <SkeletonCard className="h-40" />
        <SkeletonCard className="h-40" />
      </div>
      <SkeletonCard className="h-64" />
    </div>
  );
}

interface RouteLoadErrorState {
  error: Error | null;
}

/**
 * Catches a failed route chunk (or a render error thrown while the route first
 * mounts) and shows the standard error panel plus a Reload action — a chunk
 * rejection is cached by React until the document reloads, so retry-in-place
 * cannot succeed.
 */
export class RouteLoadError extends Component<
  { children: ReactNode },
  RouteLoadErrorState
> {
  state: RouteLoadErrorState = { error: null };

  static getDerivedStateFromError(error: Error): RouteLoadErrorState {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return (
      <div data-testid="route-load-error" className="space-y-3">
        <ErrorPanel
          message={`${error.message} — the page could not be loaded, reload to fetch it fresh.`}
        />
        <Button variant="primary" onClick={() => window.location.reload()}>
          Reload
        </Button>
      </div>
    );
  }
}
