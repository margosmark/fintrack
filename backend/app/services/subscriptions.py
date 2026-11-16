"""Subscription tracker — detect recurring transactions.

Scans committed transactions for repeating payees at regular intervals.
Returns detected subscriptions with frequency, cost, and next expected date.

**M48-S3 (docs/33 Phase 3): the `GET /subscriptions` route that called this
was removed** — detection scores rows the browser already holds, and
`frontend/src/lib/s3Engine.ts::subscriptionsReport` is the product path now —
and **M137 P1-2 re-mounted that route** (`app/api/subscriptions.py`), so the
server callers today are `POST /bills` (M55, docs/25), which resolves an
optional `from_subscription_id` through this same scan, and `GET /subscriptions`.
It stays as the reference
implementation the TS port was written against (its rounding, window and
grouping rules are what the golden fixture
`frontend/tests/fixtures/s3-vectors.json` was captured from) and
`tests/test_m29_aggregates.py` keeps pinning its staging/window semantics.

**M170 (docs/36 §M170): the cash-flow calendar no longer calls this module.**
It was the calendar's `subscription` series verbatim, which is how a detector
GUESS rendered next to the real bill for the same subscription (and a bare
`Electronic Funds Transfer` rendered at all) — a prompt feeding a surface that
reads as fact. The calendar now reads
`services/confirmed_subscriptions.py` (bills + user-accepted recurring). The
callers left here are `POST /bills`' autofill resolution and the M137
re-mounted `GET /subscriptions`, both of which are asking "what looks
recurring?", i.e. both are prompts — this module's output is a CANDIDATE
everywhere, never a confirmed subscription.
"""

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.import_batches import ImportBatch
from app.models.transactions import Transaction
from app.services.txn_filters import identity_tx_conditions, today_et


