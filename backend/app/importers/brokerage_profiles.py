"""Brokerage CSV import profiles — one per institution (docs/41, M82).

A profile is pure configuration: it turns a broker's activity CSV into
canonical activity dicts in the Wealthsimple vocabulary and hands them to
``investment_profile.build_trades``, the ONE place trade-mutation semantics
live. Profiles never touch the database, never resolve a security and never
write: staging stays in ``app/api/trades.py`` (docs/03's documented trades
seam), so there is no second write path.

Two readers:

- ``table``  — one header row, one row per transaction (every profile but IBKR's
  Activity Statement).
- ``ibkr``   — IBKR's sectioned statement: ``<Section>,Header,...`` followed by
  ``<Section>,Data,...`` blocks.

Sourcing: every column map below was reconstructed from a PUBLISHED layout,
not captured from a real customer export — see docs/41 § "Fixtures are
reconstructed". Replacing a fixture with a redacted real export is a fixture
change only; the profiles are config.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.importers.base import RawTrade, decode_raw
from app.importers.investment_profile import (
    ACTIVITY_MAP,
    InvestmentCsvProfileError,
    UnmappedRow,
    build_trades,
    normalize_action_token,
)
from app.models.trades import TradeAction

__all__ = [
    "ACTIVITY_MAP",
    "BROKERAGE_PROFILES",
    "CORPORATE_ACTION_FORMS",
    "PROFILE_SLUGS",
    "BrokerageFile",
    "BrokerageProfile",
    "ProfileError",
    "ProfileHeaderMismatchError",
    "SectionSpec",
    "UnknownCorporateActionError",
    "UnknownProfileError",
    "detect_profile",
    "get_profile",
    "parse_brokerage_csv",
]


# --------------------------------------------------------------------------- #
# Errors — every one carries the 422 `code` api/trades.py should envelope.
# --------------------------------------------------------------------------- #


class ProfileError(InvestmentCsvProfileError):
    """Base for profile-level refusals (422, never a bare 500)."""

    code = "parse_error"


class UnknownProfileError(ProfileError):
    """`profile` names a slug the registry does not hold."""

    code = "unknown_import_profile"


class ProfileHeaderMismatchError(ProfileError):
    """An explicitly requested profile does not recognise the file's header."""

    code = "profile_header_mismatch"


class UnknownCorporateActionError(ProfileError):
    """A corporate-action row reached the parser with no pinned handler (docs/41 §6)."""

    code = "unknown_corporate_action"


#: Pinned extension point (docs/41): `(profile_slug, action_token) -> handler`.
#: Empty in M82 on purpose — no published IBKR/TD export carries either docs/06
#: value, so every corporate action answers 422 until an export that does
#: arrives and earns exactly one entry here.
CORPORATE_ACTION_FORMS: dict[tuple[str, str], object] = {}


# --------------------------------------------------------------------------- #
# Canonical vocabulary
# --------------------------------------------------------------------------- #

# Fields every reader must fill before handing a row to `build_trades`.
CANONICAL_FIELDS = (
    "date",
    "action",
    "sub",
    "symbol",
    "name",
    "currency",
    "description",
    "quantity",
    "unit_price",
    "commission",
    "amount",
    "account",
)
NUMERIC_FIELDS = frozenset({"quantity", "unit_price", "commission", "amount"})

_ISO3 = re.compile(r"^[A-Z]{3}$")


