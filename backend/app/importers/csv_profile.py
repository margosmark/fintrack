"""Generic-CSV importer driven by per-institution mapping profiles (docs/03).

Profile JSON v2 (w10-contract): adds OPTIONAL ``columns.posting_date`` and
``columns.currency`` mappings (both modes) and an OPTIONAL ``signs`` object
(split mode only) overriding the structural debit=-1/credit=1 multipliers.
Every addition is optional; v1 profiles parse identically.
"""

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.importers.base import AccountView, RawTransaction, decode_raw


def _fix_stray_quotes(raw_text: str) -> str:
    """Fix unescaped double-quotes inside CSV quoted fields.

    Some bank exports (e.g. TOYS "R" US) include stray quotes inside
    quoted fields without doubling them, which breaks csv.reader.
    Replaces stray mid-field quotes with single-quotes.

    Single pass, O(n) (audit H2): each line is walked once, and the look-ahead
    that decides "does this quote close the field?" is a forward scan that
    visits every character of the line at most once in total (a whitespace run
    is scanned only by the quote that immediately precedes it). The previous
    implementation re-joined the remainder of the line for every quote —
    O(quotes x line), measured at ~60 s for one 160k-quote line — letting a
    single 5 MB upload block the API for minutes. Outputs are byte-for-byte
    identical to that implementation, pinned by the characterization cases in
    tests/test_import_dos.py.
    """
    fixed = []
    for line in raw_text.splitlines(keepends=True):
        chars = list(line)
        n = len(chars)
        in_quote = False
        i = 0
        while i < n:
            if chars[i] == '"':
                if in_quote:
                    # First non-whitespace character after this quote decides:
                    # end-of-line or "," closes the field, anything else is a
                    # stray quote -> apostrophe. (str.lstrip never leaves a
                    # "\n"/"\r" as rest[0], so those cases collapse into the
                    # empty-rest branch below.)
                    j = i + 1
                    while j < n and chars[j].isspace():
                        j += 1
                    if j >= n or chars[j] == ",":
                        in_quote = False
                    else:
                        chars[i] = "'"
                else:
                    in_quote = True
            i += 1
        fixed.append("".join(chars))
    return "".join(fixed)


# Pre-parse hard caps (docs/03 "Upload validation order"): cheap O(n) scans
# that run BEFORE csv sees the data, bounding sanitizer/parse CPU on hostile
# uploads (audit H2/L6). Thresholds are deliberately far above any real bank
# export (real lines are ~1 KB with < 20 quotes).
MAX_CSV_LINE_CHARS = 1_000_000  # one CSV line over 1 MB is never a bank export
MAX_CSV_LINE_QUOTES = 1_000  # > 1000 quotes on one line is crafted, not real


REQUIRED_COLUMNS = ("date", "raw_description")
SIGNED_AMOUNT_COLUMN = "amount_native"
SPLIT_COLUMNS = ("debit", "credit")
# Optional mappings in BOTH modes; a blank value is normalised to "absent".
OPTIONAL_COLUMNS = ("posting_date", "currency")
CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")
DEFAULT_SIGNS: dict[str, int] = {"debit": -1, "credit": 1}

# Human-readable date format templates → strptime directives.
_HUMAN_TO_STRPTIME: dict[str, str] = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY/MM/DD": "%Y/%m/%d",
    "YYYY.MM.DD": "%Y.%m.%d",
    "YYYYMMDD": "%Y%m%d",
    "DD/MM/YYYY": "%d/%m/%Y",
    "DD.MM.YYYY": "%d.%m.%Y",
    "DD-MM-YYYY": "%d-%m-%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "MM-DD-YYYY": "%m-%d-%Y",
    "DD-MMM-YYYY": "%d-%b-%Y",
    "DD MMM YYYY": "%d %b %Y",
    "MMM DD, YYYY": "%b %d, %Y",
    "DD MMM, YYYY": "%b %d, %Y",
    "MMMM D, YYYY": "%B %d, %Y",
    "D MMMM YYYY": "%d %B %Y",
}


