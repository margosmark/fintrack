"""M121 — `match_terms`: the ONE multi-term payee matcher (docs/25 §M121).

Three surfaces answer the same question — *does this transaction belong here?* —

* **bills** (`services/bill_match.py`) — a bill's TERM LIST under its
  `contains | starts_with | exact` mode (docs/25 §M121 D1);
* **categorization rules** (`services/rules_engine.py`) — `exact_payee` and
  `contains` over the rule's pattern (docs/04 layer 1);
* **the recurring detection family** (`api/transactions.py`'s candidate
  exclusions — "is this candidate's merchant one of the merchants that already
  carries a tagged row?").

Each used to carry its own single-term comparison, so the three could drift:
a bill and the rules engine that agreed on paper could disagree in code, and
the drift would be invisible because both still "look right". All three now
call ONE function with ONE signature::

    match_terms(terms, mode, normalize, subject, comparison="contains")

**Arguments**

* `terms`      — the term list (1..N; a single term is the pre-M121 world and
                 behaves identically under both modes).
* `mode`       — ``"any"`` (OR: widening; the DEFAULT — a single-term rule is
                 unchanged by it) or ``"all"`` (AND: every term must hit).
* `normalize`  — maps BOTH sides into the space the comparison runs in. A
                 surface that already normalized its inputs passes `as_is`, so
                 the bill scan keeps its "one normalization per LEDGER ROW,
                 never one per (row, bill)" property (docs/25 §M116 D5).
* `subject`    — the raw payee (or an already-normalized key, with
                 `normalize=as_is`).
* `comparison` — `contains | starts_with | exact`, applied per term AFTER
                 normalization, so a term list never changes what a mode
                 means (docs/25 §M121 D2).

**Answers that are never silent**

* an EMPTY term list matches nothing under either mode (no rule, no claim);
* a term — or a subject — that normalizes to nothing never matches: under
  `all` it fails the whole rule, under `any` it simply never hits. A blank
  term read as "matches everything" is exactly the silent wrong answer the
  API refuses at the seam (`422 validation_error`).

Privacy (docs/11): pure string comparison, no network, no LLM.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from app.services.subscriptions import normalize_payee_key

TermMode = Literal["any", "all"]
TermComparison = Literal["contains", "starts_with", "exact"]

# The rule surface is RATE-LIMITED (docs/25 §M121 D7): ten terms is already a
# wider alias list than any payee needs, and a 500-term rule would be a scan
# cost nobody chose.
MAX_TERMS = 10
MAX_TERM_LENGTH = 120

# Both wire literals, in label order — the API's `Literal` validators and the
# UI's ALL/ANY segmented control read from here so they cannot disagree.
TERM_MODES: tuple[TermMode, ...] = ("any", "all")
TERM_COMPARISONS: tuple[TermComparison, ...] = ("contains", "starts_with", "exact")


def as_is(value: str) -> str:
    """The identity "normalizer": the caller already normalized both sides.

    Passed where the input is a precomputed key (`bill_match`'s `payee_key`,
    the recurring family's `merchant_key`) so the matcher costs no work the
    scan already did.
    """
    return value


def normalized_term_or_none(term: str) -> str | None:
    """`normalize_payee_key(term)`, or None when the term can never match.

    A blank term, or one that normalizes to nothing (`"123"`, `"#7"`), would
    match every row or no row depending on the mode — both are silent wrong
    answers, so the API rejects them as `422 validation_error` instead
    (AGENTS §3: strict validation beats silent tolerance).
    """
    return normalize_payee_key(term or "") or None


def validate_terms(terms: Sequence[str], *, field: str = "match_terms") -> list[str]:
    """Trimmed, bounded, payable term list — or `ValueError` for the 422.

    Order is preserved and duplicates are left alone (they change no answer
    under either mode, so silently dropping one would be a second, unasked-for
    behaviour).
    """
    cleaned = [str(term).strip() for term in terms]
    if not cleaned:
        raise ValueError(f"{field}: must name at least one term")
    if len(cleaned) > MAX_TERMS:
        raise ValueError(f"{field}: at most {MAX_TERMS} terms are allowed")
    for term in cleaned:
        if not term:
            raise ValueError(f"{field}: terms must not be blank")
        if len(term) > MAX_TERM_LENGTH:
            raise ValueError(f"{field}: terms must be at most {MAX_TERM_LENGTH} characters")
        if normalized_term_or_none(term) is None:
            raise ValueError(
                f"{field}: terms must contain letters — digits and # prefixes "
                "alone cannot identify a payee"
            )
    return cleaned


def _term_hits(subject_key: str, term_key: str, comparison: str) -> bool:
    """One term against one already-normalized subject."""
    if not subject_key or not term_key:
        return False
    if comparison == "exact":
        return subject_key == term_key
    if comparison == "starts_with":
        return subject_key.startswith(term_key)
    return term_key in subject_key


def match_terms(
    terms: Sequence[str],
    mode: str,
    normalize: Callable[[str], str],
    subject: str,
    comparison: str = "contains",
) -> bool:
    """Does `subject` satisfy `terms` under `mode`? (the shared contract)

    See the module doc for the semantics; both sides always go through the
    SAME `normalize`, which is what keeps a bill's rule and the recurring
    detector's identity test from ever disagreeing about what a payee IS
    (docs/25 §M116 D5 / §M121 D3).
    """
    subject_key = normalize(subject or "")
    results = [_term_hits(subject_key, normalize(str(term)), comparison) for term in terms]
    if mode == "all":
        # Empty list → False as well: no terms is no rule, never "vacuously
        # true" — a rule that matched everything would be a silent wrong answer.
        return bool(results) and all(results)
    return any(results)