def _norm(value: str) -> str:
    """Header-name normaliser: fold accents, trim, casefold.

    NBDB's French export heads its columns ``Quantité``/``Libellé``; folding at
    the header boundary lets ONE column map serve both languages.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return folded.strip().casefold()


_token_norm = normalize_action_token


# --------------------------------------------------------------------------- #
# Profile shape
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SectionSpec:
    """How one IBKR statement section reduces to canonical fields."""

    columns: Mapping[str, str | tuple[str, ...]]
    #: A fixed action for every row of the section (`Dividends` -> dividend).
    action: TradeAction | None = None
    #: Read the action word from this column instead (`Deposits & Withdrawals`).
    action_column: str | None = None
    #: Derive BUY/SELL from the quantity's sign (`Trades` has no Buy/Sell column).
    sign_from_quantity: bool = False
    #: Regex applied to `description` when `symbol` is empty (IBKR dividends
    #: write ``AAPL (US1234567890) DIVIDEND`` — the symbol is only in there).
    symbol_pattern: str | None = None
    #: A column whose value marks the row a corporate action (IBKR `Code`).
    code_column: str | None = None


@dataclass(frozen=True)
class BrokerageProfile:
    slug: str
    label: str
    reader: Literal["table", "ibkr"]
    #: canonical field -> source header. A tuple is either an ordered candidate
    #: list (string fields: first header present wins) or, for numeric fields,
    #: a SUM of every present column (Fidelity reports commission twice).
    columns: Mapping[str, str | tuple[str, ...]]
    #: canonical fields that MUST resolve for the file to be this institution.
    required_headers: tuple[str, ...]
    actions: Mapping[tuple[str, str | None], TradeAction]
    #: Empty -> the legacy best-effort date parser (Wealthsimple stays byte-for
    #: byte compatible with the old path); non-empty -> a strict tuple, and any
    #: other date is an unmapped row instead of a guessed day.
    date_formats: tuple[str, ...] = ()
    #: Only used in error text — the human name of the action column.
    action_column: str = "Action"
    currency_rule: Literal["row_column", "file_constant"] = "row_column"
    currency_column: str | None = None
    currency_constant: str | None = None
    #: Extra headers whose values are ADDED to `commission` after parsing
    #: (Fidelity reports commission in a separate Fees column too).
    commission_sum: tuple[str, ...] = ()
    #: A blank/invalid currency cell is an unmapped row (True) or falls back to
    #: the target account's currency (False).
    require_currency: bool = True
    #: Zero-quantity share rows are unmapped instead of staged.
    require_quantity: bool = True
    #: Words that mean "corporate action" — any of these is 422 (docs/41 §6).
    corporate_action_tokens: frozenset[str] = frozenset()
    #: IBKR sectioned statement only.
    sections: Mapping[str, SectionSpec] = field(default_factory=dict)
    ignored_sections: frozenset[str] = frozenset()
    strict_sections: frozenset[str] = frozenset()
    section_messages: Mapping[str, str] = field(default_factory=dict)
    #: Per-file caveats surfaced as `notices`, never as rows.
    notices: tuple[str, ...] = ()

    @property
    def action_tokens(self) -> frozenset[str]:
        return frozenset(_token_norm(tok) for tok in self.corporate_action_tokens)


@dataclass(frozen=True)
class BrokerageFile:
    profile: BrokerageProfile
    rows: list[RawTrade]
    unmapped: list[UnmappedRow]
    notices: list[str]


# --------------------------------------------------------------------------- #
# Shared action vocabulary
# --------------------------------------------------------------------------- #

_TRADE_WORDS: dict[tuple[str, str | None], TradeAction] = {
    ("Buy", None): TradeAction.buy,
    ("Sell", None): TradeAction.sell,
    ("Dividend", None): TradeAction.dividend,
    ("Distribution", None): TradeAction.distribution,
    ("Return of Capital", None): TradeAction.return_of_capital,
    ("Interest", None): TradeAction.dividend,  # docs/05/06: interest rides as income
    ("Deposit", None): TradeAction.contribution,
    ("Contribution", None): TradeAction.contribution,
    ("Withdrawal", None): TradeAction.withdrawal,
    ("Transfer In", None): TradeAction.transfer_in,
    ("Transfer Out", None): TradeAction.transfer_out,
    ("Fee", None): TradeAction.fee,
    ("Commission", None): TradeAction.fee,
}

# NBDB publishes the same file in French or English (ActiveACB/StockWatcher).
_FRENCH_WORDS: dict[tuple[str, str | None], TradeAction] = {
    ("Achat", None): TradeAction.buy,
    ("Vente", None): TradeAction.sell,
    ("Dividende", None): TradeAction.dividend,
    ("Distribution", None): TradeAction.distribution,
    ("Retrait", None): TradeAction.withdrawal,
    ("Depot", None): TradeAction.contribution,
    ("Depot de tresorerie", None): TradeAction.contribution,
    ("Interet", None): TradeAction.dividend,
    ("Frais", None): TradeAction.fee,
    ("Transfert entrant", None): TradeAction.transfer_in,
    ("Transfert sortant", None): TradeAction.transfer_out,
}

_WS_EXTRAS: dict[tuple[str, str | None], TradeAction] = {
    # Wealthsimple express orders carry their own label (M82 evidence note);
    # anything we do not name lands in `unmapped`, never silently in a trade.
    ("Buy Express", None): TradeAction.buy,
    ("Sell Express", None): TradeAction.sell,
    ("Buy", "EXPRESS"): TradeAction.buy,
    ("Sell", "EXPRESS"): TradeAction.sell,
}


def _map(*sources: Mapping[tuple[str, str | None], TradeAction]) -> dict:
    merged: dict[tuple[str, str | None], TradeAction] = {}
    for source in sources:
        merged.update(source)
    return merged


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

IBKR_FLAT_COLUMNS: dict[str, str | tuple[str, ...]] = {
    "date": "DateTime",
    "action": "Buy/Sell",
    "symbol": "Symbol",
    "name": "Symbol",
    "description": "Code",
    "currency": "CurrencyPrimary",
    "quantity": "Quantity",
    "unit_price": "TradePrice",
    "commission": "IBCommission",
    "account": "Account",
}

_PROFILES: tuple[BrokerageProfile, ...] = (
    BrokerageProfile(
        slug="questrade",
        label="Questrade",
        reader="table",
        columns={
            "date": "Transaction Date",
            "action": "Action",
            "symbol": "Symbol",
            "name": "Description",
            "currency": "Currency",
            "description": "Description",
            "quantity": "Quantity",
            "unit_price": "Price",
            "commission": "Commission",
            "amount": "Net Amount",
            "account": "Account",
        },
        required_headers=("date", "action", "symbol", "quantity", "unit_price"),
        date_formats=("%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y"),
        actions=_map(_TRADE_WORDS),
        action_column="Action",
        currency_column="Currency",
        notices=(
            "Questrade: Settlement Date is read and ignored — the trade date "
            "drives every calculation.",
        ),
    ),
    BrokerageProfile(
        slug="wealthsimple",
        label="Wealthsimple",
        reader="table",
        columns={
            "date": "effective_date",
            "action": "activity_type",
            "sub": "activity_sub_type",
            "symbol": "symbol",
            "name": "name",
            "currency": "currency",
            "description": "description",
            "quantity": "quantity",
            "unit_price": "unit_price",
            "commission": "commission",
            "amount": "net_cash_amount",
            "account": "account_id",
        },
        required_headers=("date", "action", "amount"),
        date_formats=(),
        actions=_map(ACTIVITY_MAP, _WS_EXTRAS),
        action_column="activity_type",
        currency_column="currency",
        require_currency=False,
        notices=(
            "Wealthsimple: non-resident tax (NRT) imports as a FEE against the "
            "gross dividend — the dividend row keeps its gross amount and the "
            "withholding shows as its own row, so no net dividend is fabricated.",
            "Wealthsimple: a MoneyMovement/EFT row is a contribution or a "
            'withdrawal according to its description ("Deposit" / "Withdrawal" / '
            '"Money transfer into|out of the account"); with no description the '
            "amount's sign decides, and a negative contribution is refused rather "
            "than imported.",
        ),
    ),
    BrokerageProfile(
        slug="ibkr",
        label="Interactive Brokers",
        reader="ibkr",
        columns=IBKR_FLAT_COLUMNS,
        required_headers=("date", "action", "symbol", "quantity", "unit_price"),
        date_formats=("%Y%m%d;%H%M%S", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"),
        actions=_map(
            _TRADE_WORDS,
            {
                ("Dividend", None): TradeAction.dividend,
                ("Fee", None): TradeAction.fee,
            },
        ),
        action_column="Buy/Sell",
        currency_column="CurrencyPrimary",
        corporate_action_tokens=frozenset(
            {
                "SPLIT",
                "MERGER",
                "SPINOFF",
                "SPIN OFF",
                "REORG",
                "REORGANIZATION",
                "CONVERSION",
            }
        ),
        strict_sections=frozenset({"Corporate Actions"}),
        ignored_sections=frozenset({"Statement", "Account Information"}),
        section_messages={
            "Withholding Tax": (
                "section 'Withholding Tax' is reported on your tax slip and is not imported"
            ),
        },
        sections={
            "Trades": SectionSpec(
                columns={
                    "date": "Date/Time",
                    "symbol": "Symbol",
                    "name": "Symbol",
                    "description": "Code",
                    "currency": "Currency",
                    "quantity": "Quantity",
                    "unit_price": "T. Price",
                    "commission": "Comm/Fee",
                    "amount": "Proceeds",
                },
                sign_from_quantity=True,
                code_column="Code",
            ),
            "Dividends": SectionSpec(
                columns={
                    "date": "Date",
                    "symbol": "Symbol",
                    "description": "Description",
                    "currency": "Currency",
                    "amount": "Amount",
                },
                action=TradeAction.dividend,
                symbol_pattern=r"^\s*([A-Za-z0-9.\-]+)\s*\(",
            ),
            "Deposits & Withdrawals": SectionSpec(
                columns={
                    "date": ("Date", "Settlement Date"),
                    "action": "Type",
                    "description": "Description",
                    "currency": "Currency",
                    "amount": "Amount",
                },
                action_column="Type",
            ),
            "Fees": SectionSpec(
                columns={
                    "date": "Date",
                    "description": "Description",
                    "currency": "Currency",
                    "amount": "Amount",
                },
                action=TradeAction.fee,
            ),
        },
        notices=(
            "Interactive Brokers: run the Flex Query with Format = CSV; the "
            "Activity Statement layout is read block by block.",
        ),
    ),
    BrokerageProfile(
        slug="fidelity",
        label="Fidelity (US)",
        reader="table",
        columns={
            "date": "Run Date",
            "action": "Action",
            "symbol": "Symbol",
            "name": ("Security Description", "Description"),
            "description": ("Description", "Security Description"),
            "quantity": "Quantity",
            "unit_price": ("Price ($)", "Price"),
            "commission": ("Commission ($)",),
            "amount": ("Amount ($)",),
            "account": ("Account", "Account Number"),
        },
        required_headers=("date", "action", "symbol", "quantity"),
        date_formats=("%m/%d/%Y", "%Y-%m-%d"),
        actions=_map(_TRADE_WORDS),
        action_column="Action",
        currency_rule="file_constant",
        currency_constant="USD",
        commission_sum=("Fees ($)",),
        notices=(
            "Fidelity (US): this export states no currency — every row is USD "
            "regardless of the account you import it into.",
        ),
    ),
    BrokerageProfile(
        slug="td-di",
        label="TD Direct Investing",
        reader="table",
        columns={
            "date": "Trade Date",
            "action": "Action",
            "symbol": "Symbol",
            "name": "Description",
            "currency": "Currency",
            "description": "Description",
            "quantity": "Quantity",
            "unit_price": "Price",
            "commission": "Commission",
            "amount": "Net Amount",
        },
        required_headers=("date", "action", "symbol", "amount"),
        date_formats=("%Y-%m-%d", "%d-%b-%Y", "%b %d, %Y"),
        actions=_map(_TRADE_WORDS),
        action_column="Action",
        currency_column="Currency",
        corporate_action_tokens=frozenset(
            {
                "SPLIT",
                "STOCK SPLIT",
                "REVERSE SPLIT",
                "MERGER",
                "MERGE",
                "SPINOFF",
                "SPIN OFF",
                "REORG",
                "REORGANIZATION",
                "CONVERSION",
            }
        ),
        notices=(
            "TD Direct Investing: the CAD and USD sides of an account are "
            "separate Activity exports — import each against its matching account.",
        ),
    ),
    BrokerageProfile(
        slug="rbc-di",
        label="RBC Direct Investing",
        reader="table",
        columns={
            "date": "Date",
            "action": "Activity Type",
            "symbol": "Symbol",
            "name": "Description",
            "currency": "Currency",
            "description": "Description",
            "quantity": "Quantity",
            "unit_price": "Price",
            "amount": "Settlement Amount",
        },
        required_headers=("date", "action", "symbol", "quantity"),
        date_formats=("%Y-%m-%d", "%d-%b-%Y", "%b %d, %Y"),
        actions=_map(_TRADE_WORDS),
        action_column="Activity Type",
        currency_rule="row_column",
        currency_column="Currency",
        require_currency=False,
        notices=(
            "RBC Direct Investing: this export has no Commission column — trades "
            "import with commission 0.00, so check them against your statement.",
        ),
    ),
    BrokerageProfile(
        slug="nbdb",
        label="National Bank Direct Brokerage",
        reader="table",
        columns={
            "date": "Date",
            "action": "Type",
            "symbol": ("Symbol", "Symbole"),
            "name": ("Symbol", "Symbole"),
            "currency": ("Currency", "Devise"),
            "description": ("Description", "Libelle"),
            "quantity": ("Quantity", "Quantite"),
            "unit_price": ("Price", "Prix"),
            "commission": "Commission",
            "amount": ("Total", "Total"),
            "account": ("Account", "Compte"),
        },
        required_headers=("date", "action", "symbol", "quantity", "unit_price"),
        date_formats=("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"),
        actions=_map(_TRADE_WORDS, _FRENCH_WORDS),
        action_column="Type",
        currency_rule="row_column",
        currency_column="Currency",
        require_currency=False,
        notices=(
            "National Bank Direct Brokerage: French exports are "
            "semicolon-separated and are read as-is; rows with no Currency fall "
            "back to the account you import into.",
        ),
    ),
)

BROKERAGE_PROFILES: dict[str, BrokerageProfile] = {p.slug: p for p in _PROFILES}
#: Stable order — the wizard picker and docs/41's table read it.
PROFILE_SLUGS: tuple[str, ...] = tuple(p.slug for p in _PROFILES)


def get_profile(slug: str) -> BrokerageProfile | None:
    return BROKERAGE_PROFILES.get(slug)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _delimiter(text: str) -> str:
    """Delimiter from the first physical line (NBDB's French export is `;`)."""
    first = text.splitlines()[0] if text.splitlines() else ""
    counts = {d: first.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def _cell_from(record: Mapping[str, str], name: str | tuple[str, ...] | None) -> str:
    """Trimmed value of a section column (or the first candidate present)."""
    if not name:
        return ""
    candidates = (name,) if isinstance(name, str) else name
    for candidate in candidates:
        if candidate in record:
            return (record.get(candidate) or "").strip()
    return ""


_CURRENCY_TOKENS = ("CAD$", "US$", "USD", "CAD", "CAN", "C$", "U$", "$")


def _number(text: str) -> str:
    """Render a broker numeric cell as a plain Decimal literal.

    Never a float (docs invariant: decimals end-to-end). Strips ``$`` and a
    known currency token, then resolves the separators: when BOTH ``.`` and
    ``,`` appear the LAST one is the decimal point (``1.234,56`` and
    ``1,234.56`` both parse); a lone comma with 1-2 digits after it is a
    decimal comma (NBDB's French export); a lone comma with exactly 3 digits
    after it is genuinely ambiguous between thousands and a French decimal, so
    it is left for ``_parse_decimal`` to refuse — strict beats a silently
    doubled or halved quantity.
    """
    value = text.strip().replace("$", "")
    while True:
        token = next((t for t in _CURRENCY_TOKENS if value.upper().startswith(t)), None)
        if token is None:
            break
        value = value[len(token) :]
    while True:
        token = next((t for t in _CURRENCY_TOKENS if value.upper().endswith(t)), None)
        if token is None:
            break
        value = value[: -len(token)]
    value = value.strip()
    if not re.fullmatch(r"-?[\d.,]+", value):
        return text.strip()
    if "," in value and "." in value:
        last = max(value.rfind("."), value.rfind(","))
        other = "." if value[last] == "," else ","
        value = value.replace(other, "").replace(value[last], ".")
    elif "," in value:
        head, _, tail = value.partition(",")
        if len(tail) != 3:
            value = f"{head}.{tail}"
        # exactly 3 digits: ambiguous -> hand it to _parse_decimal untouched
    elif value.count(".") > 1:
        head, _, tail = value.rpartition(".")
        value = f"{head}{tail}" if len(tail) == 3 else text.strip()
    return value


def _sum_text(values: list[str]) -> str:
    """Sum present numeric cells as Decimal and render them (never float)."""
    total = Decimal("0")
    for value in values:
        total += Decimal(_number(value))
    return format(total, "f")


def _records(text: str) -> Iterator[tuple[int, list[str]]]:
    """(physical start line, fields) for every non-empty record.

    Start line — not the csv module's end-of-record ``line_num`` — is what the
    user sees when a row is refused, and IBKR statements are blank-line padded.
    """
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text))
    previous = 0
    for row in reader:
        if not row:
            previous = reader.line_num
            continue
        start = previous + 1
        previous = reader.line_num
        yield start, row