def _normalize_date_format(fmt: str) -> str:
    """Normalise a date format string to a valid strptime directive.

    If *fmt* is already a valid strptime directive (contains a ``%``-token)
    or is a known human template, return the strptime equivalent.  Otherwise
    return *fmt* unchanged so the caller's existing error path surfaces it.
    """
    if "%" in fmt:
        return fmt
    return _HUMAN_TO_STRPTIME.get(fmt, fmt)


# Sign conventions (docs/03 "amount sign convention"):
#   Signed amount_native column:
#     credits_positive=True  -> file stores credits positive / debits negative,
#                               i.e. already canonical; signs pass through.
#     credits_positive=False -> file stores debits positive; signs are inverted.
#   Split debit/credit columns: default signs are structural (debit -> negative,
#   credit -> positive); a profile ``signs`` object (w10) overrides either
#   multiplier with -1 or 1. credits_positive is ignored in split mode.


class CsvProfileError(ValueError):
    """Base class for profile/CSV parse failures surfaced as a 422 envelope."""


class MissingProfileError(CsvProfileError):
    """Institution has no importer_profile but the file needs one."""


class CsvLineTooLongError(CsvProfileError):
    """A CSV line exceeds MAX_CSV_LINE_CHARS -> 422 csv_line_too_long."""


class CsvTooComplexError(CsvProfileError):
    """A CSV line carries more than MAX_CSV_LINE_QUOTES -> 422 csv_too_complex."""


def check_csv_complexity(raw: bytes) -> None:
    """Reject over-long / quote-dense CSV lines before parsing (docs/03).

    Scans the decoded text with the same ``str.splitlines`` notion of "line"
    the sanitizer uses; one O(n) pass at C speed. Raises ``CsvLineTooLongError``
    / ``CsvTooComplexError``, which ``api/imports.py`` maps to the 422 codes
    ``csv_line_too_long`` / ``csv_too_complex``.
    """
    for lineno, line in enumerate(decode_raw(raw).splitlines(), start=1):
        if len(line) > MAX_CSV_LINE_CHARS:
            raise CsvLineTooLongError(
                f"CSV line {lineno} exceeds the {MAX_CSV_LINE_CHARS} character limit"
            )
        if line.count('"') > MAX_CSV_LINE_QUOTES:
            raise CsvTooComplexError(
                f"CSV line {lineno} has more than {MAX_CSV_LINE_QUOTES} quotes"
            )


