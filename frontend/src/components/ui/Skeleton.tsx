// Skeleton shimmer primitives (ui-contract move #6): every loading state in
// the app is a shimmer placeholder, never a spinner or "Loading…" text.

import type { CSSProperties } from "react";

export function Skeleton({
  className = "",
  style,
}: {
  className?: string;
  style?: CSSProperties;
}) {
  return <div className={`skeleton ${className}`} style={style} aria-hidden />;
}

/** A card-shaped placeholder matching the glass chart cards. */
export function SkeletonCard({ className = "h-64" }: { className?: string }) {
  return (
    <div className={`glass rounded-xl p-4 ${className}`} aria-hidden>
      <Skeleton className="h-4 w-40" />
      <div className="mt-4 space-y-2">
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-11/12" />
        <Skeleton className="h-3 w-4/5" />
      </div>
    </div>
  );
}

/** Table body placeholder: `rows` skeleton lines with natural width drift. */
export function SkeletonRows({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2.5 py-2" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton
          key={i}
          className="h-5"
          style={{ width: `${94 - ((i * 13) % 38)}%` }}
        />
      ))}
    </div>
  );
}
