"""Investment CSV importer for Wealthsimple-style exports (docs/05).

Parses Wealthsimple brokerage CSVs and produces RawTrade objects consumed by
the POST /trades/import endpoint. Activity types are mapped to TradeAction
via a fixed lookup table; unknown types and malformed rows are returned as
UnmappedRow entries rather than raising, so one bad row never rejects the
whole file.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date as dt_date
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.importers.base import RawTrade, decode_raw
from app.models.trades import CASH_ONLY_ACTIONS, CASH_OUTFLOW_ACTIONS, TradeAction

# Actions that carry shares and therefore make no sense with a zero quantity
# (docs/02 signed-quantity convention). Only enforced when a caller opts in —
# the legacy Wealthsimple path keeps its historical tolerance.
REQUIRE_QUANTITY_ACTIONS = frozenset(
    {
        TradeAction.buy,
        TradeAction.sell,
        TradeAction.drip,
        TradeAction.transfer_in,
        TradeAction.transfer_out,
    }
)

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def normalize_action_token(value: str) -> str:
    """Upper-case and collapse every non-alphanumeric run to one space.

    ``"Buy - Express"`` -> ``"BUY EXPRESS"``, ``"TRANSFER_FEE_REIMBURSEMENT"``
    -> ``"TRANSFER FEE REIMBURSEMENT"``. Used ONLY as the second (tolerant)
    lookup stage for brokerage profiles — see docs/41.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_NON_ALNUM.sub(" ", folded.upper()).split())


ACTIVITY_MAP: dict[tuple[str, str | None], TradeAction] = {
    # Buy variants
    ("Buy", None): TradeAction.buy,
    ("Buy", "DRIP"): TradeAction.drip,
    ("Trade", "BUY"): TradeAction.buy,
    ("Trade", "SELL"): TradeAction.sell,
    ("Trade", "DRIP"): TradeAction.drip,
    # Sell
    ("Sell", None): TradeAction.sell,
    # Income
    ("Dividend", None): TradeAction.dividend,
    ("Dividend", "DRIP"): TradeAction.drip,
    ("Distribution", None): TradeAction.distribution,
    ("ReturnOfCapital", None): TradeAction.return_of_capital,
    ("Interest", None): TradeAction.dividend,
    # Cash movements
    ("SecurityTransfer", None): TradeAction.transfer_in,
    ("MoneyMovement", "EFT"): TradeAction.contribution,
    # WS cash transfers carry direction ONLY in `net_cash_amount`'s sign — the
    # static value below is the fallback; CASH_TRANSFER_KEYS re-reads the sign
    # per row (negative = out, positive = in) before any abs() can erase it.
    ("MoneyMovement", "TRANSFER"): TradeAction.transfer_out,
    ("MoneyMovement", "TRANSFER_TF"): TradeAction.transfer_in,
    ("MoneyMovement", "WITHDRAWAL"): TradeAction.withdrawal,
    ("MoneyMovement", "CONTRIBUTION"): TradeAction.contribution,
    ("MoneyMovement", "DEPOSIT"): TradeAction.contribution,
    # Grants / government contributions
    ("Grant", "CESG"): TradeAction.contribution,
    ("Grant", "CLB"): TradeAction.contribution,
    ("Grant", "BCTESG"): TradeAction.contribution,
    ("Grant", None): TradeAction.contribution,
    # Fees
    ("AdministrativePayment", "TRANSFER_FEE_REIMBURSEMENT"): TradeAction.fee,
    ("Fee", None): TradeAction.fee,
    ("AdministrativePayment", None): TradeAction.fee,
    # Non-resident withholding tax on US dividends (WS `Tax/NRT`): an
    # income-side COST, mapped to `fee`. The dividend row already carries the
    # GROSS amount, so the withholding shows as its own fee row the user can
    # see — gross + fee is the auditable representation. Never fabricate a
    # "net dividend": the file states no such row (M113, docs/03).
    ("Tax", "NRT"): TradeAction.fee,
}

