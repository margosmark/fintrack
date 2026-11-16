"""Hypothesis properties for the OFX/QFX importer (docs/03).

Two sides of one contract:

1. Round-trip fidelity — an OFX document built from arbitrary transactions
   parses back to exactly those amounts, dates and descriptions.
2. The audit L5 envelope — `parse` translates EVERY parser failure into
   `OfxParseError` (which the API maps to a 422), so hostile bytes can never
   surface a raw ValueError/LookupError as a 500. The generator throws real
   OFX skeletons plus random mutations at it.

No database: the importer is a pure bytes -> list[RawTransaction] function
(AGENTS: importers never touch the DB).
"""

from datetime import date as dt_date
from decimal import Decimal

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.importers.base import AccountView
from app.importers.ofx import OfxImporter, OfxParseError

settings.register_profile(
    "fin_tracker", deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile("fin_tracker")

ACCOUNT = AccountView(currency="CAD")

HEADER = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:UTF-8
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>1
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<STMTRS>
<CURDEF>CAD
<BANKACCTFROM>
<BANKID>000999991
<ACCTID>11223344
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260101000000
<DTEND>20261231000000
"""
FOOTER = """</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""

# SGML leaf values are line-delimited: no newlines, no angle brackets.
line_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\r<>"), max_size=60
)
# Two decimals, bounded magnitude: str() of the float ofxparse yields stays
# exact in this range (the importer goes Decimal(str(txn.amount))).
amounts = st.decimals(
    allow_nan=False, allow_infinity=False, min_value=-1000000, max_value=1000000, places=2
)
tx_dates = st.dates(dt_date(2000, 1, 1), dt_date(2035, 12, 31))
nonneg_ints = st.integers(min_value=0, max_value=10**9)


@st.composite
def transactions(draw) -> list[tuple[str, str, str]]:
    """(DTPOSTED, TRNAMT, NAME) triples — the fields `rows_of` reads back."""
    rows = draw(st.lists(st.tuples(tx_dates, amounts, line_text), min_size=1, max_size=8))
    return [(d.strftime("%Y%m%d"), f"{a:.2f}", name) for d, a, name in rows]


def _doc(rows: list[tuple[str, str, str]]) -> bytes:
    body = "".join(
        f"<STMTTRN>\n<TRNTYPE>DEBIT\n<DTPOSTED>{posted}\n<TRNAMT>{amount}\n"
        f"<FITID>F{i}\n<NAME>{name}\n</STMTTRN>\n"
        for i, (posted, amount, name) in enumerate(rows)
    )
    return (HEADER + body + FOOTER).encode()


@given(transactions())
def test_round_trip_preserves_every_transaction(rows: list[tuple[str, str, str]]) -> None:
    """N transactions in, N rows out — amounts to the cent, dates as dates,
    NAME verbatim (the description source when MEMO is absent)."""
    parsed = OfxImporter.parse(_doc(rows), ACCOUNT)
    assert len(parsed) == len(rows)
    for index, (row, (posted, amount, name)) in enumerate(zip(parsed, rows, strict=True)):
        assert row.date == dt_date(int(posted[:4]), int(posted[4:6]), int(posted[6:8]))
        assert row.amount_native == Decimal(amount)
        assert row.raw_description == name.strip()
        assert row.currency is None  # account currency applies
        assert row.external_ref == f"F{index}", "FITID rides along as the debug ref"


@given(transactions())
def test_generated_documents_are_detected(rows: list[tuple[str, str, str]]) -> None:
    """detect() keys on the OFXHEADER block — every well-formed doc must route
    to this importer, or the pipeline would 422 with the wrong parser error."""
    assert OfxImporter.detect(_doc(rows))
    assert OfxImporter.detect(b"ofxheader:100\n" + _doc(rows)[14:])  # case-insensitive


@given(st.binary(max_size=4096))
def test_hostile_bytes_never_escape_the_domain_error(raw: bytes) -> None:
    """Audit L5, property form: parse either SUCCEEDS or raises OfxParseError —
    never a bare ValueError/LookupError/AttributeError reaching the router,
    which is what would turn a bad upload into a 500."""
    try:
        OfxImporter.parse(raw, ACCOUNT)
    except OfxParseError:
        pass  # the one documented failure shape


@given(
    transactions(),
    st.integers(min_value=0, max_value=4),
    st.sampled_from([b"", b"X", b"\n", b"<", b">"]),
)
def test_a_mutation_still_never_escapes(rows, target: int, junk: bytes) -> None:
    """Mutating one byte of a VALID document (truncate, splice, flip a tag)
    exercises the parser paths random bytes rarely reach — same envelope."""
    doc = bytearray(_doc(rows))
    assume(len(doc) > target)
    position = (len(doc) * 31 + target * 7) % len(doc)  # stable: hash() salts per process
    doc[position : position + len(junk)] = junk
    try:
        OfxImporter.parse(bytes(doc), ACCOUNT)
    except OfxParseError:
        pass