class CsvProfile(BaseModel):
    """Validated ``institutions.importer_profile`` (docs/03); unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")

    columns: dict[str, str]
    date_format: str = "%Y-%m-%d"
    decimal_comma: bool = False
    # Ignored when columns use the split debit/credit mode (signs are structural).
    credits_positive: bool = False
    headerless: bool = False

    # Split mode only; ignored (silently) in signed mode. Values must be exactly
    # -1 or 1; unknown keys are rejected. None -> DEFAULT_SIGNS.
    signs: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_signs(self) -> "CsvProfile":
        """w10-contract: sign values in {-1, 1}, keys limited to split columns."""
        if self.signs is None:
            return self
        for key, value in self.signs.items():
            if key not in SPLIT_COLUMNS:
                raise ValueError(
                    f"signs has unknown key {key!r}; expected {', '.join(SPLIT_COLUMNS)}"
                )
            if value not in (-1, 1) or isinstance(value, bool):
                raise ValueError(f"signs.{key} must be exactly -1 or 1, got {value!r}")
        return self

    @model_validator(mode="after")
    def _normalize_date_format(self) -> "CsvProfile":
        """Normalise human-readable date templates (e.g. 'YYYY-MM-DD') to strptime directives."""
        self.date_format = _normalize_date_format(self.date_format)
        return self

    @model_validator(mode="after")
    def _resolve_mode_conflict(self) -> "CsvProfile":
        """Auto-resolve amount_native vs debit+credit conflict.

        When both are present (LLM or stored profile), prefer split mode
        (debit+credit) since it is more specific; remove amount_native.
        """
        has_signed = SIGNED_AMOUNT_COLUMN in self.columns
        has_split = all(k in self.columns for k in SPLIT_COLUMNS)
        if has_signed and has_split:
            del self.columns[SIGNED_AMOUNT_COLUMN]
        return self

    @model_validator(mode="after")
    def _validate_mode(self) -> "CsvProfile":
        """Mode rule (w9-contract): amount_native XOR debit+credit (docs/03 split mode)."""
        missing = [key for key in REQUIRED_COLUMNS if key not in self.columns]
        if missing:
            raise ValueError(f"columns must map {', '.join(missing)}")
        has_signed = SIGNED_AMOUNT_COLUMN in self.columns
        missing_split = [key for key in SPLIT_COLUMNS if key not in self.columns]
        if has_signed and len(missing_split) < len(SPLIT_COLUMNS):
            raise ValueError(
                "columns must use either amount_native (signed amount) "
                "or debit+credit (split columns), not both"
            )
        if not has_signed and missing_split:
            raise ValueError(
                f"columns must map {', '.join(REQUIRED_COLUMNS)} and either "
                f"{SIGNED_AMOUNT_COLUMN} or both of {', '.join(SPLIT_COLUMNS)}; "
                f"missing {', '.join(missing_split)}"
            )
        return self

    @model_validator(mode="after")
    def _drop_empty_optionals(self) -> "CsvProfile":
        """Blank OPTIONAL mapping == unmapped (the UI's "None"), not an error (M111).

        The wizard's optional selects render "None" as ``""``; storing that
        literal used to trip ``_validate_headerless`` and fail every upload
        with a cryptic 422 long after the profile was saved. Dropping the key
        is exactly what docs/03 prescribes ("omit the key entirely when the
        file has no such column"), so normalise instead of rejecting. REQUIRED
        and mode columns keep failing below — a blank there is a real gap.
        Runs before the name checks in ``_validate_headerless``.
        """
        for key in OPTIONAL_COLUMNS:
            value = self.columns.get(key)
            if isinstance(value, str) and not value.strip():
                del self.columns[key]
        return self

    @model_validator(mode="after")
    def _validate_headerless(self) -> "CsvProfile":
        """When headerless, columns map 1-based positions; else header names (docs/03)."""
        if self.headerless:
            for key, value in self.columns.items():
                if not re.fullmatch(r"\d+", str(value)) or int(value) < 1:
                    raise ValueError(
                        f"columns.{key} must be a 1-based column index when "
                        f"headerless=true, got {value!r}"
                    )
        else:
            for key, value in self.columns.items():
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"columns.{key} must be a non-empty header name")
        return self


# --- Friendly rendering of a profile validation error (M111) -----------------
# `str(ValidationError)` is a multi-line dump of internals ("1 validation error
# for CsvProfile … [input_value={'columns': {'date': 'Tra…"]) — exactly what got
# truncated into Mark's bug report. The user needs the mapping to fix, so the
# one message the mapping wizard and the profile editor can actually produce is
# rewritten; every other message already reads as prose and passes through.

# Profile column key → the control that sets it, for the "set the X column" half.
_COLUMN_LABELS: dict[str, str] = {
    "date": "Date",
    "raw_description": "Description",
    SIGNED_AMOUNT_COLUMN: "Amount",
    "debit": "Debit",
    "credit": "Credit",
    "posting_date": "Posting date",
    "currency": "Currency",
}
# Split-mode blanks are also fixable by changing mode, which is what the user
# usually meant to do when a stale debit/credit key was left behind.
_SPLIT_COLUMN_HINTS: dict[str, str] = {
    "debit": "set the Debit column or switch to single-amount mode",
    "credit": "set the Credit column or switch to single-amount mode",
}
_SIGNED_COLUMN_HINT = (
    f"set the {_COLUMN_LABELS[SIGNED_AMOUNT_COLUMN]} column or switch to Debit & Credit mode"
)
_EMPTY_COLUMN_ERROR = re.compile(r"columns\.(?P<key>[A-Za-z_]+) must be a non-empty header name")


def profile_error_message(exc: ValidationError) -> str:
    """First validation error of *exc* as ONE human, actionable line.

    Returned text feeds the house `{detail, code}` envelope (``invalid_profile``
    in ``api/imports.py``) and the parser's ``invalid importer_profile: …``.
    """
    msg = exc.errors()[0]["msg"].removeprefix("Value error, ")
    match = _EMPTY_COLUMN_ERROR.fullmatch(msg)
    if match is None:
        return msg
    key = match.group("key")
    hint = _SPLIT_COLUMN_HINTS.get(key)
    if hint is None:
        hint = (
            _SIGNED_COLUMN_HINT
            if key == SIGNED_AMOUNT_COLUMN
            else f"set the {_COLUMN_LABELS.get(key, key)} column"
        )
    return f"{key} column mapping is empty — {hint}"


def _parse_magnitude(text: str, profile: CsvProfile) -> Decimal:
    """Amount magnitude after decimal_comma handling; sign decided by the caller.

    Accounting parentheses are a negative number, not a parse failure: some
    bank exports and hand-edited CSVs write ``(58.12)`` for an outflow
    (docs/58 §A). The ``credits_positive`` convention is applied by the caller
    exactly as it is for a leading ``-``.
    """
    cleaned = text.strip().replace(" ", "").replace("\xa0", "").replace("$", "")
    parenthesized = cleaned.startswith("(") and cleaned.endswith(")") and len(cleaned) > 1
    if parenthesized:
        cleaned = cleaned[1:-1]
    if profile.decimal_comma:
        # Decimal comma: '.' is a thousands separator, ',' the decimal mark.
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise CsvProfileError(f"unparseable amount: {text!r}") from None
    if parenthesized:
        return -abs(amount)  # parens mean negative whatever the inner sign
    return amount


def _parse_amount(text: str, profile: CsvProfile) -> Decimal:
    # Signed mode (docs/03): credits_positive=True -> already canonical, else inverted.
    amount = _parse_magnitude(text, profile)
    return -amount if not profile.credits_positive else amount


def parse_rows(raw: bytes, profile: CsvProfile) -> list[RawTransaction]:
    if profile.headerless:
        return _parse_rows_positional(raw, profile)
    reader = csv.DictReader(io.StringIO(_fix_stray_quotes(decode_raw(raw)), newline=""))
    if reader.fieldnames is None:
        raise CsvProfileError("empty CSV: no header row")

    def cell(row: dict[str, str | None], field: str) -> str:
        header = profile.columns[field]
        value = row.get(header)
        if value is None:
            raise CsvProfileError(f"missing column {header!r} for {field}")
        return value.strip()

    def opt_cell(row: dict[str, str | None], field: str) -> str | None:
        """Optional mapping: unmapped header -> None; mapped but blank cell -> ""."""
        if field not in profile.columns:
            return None
        return (row.get(profile.columns[field]) or "").strip()

    def posting_date_for(row: dict[str, str | None], line_number: int):
        text = opt_cell(row, "posting_date")
        if not text:  # unmapped or blank -> NULL posting_date (w10-contract)
            return None
        try:
            return datetime.strptime(text, profile.date_format).date()
        except ValueError:
            raise CsvProfileError(
                f"row {line_number}: posting_date {text!r} does not match {profile.date_format!r}"
            ) from None

    def currency_for(row: dict[str, str | None]) -> str | None:
        token = (opt_cell(row, "currency") or "").upper()
        # Blank/invalid token falls back to account.currency (None here); row kept.
        return token if CURRENCY_PATTERN.fullmatch(token) else None

    signs = (
        DEFAULT_SIGNS
        if profile.signs is None
        else {**DEFAULT_SIGNS, **{key: int(value) for key, value in profile.signs.items()}}
    )

    rows: list[RawTransaction] = []
    for line_number, row in enumerate(reader, start=2):
        date_text = cell(row, "date")
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line
        if not date_text:
            raise CsvProfileError(f"row {line_number}: empty date")
        try:
            when = datetime.strptime(date_text, profile.date_format).date()
        except ValueError:
            raise CsvProfileError(
                f"row {line_number}: date {date_text!r} does not match {profile.date_format!r}"
            ) from None
        if SIGNED_AMOUNT_COLUMN in profile.columns:
            amount = _parse_amount(cell(row, "amount_native"), profile)
        else:
            # Split mode: debit/credit magnitudes multiplied by configured signs
            # (defaults preserve the structural debit=-1 / credit=1 behavior).
            debit_text = cell(row, "debit")
            credit_text = cell(row, "credit")
            if not debit_text and not credit_text:
                continue  # both-empty row: skipped entirely (w9-contract)
            if debit_text:
                amount = _parse_magnitude(debit_text, profile) * signs["debit"]
            else:
                amount = _parse_magnitude(credit_text, profile) * signs["credit"]
        rows.append(
            RawTransaction(
                date=when,
                amount_native=amount,
                raw_description=cell(row, "raw_description"),
                currency=currency_for(row),
                posting_date=posting_date_for(row, line_number),
            )
        )
    return rows


def _parse_rows_positional(raw: bytes, profile: CsvProfile) -> list[RawTransaction]:
    """Headerless path: columns map by 1-based position; no header row consumed."""
    reader = csv.reader(io.StringIO(_fix_stray_quotes(decode_raw(raw)), newline=""))

    signs = (
        DEFAULT_SIGNS
        if profile.signs is None
        else {**DEFAULT_SIGNS, **{key: int(value) for key, value in profile.signs.items()}}
    )

    def cell(row: list[str], field: str, line_number: int) -> str:
        idx = int(profile.columns[field]) - 1
        if idx < 0 or idx >= len(row):
            raise CsvProfileError(
                f"row {line_number}: missing column at position {idx + 1} for {field}"
            )
        return (row[idx] or "").strip()

    def opt_cell(row: list[str], field: str) -> str | None:
        """Optional mapping: unmapped field -> None; out-of-range -> ""."""
        if field not in profile.columns:
            return None
        idx = int(profile.columns[field]) - 1
        if idx < 0 or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    def posting_date_for(row: list[str], line_number: int) -> date | None:
        text = opt_cell(row, "posting_date")
        if not text:
            return None
        try:
            return datetime.strptime(text, profile.date_format).date()
        except ValueError:
            raise CsvProfileError(
                f"row {line_number}: posting_date {text!r} does not match {profile.date_format!r}"
            ) from None

    def currency_for(row: list[str]) -> str | None:
        token = (opt_cell(row, "currency") or "").upper()
        return token if CURRENCY_PATTERN.fullmatch(token) else None

    rows: list[RawTransaction] = []
    for line_number, row in enumerate(reader, start=1):
        if not any((v or "").strip() for v in row):
            continue  # blank line
        date_text = cell(row, "date", line_number)
        if not date_text:
            raise CsvProfileError(f"row {line_number}: empty date")
        try:
            when = datetime.strptime(date_text, profile.date_format).date()
        except ValueError:
            raise CsvProfileError(
                f"row {line_number}: date {date_text!r} does not match {profile.date_format!r}"
            ) from None
        if SIGNED_AMOUNT_COLUMN in profile.columns:
            amount = _parse_amount(cell(row, "amount_native", line_number), profile)
        else:
            debit_text = cell(row, "debit", line_number)
            credit_text = cell(row, "credit", line_number)
            if not debit_text and not credit_text:
                continue  # both-empty row: skipped entirely (w9-contract)
            if debit_text:
                amount = _parse_magnitude(debit_text, profile) * signs["debit"]
            else:
                amount = _parse_magnitude(credit_text, profile) * signs["credit"]
        rows.append(
            RawTransaction(
                date=when,
                amount_native=amount,
                raw_description=cell(row, "raw_description", line_number),
                currency=currency_for(row),
                posting_date=posting_date_for(row, line_number),
            )
        )
    return rows


class CsvImporter:
    @staticmethod
    def detect(raw: bytes) -> bool:
        head = raw[:4096].upper()
        if b"OFXHEADER" in head or b"<OFX>" in head:
            return False
        first_line = decode_raw(raw[:4096]).splitlines()
        return bool(first_line) and "," in first_line[0]

    @staticmethod
    def parse(raw: bytes, account: AccountView) -> list[RawTransaction]:
        profile_payload = account.importer_profile
        if profile_payload is None:
            raise MissingProfileError("institution has no importer_profile for CSV import")
        if isinstance(profile_payload, CsvProfile):
            profile = profile_payload
        elif isinstance(profile_payload, dict):
            try:
                profile = CsvProfile.model_validate(profile_payload)
            except ValidationError as exc:
                raise CsvProfileError(
                    f"invalid importer_profile: {profile_error_message(exc)}"
                ) from None
        else:
            try:
                profile = CsvProfile.model_validate_json(str(profile_payload))
            except ValidationError as exc:
                raise CsvProfileError(
                    f"invalid importer_profile: {profile_error_message(exc)}"
                ) from None
        return parse_rows(raw, profile)