def _months_back(d: date, months: int) -> date:
    """First day of the month `months` back from d (calendar month start)."""
    total = d.year * 12 + (d.month - 1) - months
    return date(total // 12, total % 12 + 1, 1)


def normalize_payee_key(payee: str) -> str:
    """Normalize payee for grouping: upper, whitespace-free, un-numbered.

    The ONE grouping normalization in this codebase: M96 detection
    (`services/recurring.py`), the bill matcher (`services/bill_match.py`) and
    the `match_terms` family all import it instead of inventing a second one,
    so "SUPERSTORE 1234" and "Superstore #1234" collapse to the same key on
    every path. Behaviour is byte-identical to the TS port
    (`frontend/src/lib/payeeKey.ts`), which the s3 parity vectors re-check.

    **Whitespace is not identity (M165).** Statement formats space the same
    merchant differently — Mark's ledger holds "GOOGLE *YouTubePremium",
    "Google *Youtubepremium" and "Google*Youtubepremium" as three spellings of
    one payee, and the detector split them into two candidate groups because
    the space before the `*` survived normalization — so every whitespace run
    is dropped BEFORE the trailing `#?digits` strip (which is what makes
    "GROCERY MART #12" and "GROCERYMART 12" one key too).

    Case folding, whitespace and trailing branch/store numbers, and NOTHING
    else: no fuzzy matching, no punctuation folding — "A *B" and "AB" are
    still two different keys, because a shared normalize that guesses is two
    normalizes again.
    """
    return re.sub(r"#?\d+$", "", re.sub(r"\s+", "", payee.upper()))


#: M170 — the detector-refusal class (docs/24 §M27, docs/36 §M170).
#:
#: Statement-generic money-movement descriptors are NOT merchants. Mark's
#: ledger carried `Electronic Funds Transf… -$359.08` as a "subscription"
#: because a bank statement prints the transfer TYPE in the payee column —
#: the money IS an EFT, and the actual merchant (when there is one) is the
#: strip after it, which is exactly why no part of the descriptor is a
#: subscription identity to group on. Cadence and amount fit cannot rescue
#: it either: a rent-sized transfer every month is still a transfer.
#:
#: Deterministic on purpose (the M166 country-map shape — no LLM, no fuzzy
#: match): each entry is a PREFIX of the normalized payee key
#: (`normalize_payee_key` output — uppercased, whitespace-free), so the test
#: is one `startswith` and the list stays readable as English. Deliberately
#: small: this is a refusal list, and every entry added to it is a payee the
#: detector may never surface, so it grows only for descriptors that are
#: unambiguously not a merchant.
#:
#: Scope: the M27 DETECTOR's input filter only (`detect_subscriptions`, which
#: feeds `POST /bills`' autofill resolution and `GET /subscriptions`). The
#: M96/M138 prompt surfaces keep their candidates on purpose — they are
#: prompts, and their own fixture pins that a `PAYROLL` group still earns a
#: row there (docs/09 §Bills you probably have); a user who ACCEPTS such a
#: group has asserted it, and user-asserted state is confirmed state
#: (`services/confirmed_subscriptions.py`) regardless of this list.
STATEMENT_GENERIC_PREFIXES: tuple[str, ...] = (
    "ELECTRONICFUNDSTRANSFER",
    "EFT",
    "INTERACE-TRANSFER",
    "INTERACTRANSFER",
    "E-TRANSFER",
    "TAXPAYMENT",
    "PAYROLL",
)


def is_statement_generic(payee_key: str) -> bool:
    """True when `payee_key` (a `normalize_payee_key` output) names a
    statement-generic money movement rather than a merchant — the M170
    detector-refusal class.

    A prefix test on the normalized key: `ELECTRONICFUNDSTRANSFER 5551`,
    `INTERAC e-Transfer - 100` and `PAYROLL DEPOSIT ACME` all refuse, whatever
    their amounts and gaps look like.
    """
    return any(payee_key.startswith(prefix) for prefix in STATEMENT_GENERIC_PREFIXES)


def _detect_frequency(dates: list[date]) -> str:
    """Detect payment frequency from sorted date list."""
    if len(dates) < 2:
        return "unknown"
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    avg_gap = sum(gaps) / len(gaps)
    if avg_gap <= 10:
        return "weekly"
    if avg_gap <= 20:
        return "biweekly"
    if avg_gap <= 40:
        return "monthly"
    if avg_gap <= 100:
        return "quarterly"
    return "yearly"


def _next_expected(dates: list[date], frequency: str) -> date | None:
    """Estimate next payment date from last known + frequency."""
    if not dates:
        return None
    last = max(dates)
    offsets = {
        "weekly": 7,
        "biweekly": 14,
        "monthly": 30,
        "quarterly": 91,
        "yearly": 365,
    }
    delta = offsets.get(frequency, 30)
    return last + timedelta(days=delta)


def detect_subscriptions(session: Session, user_id: int) -> list[dict]:
    """Detect recurring spending patterns from committed transactions.

    The scan is bounded to a trailing 13-month window (docs/29 F10/F1,
    relative to `today_et()`) — stale payees from years-old imports must not
    masquerade as active subscriptions.
    """
    window_start = _months_back(today_et(), 12)  # first day 12 months ago → 13 months inclusive
    rows = session.exec(
        select(Transaction)
        .outerjoin(ImportBatch, Transaction.import_batch_id == ImportBatch.id)
        .where(
            *identity_tx_conditions(user_id),
            Transaction.amount_native < 0,
            Transaction.date >= window_start,
        )
        .order_by(Transaction.posting_date)
    ).all()

    # Group by normalized payee. posting_date is nullable (docs/02) — manual
    # and demo-seeded rows often carry only `date`, so fall back to it.
    # Rows with neither date are skipped (never crash the endpoint).
    groups: dict[str, list[dict]] = defaultdict(list)
    for tx in rows:
        key = normalize_payee_key(tx.normalized_payee or tx.raw_description or "")
        if not key:
            continue
        # M170: a statement-generic money movement is not a merchant, so it
        # is never a subscription — however well its amounts and gaps fit.
        if is_statement_generic(key):
            continue
        effective = tx.posting_date
        if isinstance(effective, datetime):
            effective = effective.date()
        if effective is None:
            effective = tx.date
            if isinstance(effective, datetime):
                effective = effective.date()
        if effective is None:
            continue
        groups[key].append(
            {
                "date": effective,
                "amount": abs(tx.amount_native),
                "payee": tx.normalized_payee or tx.raw_description or "",
            }
        )

    subscriptions = []
    for _payee_key, entries in groups.items():
        if len(entries) < 2:
            continue
        dates = sorted(e["date"] for e in entries)
        amounts = [e["amount"] for e in entries]
        freq = _detect_frequency(dates)
        if freq == "unknown":
            continue
        avg_amount = sum(amounts) / len(amounts)
        annual = avg_amount * {
            "weekly": 52,
            "biweekly": 26,
            "monthly": 12,
            "quarterly": 4,
            "yearly": 1,
        }.get(freq, 12)
        subscriptions.append(
            {
                "payee": entries[0]["payee"],
                "frequency": freq,
                "avg_amount": avg_amount.quantize(Decimal("0.01")),
                "annual_cost": annual.quantize(Decimal("0.01")),
                "occurrences": len(entries),
                "last_date": max(dates),
                "next_expected": _next_expected(dates, freq),
            }
        )

    subscriptions.sort(key=lambda s: s["annual_cost"], reverse=True)
    return subscriptions
