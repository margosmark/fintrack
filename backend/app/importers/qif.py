"""QIF (Quicken Interchange Format) importer — the second format Canadian
banks actually export (docs/03).

QIF is line-oriented text: an optional section header (``!Type:Bank``),
then one record per transaction made of single-character field lines,
terminated by ``^`` (Quicken) or a blank line::

    !Type:Bank
    D03/15/2026
    T-45.67
    PMETRO STORES #123
    Mweekly groceries
    ^

Field codes consumed: ``D`` date, ``T`` signed amount, ``P`` payee,
``M`` memo, ``L`` category (payee fallback), ``N`` cheque/number (ignored),
``A`` address lines, ``C`` cleared flag, ``S``/``E`` split parts (the record
still carries its signed total in ``T``).

Strict-by-design (AGENTS §3 arbitration principle): a record with no date,
no amount, or an unparseable amount/date raises :class:`QifParseError` and
the whole file is rejected ``422 qif_parse_error`` — it is never silently
skipped. Section headers that hold no bank-style transactions (``!Account``,
``!Option``, ``!Type:Invst``) are skipped or rejected explicitly.
"""

import re
from collections.abc import Iterable
from datetime import date as dt_date
from decimal import Decimal, InvalidOperation

from app.importers.base import AccountView, RawTransaction, decode_raw

# Section headers we understand. ``!Type:Invst`` (investments) is deliberately
# absent — trade history imports through the investment CSV path (docs/05).
_TRANSACTIONAL_TYPES = frozenset({"BANK", "CCARD", "CASH", "OTH A", "OTH L", "OTH R"})
_HEADER = re.compile(r"^!(Type|Option|Account|Mem)\b", re.IGNORECASE)
_SUPPORTED_SECTION = re.compile(r"^!Type:([A-Za-z ]+)", re.IGNORECASE)
_YEAR_FIRST = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$")
_NAMED_DATE = re.compile(r"^(\d{1,2})[-\s]([A-Za-z]{3,})[-\s](\d{2,4})$")
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
# Two-digit years: 00-69 → 2000s, 70-99 → 1900s (POSIX pivot).
_PIVOT = 70


class QifParseError(ValueError):
    """Malformed QIF input; mapped to the 422 qif_parse_error envelope (docs/03)."""


def _expand_year(value: str) -> int:
    year = int(value)
    if len(value) <= 2:
        year += 2000 if year < _PIVOT else 1900
    return year


def _make_date(year: int, month: int, day: int, raw: str) -> dt_date:
    try:
        return dt_date(year, month, day)
    except ValueError:
        raise QifParseError(f"invalid QIF date {raw!r}") from None


def parse_qif_date(text: str) -> dt_date:
    """Parse the date formats Canadian banks emit in QIF exports.

    QIF carries no locale marker, so an ambiguous ``DD/MM`` vs ``MM/DD`` pair
    (e.g. ``05/06/2026``) resolves to **month-first** — Quicken's own default
    and what RBC/TD/BMO exports mean. When one component is > 12 the order is
    unambiguous and the data decides. This pitfall is documented per
    institution in the ``/help/import-guides`` hub.
    """
    raw = (text or "").strip()
    if not raw:
        raise QifParseError("QIF record is missing its date (D field)")

    match = _YEAR_FIRST.match(raw)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return _make_date(year, month, day, raw)

    match = _NUMERIC_DATE.match(raw)
    if match:
        first, second, year_raw = match.groups()
        year = _expand_year(year_raw)
        a, b = int(first), int(second)
        if a > 12:
            day, month = a, b
        elif b > 12:
            month, day = a, b
        else:  # ambiguous → month-first (docs/03 / import-guides pitfall)
            month, day = a, b
        return _make_date(year, month, day, raw)

    match = _NAMED_DATE.match(raw)
    if match:
        day_raw, month_raw, year_raw = match.groups()
        month = _MONTHS.get(month_raw[:3].lower())
        if month is None:
            raise QifParseError(f"unrecognized QIF month in {raw!r}")
        return _make_date(_expand_year(year_raw), month, int(day_raw), raw)

    raise QifParseError(f"unrecognized QIF date {raw!r}")