#: Wealthsimple cash-transfer keys (M113, docs/03 + docs/41). These rows are
#: CASH wearing transfer vocabulary — no symbol, quantity 0 — and their
#: direction exists nowhere except `net_cash_amount`'s SIGN: negative = money
#: out, positive = money in (both real exports agree: TFSA `TRANSFER_TF` rows
#: are positive "Money transfer into the account", the RESP `TRANSFER` row is
#: negative "Money transfer out of the account"). The sign is read in
#: `build_trades` BEFORE any `abs()`, so `abs_magnitudes` cannot erase it.
#: `SecurityTransfer` is deliberately NOT here: an in-kind share transfer
#: always carries a symbol and keeps the symbol/cost-basis rules.
CASH_TRANSFER_KEYS: frozenset[tuple[str, str | None]] = frozenset(
    {("MoneyMovement", "TRANSFER"), ("MoneyMovement", "TRANSFER_TF")}
)

#: The same keys after the tolerant stage-2 normalisation, so a decorated
#: label resolves to the same cash rule instead of silently falling back to
#: the share-transfer branch (which would refuse a symbol-less row).
CASH_TRANSFER_KEYS_NORM: frozenset[tuple[str, str | None]] = frozenset(
    (normalize_action_token(kind), normalize_action_token(sub) if sub else None)
    for kind, sub in CASH_TRANSFER_KEYS
)

#: Wealthsimple EFT rows (M131, docs/03): a contribution AND a withdrawal both
#: arrive as ``MoneyMovement/EFT`` — the direction lives in the DESCRIPTION
#: ("Deposit" / "Withdrawal" / "Money transfer into|out of the account"), not in
#: the sub-type. ACTIVITY_MAP alone therefore mapped every EFT to
#: ``contribution``, and the "Withdrawal" row (-24,318.76) was stored
#: as a NEGATIVE contribution: the cash total survived (the effect is the
#: signed amount) but contribution-room math, the value-history marker and
#: every semantic read of the row lied. Direction is decided per row here —
#: description first, ``net_cash_amount``'s sign when no pattern matches — so
#: a sign-only export still lands correctly.
CASH_DIRECTION_KEYS: frozenset[tuple[str, str | None]] = frozenset({("MoneyMovement", "EFT")})

#: The same rule after stage-2 normalisation (see CASH_TRANSFER_KEYS_NORM).
CASH_DIRECTION_KEYS_NORM: frozenset[tuple[str, str | None]] = frozenset(
    (normalize_action_token(kind), normalize_action_token(sub) if sub else None)
    for kind, sub in CASH_DIRECTION_KEYS
)

#: Fee-REIMBURSEMENT rows (M132, docs/02 §trades — cash-reimbursement rows). WS states
#: one of these POSITIVE when it credits money back into the account ("CAD
#: credited", +145.75 on real account 731), so it is money IN — while
#: ``TradeAction.fee``'s cash effect is ``-abs(price_native)``, ALWAYS money
#: out (M131). Mapping the credit to `fee` therefore subtracted it twice and
#: the ledger could never reconcile to Wealthsimple's own cash line: on
#: account 731 the two credits were 2 x 145.75 CAD of the -582.73-vs-$0.27
#: gap. A POSITIVE reimbursement is stored as a CASH ``transfer_in`` (money
#: arrived from outside the account, docs/06 in-kind rules untouched); a
#: NEGATIVE one is money out and keeps the ``fee`` rule.
REIMBURSEMENT_KEYS: frozenset[tuple[str, str | None]] = frozenset(
    {("AdministrativePayment", "TRANSFER_FEE_REIMBURSEMENT")}
)

#: The same key after stage-2 normalisation.
REIMBURSEMENT_KEYS_NORM: frozenset[tuple[str, str | None]] = frozenset(
    (normalize_action_token(kind), normalize_action_token(sub) if sub else None)
    for kind, sub in REIMBURSEMENT_KEYS
)

_WITHDRAWAL_DESCRIPTION = re.compile(r"withdraw", re.IGNORECASE)
_DEPOSIT_DESCRIPTION = re.compile(r"\bdeposit\b", re.IGNORECASE)
_TRANSFER_IN_DESCRIPTION = re.compile(r"transfer\s+(?:money\s+)?into", re.IGNORECASE)
_TRANSFER_OUT_DESCRIPTION = re.compile(r"transfer\s+(?:money\s+)?out", re.IGNORECASE)


