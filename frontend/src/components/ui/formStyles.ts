export const AUTH_FIELD =
  "w-full rounded-lg border border-control bg-surface px-3 py-2.5 text-sm outline-none transition-all duration-200 placeholder:text-muted field-ring hover:border-accent/70";

/**
 * Canonical text input / select class. Every owned component and the pages
 * (FPages) build on this single base, appending size/state modifiers only —
 * see scout #8 (the per-file FIELD / INPUT_CLASS strings are now this).
 */
export const inputClass =
  "field-ring rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";