def parse_qif_amount(text: str) -> Decimal:
    cleaned = (text or "").strip().replace(",", "").replace("$", "").replace("\xa0", "")
    if not cleaned:
        raise QifParseError("QIF record is missing its amount (T field)")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise QifParseError(f"unparseable QIF amount {text!r}") from None


def _split_sections(lines: Iterable[str]) -> list[tuple[str | None, list[str]]]:
    """Group the file into ``(header, record_lines)`` sections.

    ``^`` and blank lines both terminate a record; a ``!`` line opens a new
    section and flushes whatever it interrupted.
    """
    sections: list[tuple[str | None, list[str]]] = []
    header: str | None = None
    record: list[str] = []

    def flush() -> None:
        if record:
            sections.append((header, list(record)))
            record.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line.startswith("!"):
            flush()
            header = line
            continue
        if line == "^":
            flush()
            continue
        record.append(line)
    flush()
    return sections


def _record_fields(record: list[str], ordinal: int) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in record:
        if len(line) < 1:
            continue
        fields.setdefault(line[0].upper(), []).append(line[1:].strip())
    if not fields:
        raise QifParseError(f"QIF record {ordinal} is empty")
    return fields


def _first(fields: dict[str, list[str]], *codes: str) -> str:
    for code in codes:
        for value in fields.get(code, []):
            if value:
                return value
    return ""


class QifImporter:
    @staticmethod
    def detect(raw: bytes) -> bool:
        head = decode_raw(raw[:8192])
        for line in head.splitlines():
            if _HEADER.match(line.strip()):
                return True
        return False

    @staticmethod
    def parse(raw: bytes, account: AccountView) -> list[RawTransaction]:
        text = decode_raw(raw)
        if not QifImporter.detect(raw):
            raise QifParseError("not a QIF file: no !Type:/!Option:/!Account section header")

        sections = _split_sections(text.splitlines())
        rows: list[RawTransaction] = []
        seen_unsupported = False

        for header, record in sections:
            if header is None:
                # Headerless run (some banks omit !Type:) — treat as bank data.
                matched_type = "BANK"
            else:
                match = _SUPPORTED_SECTION.match(header)
                if match is None:
                    # !Option / !Account / !Mem bookkeeping sections hold no
                    # transactions; skip them, they are not an error.
                    continue
                matched_type = match.group(1).strip().upper()

            if matched_type not in _TRANSACTIONAL_TYPES:
                seen_unsupported = True
                continue

            rows.extend(_parse_record(record, len(rows) + 1))

        if not rows:
            if seen_unsupported:
                raise QifParseError(
                    "QIF contains no bank/cash transactions — investment (!Type:Invst) "
                    "and other non-bank sections are not supported; export trades as CSV instead"
                )
            raise QifParseError("QIF contains no transaction records")
        return rows


def _parse_record(record: list[str], ordinal: int) -> list[RawTransaction]:
    fields = _record_fields(record, ordinal)
    date_text = _first(fields, "D")
    if not date_text:
        raise QifParseError(f"QIF record {ordinal} is missing its date (D field)")
    amount_text = _first(fields, "T")
    if not amount_text:
        raise QifParseError(
            f"QIF record {ordinal} is missing its amount (T field)"
            + (" — split records must carry their signed total" if "S" in fields else "")
        )
    description = _first(fields, "P", "M", "L")
    return [
        RawTransaction(
            date=parse_qif_date(date_text),
            amount_native=parse_qif_amount(amount_text),
            raw_description=description,
            currency=None,  # QIF carries no currency; the account's applies
            posting_date=None,
            external_ref=_first(fields, "N") or None,  # cheque/transaction number
        )
    ]