def cash_row_direction(description: str, net_cash: Decimal) -> TradeAction:
    """contribution (money in) vs withdrawal (money out) for a WS cash row.

    The description decides when it says anything (M131); a file that carries
    none falls back to ``net_cash_amount``'s sign, negative = money out. A
    withdrawal is stored at its NEGATIVE native amount — `withdrawal`'s cash
    effect is ``-abs(price_native)`` (docs/05), so the ledger reads the same
    either way while the row's own number stays honest.
    """
    if _WITHDRAWAL_DESCRIPTION.search(description) or _TRANSFER_OUT_DESCRIPTION.search(description):
        return TradeAction.withdrawal
    if _DEPOSIT_DESCRIPTION.search(description) or _TRANSFER_IN_DESCRIPTION.search(description):
        return TradeAction.contribution
    return TradeAction.contribution if net_cash >= 0 else TradeAction.withdrawal


class InvestmentCsvProfileError(ValueError):
    """Raised for unrecognized activity types or malformed CSV data."""


@dataclass(frozen=True)
class UnmappedRow:
    """A CSV row that could not be imported, with the reason it was skipped."""

    line: int
    activity_type: str
    activity_sub_type: str | None
    symbol: str
    date: str | None
    reason: str
    # Profile label when a brokerage profile (docs/41) produced this row; the
    # legacy `parse_investment_csv(raw)` path leaves it None.
    institution: str | None = None
    # Machine-readable refusal class. "stuck" = staging answers 422 instead of
    # skipping the row; None = the row is simply unmappable (docs/41).
    code: str | None = None


def _parse_date(date_str: str) -> dt_date | None:
    """Best-effort parse of a CSV date. Returns None if unparseable."""
    s = (date_str or "").strip()
    # Strip a trailing time component: "2024-01-02 12:00:00" or "...T12:00:00".
    for sep in ("T", " "):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    try:
        return dt_date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(text: str, field: str) -> Decimal:
    """Parse a CSV decimal field; raises on empty or unparseable values."""
    text = text.strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise InvestmentCsvProfileError(f"Invalid {field}: {text!r}") from exc


def parse_investment_csv(
    raw: bytes,
    activity_map: dict[tuple[str, str | None], TradeAction] | None = None,
    account_currency: str | None = None,
) -> tuple[list[RawTrade], list[UnmappedRow]]:
    """Parse a Wealthsimple-style investment CSV into RawTrade objects.

    ``account_currency`` (M132) is the currency of the account the file is
    imported into: a row whose currency cell is empty reads as that currency
    instead of the historical hard-coded ``USD``. ``None`` keeps the legacy
    fallback for callers that have no account in scope.

    Returns a tuple of (parsed trades, unmapped/skipped rows). Unknown
    activity types, unparseable dates, missing symbols, and malformed
    numeric fields are collected as UnmappedRow and skipped — the whole
    file is never rejected for one bad row.

    Raises:
        InvestmentCsvProfileError: When the CSV has no header row, or when
            the csv module rejects the input (audit L5: csv.Error must never
            escape as a generic 500 — api/trades.py maps this error to a 422
            ``parse_error`` envelope).
    """
    lookup = activity_map if activity_map is not None else ACTIVITY_MAP
    text = decode_raw(raw)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None:
            raise InvestmentCsvProfileError("CSV file has no header row")
        return _read_investment_rows(reader, lookup, account_currency=account_currency)
    except csv.Error as exc:
        raise InvestmentCsvProfileError(f"malformed CSV: {exc}") from None


def _read_investment_rows(
    reader: csv.DictReader,
    lookup: dict[tuple[str, str | None], TradeAction],
    account_currency: str | None = None,
) -> tuple[list[RawTrade], list[UnmappedRow]]:
    """Legacy DictReader entry point (docs/05): physical line 2 onward."""
    return build_trades(enumerate(reader, start=2), lookup, account_currency=account_currency)


def _cell(row: Mapping[str, str | None], field: str) -> str:
    """Trimmed string value of a canonical field; missing/ragged -> ``\"\"``."""
    value = row.get(field)
    return value.strip() if isinstance(value, str) else ""


