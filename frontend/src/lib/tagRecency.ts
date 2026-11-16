// Session-level tag recency (M104, docs/09 §Tag UX): when a tag picker's
// query is empty it offers the tags you attached most recently FIRST — the
// second tag you add in a sitting is usually the one you just used.
//
// Deliberately NOT persisted: there is no storage contract for this (a reload
// starts the session over) and a stale "recent" list that survived a rename
// or a merge would be worse than none. One module-level stack is shared by
// every picker on the page, which is exactly the "recently-used-first" hint
// the PM spec asked for as a client-side ordering hint.

/** How many ids the session remembers (enough for a labelling sitting). */
const MAX_RECENT = 12;

let recent: number[] = [];

/** Record that a tag was actually attached (call on SUCCESS, not on click). */
export function noteTagUsed(tagId: number): void {
  recent = [tagId, ...recent.filter((id) => id !== tagId)].slice(0, MAX_RECENT);
}

/**
 * Order tags by recency first, name order after — the array must arrive in
 * name order (the API's order), so ties and unknown ids keep it stable.
 */
export function byRecency<T extends { id: number; name: string }>(tags: T[]): T[] {
  if (recent.length === 0) return tags;
  const rank = new Map(recent.map((id, index) => [id, index]));
  return [...tags].sort((a, b) => {
    const ra = rank.get(a.id) ?? Number.MAX_SAFE_INTEGER;
    const rb = rank.get(b.id) ?? Number.MAX_SAFE_INTEGER;
    if (ra !== rb) return ra - rb;
    return 0; // both unknown: keep the incoming (name) order
  });
}

/** Test hook — the stack is session state, so a suite must be able to reset it. */
export function resetTagRecency(): void {
  recent = [];
}
