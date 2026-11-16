// payeeKey — the ONE payee-grouping normalization, in the browser.
//
// Byte-identical port of
// `backend/app/services/subscriptions.py::normalize_payee_key`, which the
// detector, the bill matcher and the `match_terms` family all share. Two
// surfaces need the SAME answer the server gives without asking it:
//
//   * the retired M27 subscriptions fallback (`lib/s3Engine.ts`, removed by
//     M160) grouped by this key — the key itself is unchanged;
//   * `lib/billCoverage.ts` asks "is this candidate already a bill?" by
//     comparing a candidate against a bill's term list — the comparison the
//     server's bill matcher would make, so the card never claims a coverage
//     the `/bills/{id}/detail` scan would deny (M165).
//
// Rule (docs/25 §M116 D5): UPPER-case, every whitespace run dropped, then the
// trailing `#?digits` strip. Whitespace is NOT identity — statement formats
// space the same merchant differently ("GOOGLE *YouTubePremium" vs
// "Google*Youtubepremium"), and splitting on that space is exactly the
// manufactured duplicate M165 fixed. Nothing else is folded: no fuzzy match,
// no punctuation folding, because a normalizer that guesses is two
// normalizers again.
export function normalizePayeeKey(payee: string): string {
  return payee.toUpperCase().replace(/\s+/g, "").replace(/#?\d+$/, "");
}
