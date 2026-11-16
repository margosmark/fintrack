// useTermSuggestions — M122 T1 (docs/25 §M122): live match-term suggestions
// from the user's OWN committed ledger.
//
// The gate is the contract: an EMPTY draft asks for the top payee groups (the
// "opening the editor" candidates), a draft of ≥3 chars asks for the groups it
// would match, and a 1–2 char draft asks for NOTHING — at that length the
// ledger cannot confirm anything, so a fetch per keystroke would be pure
// noise. The draft is debounced so fast typing costs one request, not six.
//
// The server does the heavy lifting (grouping, ranking, excluding the bill's
// current matched set); this hook only owns WHEN to ask and never re-offers a
// term the user already committed as a chip.

import { useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  BILL_SUGGEST_MIN_CHARS,
  suggestTerms,
  type BillMatchMode,
  type BillSuggestTerm,
} from "../../api/bills";

const DEBOUNCE_MS = 200;

export interface TermSuggestionsOptions {
  /** False while the editor is closed — no fetch, no cache churn. */
  enabled: boolean;
  /** The text still typed in the term input (not yet a chip). */
  draft: string;
  /** The bill being edited; `null`/omitted = the create form. */
  billId?: number | null;
  matchMode: BillMatchMode;
  /** Committed chips — a suggestion that equals one is noise. */
  terms: string[];
}

export interface TermSuggestions {
  suggestions: BillSuggestTerm[];
  /** True while a request is in flight (the field may show a hint row). */
  loading: boolean;
}

export function useTermSuggestions({
  enabled,
  draft,
  billId,
  matchMode,
  terms,
}: TermSuggestionsOptions): TermSuggestions {
  const trimmed = draft.trim();
  const gated = trimmed === "" || trimmed.length >= BILL_SUGGEST_MIN_CHARS;

  const [debounced, setDebounced] = useState(trimmed);
  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(trimmed), DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [trimmed]);

  const query = useQuery({
    queryKey: ["bill-suggest-terms", billId ?? null, debounced, matchMode],
    queryFn: () => suggestTerms({ q: debounced, billId, matchMode }),
    enabled: enabled && gated,
    // Smooth over the debounce: the previous answer stays visible until the
    // new one lands, exactly like the imports suggest-profile flow.
    placeholderData: keepPreviousData,
    staleTime: 30_000,
    retry: false,
  });

  const chips = new Set(terms);
  return {
    suggestions: (query.data ?? []).filter((item) => !chips.has(item.normalized_payee)),
    loading: query.isFetching,
  };
}