def _try_date(text: str, formats: tuple[str, ...]) -> dt_date | None:
    """First pinned format that parses *text*; also tries the ISO time prefix."""
    candidates = [text]
    for sep in ("T", " "):
        if sep in text:
            candidates.append(text.split(sep, 1)[0])
    for candidate in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def build_trades(
    entries: Iterable[tuple[int, Mapping[str, str | None]]],
    lookup: Mapping[tuple[str, str | None], TradeAction],
    *,
    institution: str | None = None,
    tolerant: bool = False,
    require_quantity: bool = False,
    date_formats: tuple[str, ...] | None = None,
    require_currency: bool = False,
    abs_magnitudes: bool = False,
    account_currency: str | None = None,
) -> tuple[list[RawTrade], list[UnmappedRow]]:
    """Drive canonical activity rows (docs/41), collecting per-row problems.

    This is the ONE place trade-mutation semantics live: sign convention,
    cash-only handling, transfer cost basis, symbol/quantity requirements.
    Every brokerage profile funnels here, so a profile cannot drift from the
    Wealthsimple path.

    - ``institution`` prefixes every reason and fills ``UnmappedRow.institution``
      so a preview row is per-institution distinct (docs/41). ``None`` keeps the
      legacy wording byte-for-byte.
    - ``tolerant`` adds the second lookup stage (normalised action token); the
      legacy path stays exact-match only.
    - ``require_quantity`` rejects a zero-quantity share row (profiles only).
    - ``date_formats`` replaces the legacy best-effort date parser with the
      profile's pinned, self-delimiting formats — ambiguity is never guessed.
    - ``require_currency`` refuses a row with an empty currency cell instead
      of falling back (profiles only; the legacy path defaults to USD).
    - ``abs_magnitudes`` makes a trade's COMMISSION positive, so a broker that
      writes IBCommission as a negative number still records a cost. It does
      NOT rewrite a cash-only row's amount (M131): ``fee``/``withdrawal`` keep
      the file's native sign and their cash effect is ``-abs(price_native)``
      (docs/05), which is direction-safe either way. A cash TRANSFER's amount
      is a magnitude because M113 already read its direction from the sign.
    - ``account_currency`` is the currency of the account the file is being
      imported INTO (M132). A row whose currency cell is empty reads as the
      TARGET ACCOUNT's currency — never a hard-coded USD, which is what a
      blank cell on a CAD account used to become — and the value is written
      onto ``RawTrade.currency``, the authority ``trades.currency`` stores.
    """
    norm_lookup: dict[tuple[str, str | None], TradeAction] | None = None
    if tolerant:
        norm_lookup = {}
        for (raw_type, raw_sub), mapped in lookup.items():
            norm_key = (
                normalize_action_token(raw_type),
                normalize_action_token(raw_sub) if raw_sub else None,
            )
            norm_lookup[norm_key] = mapped

    def skip(
        line: int,
        type_: str,
        sub: str | None,
        symbol: str,
        when: str | None,
        text: str,
        code: str | None = None,
    ):
        return UnmappedRow(
            line=line,
            activity_type=type_,
            activity_sub_type=sub,
            symbol=symbol,
            date=when,
            reason=f"{institution}: {text}" if institution else text,
            institution=institution,
            code=code,
        )

    trades: list[RawTrade] = []
    unmapped: list[UnmappedRow] = []
    for line_num, row in entries:
        # Skip fully-blank rows: trailing newlines and short/blank lines yield
        # None or empty cells, which must not be treated as a real trade.
        # (Ragged rows park their overflow under the DictReader ``None`` key as
        # a list — filtering to str keeps that from ever raising a 500.)
        cells = [value for value in row.values() if isinstance(value, str)]
        if not cells or all(not cell.strip() for cell in cells):
            continue

        activity_type = _cell(row, "activity_type")
        sub_type_raw = _cell(row, "activity_sub_type")
        # Treat common "empty" markers (dash, N/A) as None
        sub_type: str | None = (
            sub_type_raw if sub_type_raw and sub_type_raw not in ("-", "N/A", "n/a", "NA") else None
        )

        key = (activity_type, sub_type)
        action = lookup.get(key)
        if action is None and norm_lookup is not None:
            action = norm_lookup.get(
                (
                    normalize_action_token(activity_type),
                    normalize_action_token(sub_type) if sub_type else None,
                )
            )
        if action is None:
            unmapped.append(
                skip(
                    line_num,
                    activity_type,
                    sub_type,
                    _cell(row, "symbol"),
                    _cell(row, "effective_date") or None,
                    f"Unknown activity: {activity_type!r} / {sub_type!r}",
                )
            )
            continue

        symbol = _cell(row, "symbol")
        security_name = _cell(row, "name")
        currency = _cell(row, "currency")
        if not currency and require_currency:
            unmapped.append(
                skip(
                    line_num,
                    activity_type,
                    sub_type,
                    symbol,
                    _cell(row, "effective_date") or None,
                    "Missing currency",
                )
            )
            continue
        # M132 (docs/02 §trades): the row's OWN currency — the file's cell when
        # it states one, otherwise the currency of the account being imported
        # into. It is never a hard-coded "USD" (a blank cell on a CAD account
        # is CAD), because this exact value is what `trades.currency` stores and
        # the cash leg converts at it. Uppercased so it satisfies the table's
        # `^[A-Z]{3}$` constraint whatever the exporter wrote.
        currency = (currency or account_currency or "USD").strip().upper()
        description = _cell(row, "description")

        # Parse date (profile formats first, legacy best-effort otherwise)
        date_str = _cell(row, "effective_date")
        if date_formats is None:
            date = _parse_date(date_str)
        else:
            date = _try_date(date_str, date_formats)
        if date is None:
            detail = (
                f"Unparseable date: {date_str!r} (expected one of {', '.join(date_formats)})"
                if date_formats
                else f"Unparseable date: {date_str!r}"
            )
            unmapped.append(
                skip(line_num, activity_type, sub_type, symbol, date_str or None, detail)
            )
            continue

        try:
            quantity = _parse_decimal(row.get("quantity") or "0", "quantity")
            unit_price = _parse_decimal(row.get("unit_price") or "0", "unit_price")
            commission = _parse_decimal(row.get("commission") or "0", "commission")
            net_cash = _parse_decimal(row.get("net_cash_amount") or "0", "net_cash_amount")
        except InvestmentCsvProfileError as exc:
            unmapped.append(
                skip(line_num, activity_type, sub_type, symbol, date_str or None, str(exc))
            )
            continue

        # M113 cash-transfer keys (docs/03, docs/41): direction is read from
        # net_cash_amount's SIGN HERE — before the sign convention and before
        # any abs() below, so `abs_magnitudes` can never turn a transfer-out
        # into a transfer-in (or vice versa). Negative = money out, positive =
        # money in; a zero amount is not money moving out, so it stays `in`.
        cash_transfer = key in CASH_TRANSFER_KEYS
        if not cash_transfer:
            cash_transfer = (
                normalize_action_token(activity_type),
                normalize_action_token(sub_type) if sub_type else None,
            ) in CASH_TRANSFER_KEYS_NORM
        if cash_transfer:
            action = TradeAction.transfer_in if net_cash >= 0 else TradeAction.transfer_out

        # M131 cash-direction keys (docs/03): an EFT row's contribution-vs-
        # withdrawal lives in the DESCRIPTION, with net_cash's sign as the
        # fallback. Same placement as the transfer rule above — decided before
        # any abs() so the sign can never be erased before it is read.
        cash_direction = key in CASH_DIRECTION_KEYS
        if not cash_direction:
            cash_direction = (
                normalize_action_token(activity_type),
                normalize_action_token(sub_type) if sub_type else None,
            ) in CASH_DIRECTION_KEYS_NORM
        if cash_direction and not cash_transfer:
            action = cash_row_direction(description, net_cash)

        # M132 cash-reimbursement rows (docs/02 §trades): a
        # POSITIVE `TRANSFER_FEE_REIMBURSEMENT` is a CREDIT — "CAD credited" on
        # real account 731, +145.75 — money INTO the account, so it cannot wear
        # `fee` (whose docs/05 cash effect is `-abs`, ALWAYS money out). It
        # becomes a CASH `transfer_in`: quantity 0, no symbol required, effect
        # `+abs(price_native)`. A NEGATIVE reimbursement is money out and keeps
        # the `fee` mapping ACTIVITY_MAP chose.
        reimbursement = (
            key in REIMBURSEMENT_KEYS
            or (
                normalize_action_token(activity_type),
                normalize_action_token(sub_type) if sub_type else None,
            )
            in REIMBURSEMENT_KEYS_NORM
        )
        if reimbursement and net_cash > 0:
            action = TradeAction.transfer_in
            cash_transfer = True

        # Apply sign convention (docs/02): buy/drip/transfer_in positive,
        # sell/transfer_out negative.
        if action in (TradeAction.sell, TradeAction.transfer_out):
            quantity = -abs(quantity)
        elif action in (TradeAction.buy, TradeAction.drip, TradeAction.transfer_in):
            quantity = abs(quantity)

        if abs_magnitudes:
            commission = abs(commission)

        # A cash-transfer row's file quantity is its cash amount, not shares —
        # requiring shares for it would refuse every one of them (M113).
        if (
            require_quantity
            and not cash_transfer
            and action in REQUIRE_QUANTITY_ACTIONS
            and quantity == 0
        ):
            unmapped.append(
                skip(
                    line_num,
                    activity_type,
                    sub_type,
                    symbol,
                    date_str or None,
                    f"Missing quantity for {action.value}",
                )
            )
            continue

        # Cash-only actions and WS cash transfers: quantity=0 and price_native
        # carries the row's cash amount. The three cases store DIFFERENTLY (M131):
        #
        # * a cash TRANSFER keeps the magnitude — M113 decided its direction
        #   from net_cash's sign above, so the action alone is the sign;
        # * a cash OUTFLOW (fee/withdrawal) is stored NEGATIVE, the native sign
        #   of money leaving. Its cash effect is `-abs(price_native)` (docs/05),
        #   so the ledger is unchanged while the transactions surface, exports
        #   and the event log stop showing an inflationary `+$145.75 fee`;
        # * a cash INFLOW (dividend/roc/contribution) keeps the file's sign —
        #   those are money in and their effect is `+price_native`.
        #   `abs_magnitudes` still normalises a TRADE's commission; it no longer
        #   rewrites a cash row's amount (docs/41 § sign conventions).
        if action in CASH_ONLY_ACTIONS or cash_transfer:
            quantity = Decimal("0")
            if cash_transfer:
                unit_price = abs(net_cash)
            elif action in CASH_OUTFLOW_ACTIONS:
                unit_price = -abs(net_cash)
            else:
                unit_price = net_cash
            commission = Decimal("0")
            # M131: a contribution is money IN. A negative amount is a
            # contradiction — either a withdrawal the description failed to say
            # so, or a sign the exporter got wrong. Storing it would poison
            # contribution-room math and every semantic reader while the CASH
            # total survives by accident (docs/03). Refuse the row (422
            # `negative_contribution`) instead of importing it as a lie.
            if action == TradeAction.contribution and unit_price < 0:
                unmapped.append(
                    skip(
                        line_num,
                        activity_type,
                        sub_type,
                        symbol,
                        date_str or None,
                        f"Contribution amount {net_cash} is negative — a contribution is "
                        "money in; export the row as a withdrawal or fix the sign",
                        code="negative_contribution",
                    )
                )
                continue
        elif action in (TradeAction.transfer_in, TradeAction.transfer_out):
            # Share transfer: requires a non-empty symbol so a Security can be
            # created; price_native carries the PER-SHARE cost basis (falling
            # back to net_cash / quantity when only a total is supplied) so that
            # quantity * price_native equals the transferred cost in the ACB
            # engine. commission is always zero for transfers (docs/02).
            if symbol == "":
                unmapped.append(
                    skip(
                        line_num,
                        activity_type,
                        sub_type,
                        symbol,
                        date_str or None,
                        f"Missing symbol for {action.value}",
                    )
                )
                continue
            commission = Decimal("0")
            if unit_price == 0 and net_cash != 0 and abs(quantity) != 0:
                unit_price = net_cash / abs(quantity)
        elif symbol == "":
            # Non-cash action with no symbol cannot create a meaningful Security.
            unmapped.append(
                skip(
                    line_num,
                    activity_type,
                    sub_type,
                    symbol,
                    date_str or None,
                    f"Missing symbol for {action.value}",
                )
            )
            continue

        if security_name == "":
            security_name = symbol

        account_id_source = _cell(row, "account_id")

        trades.append(
            RawTrade(
                account_id_source=account_id_source,
                action=action.value,
                date=date,
                symbol=symbol,
                security_name=security_name,
                currency=currency,
                quantity=quantity,
                price_native=unit_price,
                commission=commission,
                net_cash=net_cash,
                description=description,
            )
        )

    return trades, unmapped