def _header_index(header: list[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for raw in header:
        index.setdefault(_norm(raw), raw)
    return index


def _resolve(
    columns: Mapping[str, str | tuple[str, ...]],
    field_name: str,
    index: Mapping[str, str],
) -> list[str]:
    """Canonical field -> the source header present in this file (if any).

    A tuple is an ordered candidate list: the first header the file actually
    carries wins, so `Security Description` and `Description` can both be
    declared for one canonical field without both ever being read.
    """
    spec = columns.get(field_name)
    if spec is None:
        return []
    candidates = (spec,) if isinstance(spec, str) else spec
    present = [index[_norm(name)] for name in candidates if _norm(name) in index]
    return present[:1]


def _expected(columns: Mapping[str, str | tuple[str, ...]], field_name: str) -> str:
    spec = columns.get(field_name)
    if spec is None:
        return field_name
    return " | ".join((spec,) if isinstance(spec, str) else spec)


def _require_headers(
    profile: BrokerageProfile, index: Mapping[str, str], header: list[str]
) -> None:
    missing = [key for key in profile.required_headers if not _resolve(profile.columns, key, index)]
    if missing:
        raise ProfileHeaderMismatchError(
            f"{profile.label}: the file header does not match this profile — "
            f"missing {', '.join(_expected(profile.columns, key) for key in missing)} "
            f"(header: {', '.join(header)})"
        )


def _corporate_action(profile: BrokerageProfile, token: str) -> bool:
    return bool(token) and _token_norm(token) in profile.action_tokens


def _section_corporate_error(profile: BrokerageProfile, line: int, section: str) -> None:
    raise UnknownCorporateActionError(
        f"{profile.label}: line {line} is in the {section!r} section of the "
        "statement. docs/06 pins corporate actions as manual "
        "corporate_action_adjustment entries and no supported export states the "
        "value that entry needs, so the whole file is refused rather than "
        "imported wrong. Enter it by hand (POST /trades) and re-import the rest, "
        "or re-export with that section excluded."
    )


def _corporate_error(profile: BrokerageProfile, line: int, token: str) -> None:
    raise UnknownCorporateActionError(
        f"{profile.label}: line {line} carries the corporate action {token!r}. "
        "docs/06 pins corporate actions as manual "
        "corporate_action_adjustment entries and no supported export states the "
        "value that entry needs, so the whole file is refused rather than "
        "imported wrong. Enter it by hand (POST /trades) and re-import the rest, "
        "or re-export with that activity excluded."
    )


def _resolve_currency(
    profile: BrokerageProfile,
    row_currency: str,
    account_currency: str,
    line: int,
    activity_type: str,
    symbol: str,
    date_text: str,
    unmapped: list[UnmappedRow],
    column_label: str | None = None,
) -> str | None:
    """Apply the profile's currency rule; returns None when the row is refused."""
    if profile.currency_rule == "file_constant":
        # The file never states a currency and the account never overrides it.
        return profile.currency_constant
    token = row_currency.strip().upper()
    if _ISO3.match(token):
        return token
    if profile.require_currency:
        unmapped.append(
            UnmappedRow(
                line=line,
                activity_type=activity_type,
                activity_sub_type=None,
                symbol=symbol,
                date=date_text or None,
                reason=(
                    f"{profile.label}: missing "
                    f"{column_label or profile.currency_column or 'currency'} "
                    f"for {symbol or 'this row'}"
                ),
                institution=profile.label,
            )
        )
        return None
    # require_currency=false: the row is KEPT and the target account's
    # currency decides (docs/41 multi-currency rules).
    return account_currency


def _canonical_from_table(
    profile: BrokerageProfile,
    header: list[str],
    line: int,
    fields: list[str],
    account_currency: str,
    unmapped: list[UnmappedRow],
) -> dict[str, str] | None:
    index = _header_index(header)
    positions: dict[str, list[int]] = {}
    for key in CANONICAL_FIELDS:
        headers = _resolve(profile.columns, key, index)
        positions[key] = [header.index(name) for name in headers]
    positions["commission"] += [
        header.index(index[_norm(name)]) for name in profile.commission_sum if _norm(name) in index
    ]

    def text_of(key: str) -> str:
        for position in positions[key]:
            if position < len(fields):
                return (fields[position] or "").strip()
        return ""

    def number_of(key: str) -> str:
        values = [fields[p].strip() for p in positions[key] if p < len(fields)]
        values = [v for v in values if v]
        if not values:
            return ""
        if len(values) == 1:
            return _number(values[0])
        return _sum_text(values)

    activity_type = text_of("action")
    if _corporate_action(profile, activity_type):
        _corporate_error(profile, line, activity_type)
    if profile.currency_rule == "row_column" and not positions["currency"]:
        # The file carries no currency column at all.
        if profile.require_currency:
            raise ProfileHeaderMismatchError(
                f"{profile.label}: no {profile.currency_column} column but the profile requires one"
            )
        currency = account_currency
    else:
        currency = _resolve_currency(
            profile,
            text_of("currency"),
            account_currency,
            line,
            activity_type,
            text_of("symbol"),
            text_of("date"),
            unmapped,
        )
        if currency is None:
            return None

    return {
        "effective_date": text_of("date"),
        "activity_type": activity_type,
        "activity_sub_type": text_of("sub"),
        "symbol": text_of("symbol"),
        "name": text_of("name"),
        "currency": currency,
        "description": text_of("description"),
        "quantity": number_of("quantity"),
        "unit_price": number_of("unit_price"),
        "commission": number_of("commission"),
        "net_cash_amount": number_of("amount"),
        "account_id": text_of("account"),
        "__line__": str(line),
    }


def _read_table(
    profile: BrokerageProfile, text: str, account_currency: str
) -> tuple[list[dict[str, str]], list[UnmappedRow], list[str]]:
    records = list(_records(text))
    if not records:
        raise InvestmentCsvProfileError("CSV file has no header row")
    header_line, header = records[0]
    if len(header) < 2:
        raise InvestmentCsvProfileError(
            f"CSV file has no header row (line {header_line}: {', '.join(header)!r})"
        )
    index = _header_index(header)
    _require_headers(profile, index, header)

    rows: list[dict[str, str]] = []
    unmapped: list[UnmappedRow] = []
    for line, fields in records[1:]:
        if not any(cell.strip() for cell in fields):
            continue
        if line == header_line:
            continue
        if len(fields) != len(header):
            # A ragged row would be read against the WRONG column and land as a
            # wrong trade — refuse it and show the counts (docs/41).
            unmapped.append(
                UnmappedRow(
                    line=line,
                    activity_type=fields[0].strip() if fields else "",
                    activity_sub_type=None,
                    symbol="",
                    date=fields[0].strip() or None if fields else None,
                    reason=(
                        f"{profile.label}: row has {len(fields)} fields, header "
                        f"has {len(header)} — refusing the row rather than "
                        "reading shifted columns"
                    ),
                    institution=profile.label,
                )
            )
            continue
        canonical = _canonical_from_table(profile, header, line, fields, account_currency, unmapped)
        if canonical is not None:
            rows.append(canonical)
    return rows, unmapped, []


def _read_ibkr(
    profile: BrokerageProfile, text: str, account_currency: str
) -> tuple[list[dict[str, str]], list[UnmappedRow], list[str]]:
    headers: dict[str, list[str]] = {}
    rows: list[dict[str, str]] = []
    unmapped: list[UnmappedRow] = []
    notices: list[str] = []
    section_rows: dict[str, int] = {}

    for line, fields in _records(text):
        if len(fields) < 2:
            continue
        section = fields[0].strip()
        kind = fields[1].strip().casefold()
        if kind == "header":
            headers[section] = [name.strip() for name in fields[2:]]
            continue
        if kind != "data":
            continue  # SubHeader / Total / Summary furniture
        if section in profile.ignored_sections:
            continue
        if section in profile.strict_sections:
            _section_corporate_error(profile, line, section)
        spec = profile.sections.get(section)
        if spec is None:
            section_rows[section] = section_rows.get(section, 0) + 1
            continue
        section_rows[section] = section_rows.get(section, 0) + 1

        section_header = headers.get(section)
        if not section_header:
            unmapped.append(
                UnmappedRow(
                    line=line,
                    activity_type=section,
                    activity_sub_type=None,
                    symbol="",
                    date=None,
                    reason=f"{profile.label}: section {section!r} has no Header row",
                    institution=profile.label,
                )
            )
            continue
        payload = fields[2:]
        if len(payload) != len(section_header):
            unmapped.append(
                UnmappedRow(
                    line=line,
                    activity_type=section,
                    activity_sub_type=None,
                    symbol="",
                    date=None,
                    reason=(
                        f"{profile.label}: section {section!r} data/header arity "
                        f"mismatch ({len(payload)} vs {len(section_header)})"
                    ),
                    institution=profile.label,
                )
            )
            continue
        record = dict(zip(section_header, payload, strict=True))

        quantity_text = _cell_from(record, spec.columns.get("quantity"))
        if spec.code_column:
            code = _cell_from(record, spec.code_column)
            if _corporate_action(profile, code):
                _corporate_error(profile, line, code)
        if spec.action is not None:
            activity_type = spec.action.value
        elif spec.action_column is not None:
            activity_type = _cell_from(record, spec.action_column)
        elif spec.sign_from_quantity:
            # Flex trades carry no Buy/Sell word — the sign IS the side.
            try:
                magnitude = Decimal(_number(quantity_text)) if quantity_text else Decimal("0")
            except InvalidOperation:
                unmapped.append(
                    UnmappedRow(
                        line=line,
                        activity_type=section,
                        activity_sub_type=None,
                        symbol=_cell_from(record, spec.columns.get("symbol")),
                        date=_cell_from(record, spec.columns.get("date")) or None,
                        reason=f"{profile.label}: unparseable quantity {quantity_text!r}",
                        institution=profile.label,
                    )
                )
                continue
            activity_type = "Buy" if magnitude > 0 else "Sell" if magnitude < 0 else ""
        else:
            activity_type = ""

        description = _cell_from(record, spec.columns.get("description"))
        symbol = _cell_from(record, spec.columns.get("symbol"))
        if not symbol and spec.symbol_pattern and description:
            matched = re.match(spec.symbol_pattern, description)
            if matched:
                symbol = matched.group(1)

        date_text = _cell_from(record, spec.columns.get("date"))
        currency = _resolve_currency(
            profile,
            _cell_from(record, spec.columns.get("currency")),
            account_currency,
            line,
            activity_type,
            symbol,
            date_text,
            unmapped,
            spec.columns.get("currency") or "Currency",
        )
        if currency is None:
            continue

        rows.append(
            {
                "effective_date": date_text,
                "activity_type": activity_type,
                "activity_sub_type": "",
                "symbol": symbol,
                "name": symbol or _cell_from(record, spec.columns.get("name")),
                "currency": currency,
                "description": description,
                "quantity": _number(quantity_text),
                "unit_price": _number(_cell_from(record, spec.columns.get("unit_price"))),
                "commission": _number(_cell_from(record, spec.columns.get("commission"))),
                "net_cash_amount": _number(_cell_from(record, spec.columns.get("amount"))),
                "account_id": _cell_from(record, spec.columns.get("account")),
                "__line__": str(line),
            }
        )

    for section, count in section_rows.items():
        if section in profile.sections:
            continue
        message = profile.section_messages.get(section)
        if message:
            notices.append(f"{profile.label}: {message} ({count} rows).")
        else:
            notices.append(f"{profile.label}: section {section!r} ({count} rows) is not imported.")
    return rows, unmapped, notices


def parse_brokerage_csv(
    raw: bytes,
    slug: str | None,
    *,
    account_currency: str,
) -> BrokerageFile | None:
    """Parse a brokerage activity CSV, or return ``None`` when no profile fits.

    ``slug=None`` auto-detects from the header; a detected profile that fails
    its own header check still raises (a file is never force-fit). An explicit
    ``slug`` that the registry does not hold raises ``UnknownProfileError``.
    """
    text = decode_raw(raw)
    profile = detect_profile(text) if slug in (None, "") else get_profile(slug)
    if profile is None:
        if slug in (None, ""):
            return None
        raise UnknownProfileError(
            f"unknown brokerage profile {slug!r}; expected one of {', '.join(PROFILE_SLUGS)}"
        )

    try:
        if profile.reader == "ibkr" and _is_sectioned(text):
            canonical, unmapped, notices = _read_ibkr(profile, text, account_currency)
        else:
            canonical, unmapped, notices = _read_table(profile, text, account_currency)
    except csv.Error as exc:
        raise InvestmentCsvProfileError(f"malformed CSV: {exc}") from None

    entries = [
        (int(row.pop("__line__")), row)  # type: ignore[arg-type]
        for row in canonical
    ]
    rows, semantic_unmapped = build_trades(
        entries,
        profile.actions,
        institution=profile.label,
        tolerant=True,
        require_quantity=profile.require_quantity,
        date_formats=profile.date_formats or None,
        # M82: every profile file normalises a TRADE's commission to a positive
        # magnitude (a broker that writes a negative IBCommission still means a
        # cost). It does NOT rewrite a cash-only row's amount since M131 —
        # fee/withdrawal keep the file's native sign and their cash effect is
        # `-abs(price_native)` (docs/05) — so a cash row stays honest on every
        # surface that renders it directly.
        abs_magnitudes=True,
    )
    return BrokerageFile(
        profile=profile,
        rows=rows,
        unmapped=[*unmapped, *semantic_unmapped],
        notices=[*profile.notices, *notices],
    )


def _is_sectioned(text: str) -> bool:
    """IBKR Activity Statement shape: ``<Section>,Header,...`` on the first line."""
    first = next(iter(_records(text)), None)
    if first is None:
        return False
    _, header = first
    return len(header) >= 2 and header[1].strip().casefold() == "header"


def detect_profile(text_or_raw: str | bytes) -> BrokerageProfile | None:
    """Header-signature match against every profile, in `PROFILE_SLUGS` order."""
    text = decode_raw(text_or_raw) if isinstance(text_or_raw, bytes) else text_or_raw
    try:
        first = next(iter(_records(text)), None)
    except csv.Error:
        return None
    if first is None:
        return None
    _, header = first
    index = _header_index(header)
    if len(header) >= 2 and header[1].strip().casefold() == "header":
        return BROKERAGE_PROFILES["ibkr"]
    for slug in PROFILE_SLUGS:
        profile = BROKERAGE_PROFILES[slug]
        if all(_resolve(profile.columns, key, index) for key in profile.required_headers):
            return profile
    return None
