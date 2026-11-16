"""Hypothesis properties for the dedup canonical string + hash (docs/03).

`build_dedup_hash` decides whether an imported row is a duplicate of one seen
before: the UNIQUE constraint spans pending+committed, so this hash IS the
dedup key. The contract is the CANONICAL STRING (w2-contract #4): account,
date, amount quantized to 2dp, description with collapsed whitespace — and
nothing else (posting_date, external_ref, currency deliberately do not
participate, so a re-export with different metadata still dedups).

No database: RawTransaction is a frozen dataclass, the hash is pure.
"""

from datetime import date as dt_date
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.importers.base import RawTransaction, build_dedup_hash

settings.register_profile(
    "fin_tracker", deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile("fin_tracker")

HEX_64 = frozenset("0123456789abcdef")

account_ids = st.integers(min_value=1, max_value=10**9)
dates = st.dates(dt_date(1990, 1, 1), dt_date(2035, 12, 31))
# Quantized-to-cents magnitudes plus sub-cent noise, so the amount property
# exercises the quantize boundary rather than only already-clean values.
amounts = st.decimals(
    allow_nan=False,
    allow_infinity=False,
    min_value=Decimal("-100000000"),
    max_value=Decimal("100000000"),
    places=4,
)
# Real descriptions: internal runs of whitespace, leading/trailing padding,
# tab/newline separators — the canonical string must fold all of them.
descriptions = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=0,
    max_size=120,
)


def _txn(description: str, amount: Decimal, when: dt_date) -> RawTransaction:
    return RawTransaction(date=when, amount_native=amount, raw_description=description)


@given(account_ids, dates, amounts, descriptions)
def test_hash_is_sixty_four_lowercase_hex(
    account_id: int, when: dt_date, amount: Decimal, description: str
) -> None:
    """sha256 hexdigest in, exactly 64 lowercase hex chars out — the shape the
    dedup column and every golden fixture assume."""
    digest = build_dedup_hash(account_id, _txn(description, amount, when))
    assert len(digest) == 64
    assert set(digest) <= HEX_64


@given(account_ids, dates, amounts, descriptions)
def test_hash_is_deterministic(
    account_id: int, when: dt_date, amount: Decimal, description: str
) -> None:
    """Same input, same hash — two parses of one file must agree row-for-row."""
    txn = _txn(description, amount, when)
    assert build_dedup_hash(account_id, txn) == build_dedup_hash(account_id, txn)


@given(account_ids, dates, amounts, st.lists(descriptions, min_size=2, max_size=2))
def test_whitespace_variants_of_one_description_collapse_to_one_row(
    account_id: int,
    when: dt_date,
    amount: Decimal,
    descriptions_pair: list[str],
) -> None:
    """The same words with different padding/separators are ONE transaction.

    This is the real-world dedup case: an OFX re-export whose NAME field gains
    a trailing space must not create a second ledger row.
    """
    left, right = descriptions_pair
    if " ".join(left.split()) != " ".join(right.split()):
        return  # not a whitespace-variant pair; nothing to prove
    assert build_dedup_hash(account_id, _txn(left, amount, when)) == build_dedup_hash(
        account_id, _txn(right, amount, when)
    )


@given(account_ids, dates, amounts, descriptions)
def test_sub_cent_amount_noise_does_not_fork_a_row(
    account_id: int,
    when: dt_date,
    amount: Decimal,
    description: str,
) -> None:
    """Amounts quantizing to the same cents are the same transaction — the
    canonical string carries the 2dp quantize, so 1.00 and 1.004 dedup."""
    clean = amount.quantize(Decimal("0.01"))
    noisy = clean + Decimal("0.004")
    assert build_dedup_hash(account_id, _txn(description, clean, when)) == build_dedup_hash(
        account_id, _txn(description, noisy, when)
    )


@given(account_ids, account_ids, dates, amounts, descriptions)
def test_account_and_date_participate_in_the_key(
    account_id_a: int,
    account_id_b: int,
    when: dt_date,
    amount: Decimal,
    description: str,
) -> None:
    """The same payee/same day/same amount in two accounts is two rows —
    account_id is part of the canonical string, not a scoping afterthought."""
    if account_id_a == account_id_b:
        return
    assert build_dedup_hash(account_id_a, _txn(description, amount, when)) != build_dedup_hash(
        account_id_b, _txn(description, amount, when)
    )


@given(
    account_ids,
    st.tuples(
        st.dates(dt_date(1990, 1, 1), dt_date(2034, 12, 31)),
        st.dates(dt_date(1990, 1, 1), dt_date(2034, 12, 31)),
    ).filter(lambda p: p[0] != p[1]),
    amounts,
    descriptions,
)
def test_date_participates_in_the_key(
    account_id: int,
    date_pair: tuple[dt_date, dt_date],
    amount: Decimal,
    description: str,
) -> None:
    """Two charges on different days are two rows even when amount and payee
    match exactly (the monthly-subscription pattern)."""
    day_a, day_b = date_pair
    assert build_dedup_hash(account_id, _txn(description, amount, day_a)) != build_dedup_hash(
        account_id, _txn(description, amount, day_b)
    )


@given(account_ids, dates, amounts, descriptions, st.characters(blacklist_categories=("Cs",)))
def test_metadata_outside_the_canonical_string_is_ignored(
    account_id: int,
    when: dt_date,
    amount: Decimal,
    description: str,
    extra: str,
) -> None:
    """posting_date / external_ref / currency do NOT participate (w2-contract
    #4): a re-export that stamps new FITIDs or a posting lag must still dedup."""
    base = _txn(description, amount, when)
    enriched = RawTransaction(
        date=when,
        amount_native=amount,
        raw_description=description,
        currency=extra or None,
        posting_date=when,
        external_ref=extra or None,
    )
    assert build_dedup_hash(account_id, base) == build_dedup_hash(account_id, enriched)
