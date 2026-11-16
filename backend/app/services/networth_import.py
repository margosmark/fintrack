"""Net-worth file import — ProjectionLab JSON + balance spreadsheets (docs/58 §B).

Preview-first, like every import path here:

* ``parse_networth_file`` — detect the file kind (ProjectionLab JSON export
  vs. a balance CSV) and turn it into balance rows plus the complete
  ``skipped`` list. Raises :class:`NetWorthImportError` (mapped to a
  ``{detail, code}`` envelope by the router) — never a bare 500.
* ``build_plan`` — map rows onto the tenant's accounts: ``create`` (new
  account, ``starting_balance`` = the file's latest balance) or ``reuse``
  (same name already exists). Writes NOTHING.
* ``apply_plan`` — create the ``create`` entries in the caller's transaction.

Manual data wins: a reused account's balance, type and currency are never
touched. The plan reports ``file_balance`` next to ``current_balance`` so the
reviewer sees the difference instead of it being applied silently.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as dt_date
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlmodel import Session, select

from app.importers.base import decode_raw
from app.importers.format_presets import infer_date_format
from app.models.accounts import Account, AccountType
from app.services.txn_filters import committed_tx_rows

MAX_NETWORTH_ROWS = 1000
_ZERO = Decimal("0")

# The 3-letter ISO contract every other account write enforces
# (schemas/accounts.py `AccountCreate.currency`, api/imports.py
# `_validate_networth_form`) — a row's own `currency` cell must satisfy the
# same rule (docs/58 §B), or it would 422 every fx lookup and the validated
# PATCH path could never repair it (security S-1).
_ROW_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")

# ProjectionLab's export: top-level {today, progress, plans, meta}.
_PL_ACCOUNT_KEYS = ("savingsAccounts", "investmentAccounts", "debts")


class NetWorthImportError(Exception):
    """A file the importer refuses, with the envelope's code and status."""

    def __init__(self, detail: str, code: str, status: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status = status


@dataclass
class BalanceRow:
    """One account's balance as the file states it."""

    account: str
    balance: Decimal
    index: int = 0  # 1-based source row, for messages
    date: dt_date | None = None
    currency: str | None = None
    kind: str = "csv"  # csv | savings | investment | debt
    source_type: str | None = None  # file's own type/subtype token


@dataclass
class Prepared:
    """A parsed file, ready to plan against."""

    source_format: str  # projectionlab_json | balance_csv
    file_name: str
    rows: list[BalanceRow] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)  # kind/name/reason
    progress_snapshots: int = 0  # snapshots seen and NOT imported (docs/58 §B)


@dataclass
class _Entry:
    """One planned account — what ``apply_plan`` acts on."""

    name: str
    action: str  # create | reuse | unmapped
    mapped_type: AccountType | None
    currency: str
    file_balance: Decimal
    source_type: str | None = None
    existing_id: int | None = None
    current_balance: Decimal | None = None
    reason: str | None = None
    notes: list[str] = field(default_factory=list)


# ── number / token helpers ─────────────────────────────────────────────────


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        text = value.strip().replace("\xa0", "").replace("$", "").replace(" ", "")
        if not text:
            return None
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None
        return -abs(amount) if negative else amount
    return None


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


# AccountType tokens the CSV `Type` column may carry (normalized: alnum only).
_CSV_TYPE_TOKENS: dict[str, AccountType] = {
    _norm("chequing"): AccountType.chequing,
    _norm("checking"): AccountType.chequing,
    _norm("current"): AccountType.chequing,
    _norm("savings"): AccountType.savings,
    _norm("credit card"): AccountType.credit_card,
    _norm("creditcard"): AccountType.credit_card,
    _norm("cc"): AccountType.credit_card,
    _norm("line of credit"): AccountType.line_of_credit,
    _norm("lineofcredit"): AccountType.line_of_credit,
    _norm("loc"): AccountType.line_of_credit,
    _norm("loan"): AccountType.line_of_credit,
    _norm("mortgage"): AccountType.line_of_credit,
    _norm("brokerage"): AccountType.brokerage_non_registered,
    _norm("investment"): AccountType.brokerage_non_registered,
    _norm("non-registered"): AccountType.brokerage_non_registered,
    _norm("taxable"): AccountType.brokerage_non_registered,
    _norm("tfsa"): AccountType.tfsa,
    _norm("rrsp"): AccountType.rrsp,
    _norm("resp"): AccountType.resp,
    _norm("fhsa"): AccountType.fhsa,
    _norm("lira"): AccountType.lira,
    _norm("rdsp"): AccountType.rdsp,
}

# ProjectionLab investmentAccounts.type -> fin_tracker (docs/58 §B).
_PL_INVESTMENT_TYPES: dict[str, AccountType] = {
    _norm("taxable"): AccountType.brokerage_non_registered,
    _norm("401k"): AccountType.rrsp,
    _norm("traditional-ira"): AccountType.rrsp,
    _norm("roth-ira"): AccountType.tfsa,
    _norm("529"): AccountType.resp,
    # No Canadian equivalent for a US HSA: imported as a taxable account so no
    # gain is hidden, with a note (docs/58 §B).
    _norm("hsa"): AccountType.brokerage_non_registered,
}
_PL_NOTE_TYPES = {_norm("hsa")}


# ── parsing ────────────────────────────────────────────────────────────────


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _require_name_balance(
    name: str | None, balance: Decimal | None, skipped: list[dict[str, str]], label: str
) -> bool:
    """Push an unusable entry onto ``skipped`` (never silent) and say so."""
    if not name:
        skipped.append(
            {
                "kind": "account",
                "name": "(unnamed)",
                "reason": f"{label} entry has no name in the file (docs/58 §B)",
            }
        )
        return False
    if balance is None:
        skipped.append(
            {
                "kind": "account",
                "name": name,
                "reason": f"{label} entry carries no balance in the file (docs/58 §B)",
            }
        )
        return False
    return True


def _parse_projectionlab(raw: bytes, file_name: str) -> Prepared:
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetWorthImportError(
            f"File is not valid JSON: {exc}", "unsupported_networth_format"
        ) from None
    if not isinstance(doc, dict):
        raise NetWorthImportError(
            "Expected a ProjectionLab export object", "unsupported_networth_format"
        )
    today = doc.get("today")
    if not isinstance(today, dict) or not any(
        isinstance(today.get(key), list) for key in _PL_ACCOUNT_KEYS
    ):
        raise NetWorthImportError(
            "Expected a ProjectionLab export with today.savingsAccounts / "
            "today.investmentAccounts / today.debts",
            "unsupported_networth_format",
        )

    prepared = Prepared(source_format="projectionlab_json", file_name=file_name)
    skipped = prepared.skipped
    index = 0

    def add(kind: str, name: str | None, balance: Decimal | None, source_type: str | None) -> None:
        nonlocal index
        index += 1
        if _require_name_balance(name, balance, skipped, "account"):
            prepared.rows.append(
                BalanceRow(
                    account=name,  # type: ignore[arg-type]
                    balance=balance,  # type: ignore[arg-type]
                    index=index,
                    kind=kind,
                    source_type=source_type,
                )
            )

    for item in _items(today.get("savingsAccounts")):
        balance = _decimal(item.get("balance"))
        if balance is None:
            balance = _decimal(item.get("initialValue"))
        add("savings", _text(item.get("name")), balance, _text(item.get("type")))

    for item in _items(today.get("investmentAccounts")):
        balance = _decimal(item.get("balance"))
        if balance is None:
            balance = _decimal(item.get("initialValue"))
        add("investment", _text(item.get("name")), balance, _text(item.get("type")))

    for item in _items(today.get("debts")):
        amount = _decimal(item.get("amount"))
        balance = -abs(amount) if amount is not None else None
        add("debt", _text(item.get("name")), balance, _text(item.get("subtype")))

    for item in _items(today.get("assets")):
        name = _text(item.get("name")) or "(unnamed asset)"
        skipped.append(
            {
                "kind": "asset",
                "name": name,
                "reason": (
                    "real-estate/vehicle asset: fin_tracker has no asset account "
                    "type, and an asset's balance is its LOAN balance when "
                    "financed — not written (docs/58 §B)"
                ),
            }
        )

    progress = _items(doc.get("progress")) or _items(today.get("progress"))
    prepared.progress_snapshots = len(progress)
    if progress:
        skipped.append(
            {
                "kind": "progress_history",
                "name": f"{len(progress)} net-worth snapshot(s)",
                "reason": (
                    "dated net-worth history: fin_tracker stores one balance per "
                    "account, so only the latest is imported (docs/58 §B)"
                ),
            }
        )

    plans = _items(doc.get("plans"))
    if plans:
        skipped.append(
            {
                "kind": "plan_data",
                "name": f"{len(plans)} plan entr{'y' if len(plans) == 1 else 'ies'}",
                "reason": "income/expense/milestone planning data, not balances (docs/58 §B)",
            }
        )

    if not prepared.rows and not skipped:
        raise NetWorthImportError(
            "ProjectionLab export contains no accounts", "networth_no_accounts"
        )
    return prepared


def _parse_balance_text(text: str, line_number: int) -> Decimal:
    amount = _decimal(text)
    if amount is None:
        raise NetWorthImportError(
            f"row {line_number}: unparseable balance {text!r}", "networth_row_invalid"
        )
    return amount


def _csv_columns(headers: list[str]) -> dict[str, str | None]:
    """Resolve the file's own header names to the fields we read (docs/58 §B)."""
    index: dict[str, str] = {}
    for header in headers:
        key = re.sub(r"\s+", " ", (header or "").strip().lower())
        index.setdefault(key, header)

    def pick(candidates: tuple[str, ...]) -> str | None:
        for candidate in candidates:
            if candidate in index:
                return index[candidate]
        return None

    return {
        "account": pick(("account", "account name", "name")),
        "balance": pick(("balance", "value", "amount", "net worth", "total", "current balance")),
        "date": pick(("date", "as of", "as of date", "posting date", "updated")),
        "type": pick(("type", "category", "kind")),
        "currency": pick(("currency", "ccy")),
    }


def _row_currency(cell: Any, line_number: int) -> str | None:
    """One balance row's own ``currency`` cell, held to the 3-letter rule.

    Normalized exactly like the other write paths (stripped, upper-cased);
    an empty cell means "no opinion" and the plan falls back to the form's
    validated default. Any other value is REFUSED here rather than persisted
    (``422 networth_row_invalid``) — both preview and commit parse the file,
    so one check covers both (docs/58 §B, security S-1).
    """
    value = str(cell or "").strip().upper()
    if not value:
        return None
    if not _ROW_CURRENCY_RE.match(value):
        raise NetWorthImportError(
            f"row {line_number}: currency {value!r} is not a 3-letter ISO code",
            "networth_row_invalid",
        )
    return value


def _parse_balance_csv(raw: bytes, file_name: str, account_name: str | None) -> Prepared:
    text = decode_raw(raw)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise NetWorthImportError("empty CSV: no header row", "networth_missing_columns")
    columns = _csv_columns(list(reader.fieldnames))
    looked_for = "balance, value, amount, net worth, total"
    if columns["balance"] is None:
        raise NetWorthImportError(
            f"No balance column found (looked for: {looked_for})",
            "networth_missing_columns",
        )
    if columns["account"] is None and not account_name:
        raise NetWorthImportError(
            "No account column found (looked for: account, account name, name) "
            "and no account_name was given",
            "networth_missing_columns",
        )

    prepared = Prepared(source_format="balance_csv", file_name=file_name)
    rows: list[BalanceRow] = []
    raw_dates: list[tuple[BalanceRow, str]] = []
    for line_number, row in enumerate(reader, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            continue
        if len(rows) >= MAX_NETWORTH_ROWS:
            raise NetWorthImportError(
                f"Balance files are capped at {MAX_NETWORTH_ROWS} rows",
                "networth_row_cap",
                status=413,
            )
        name = (str(row.get(columns["account"]) or "").strip() if columns["account"] else "") or (
            (account_name or "").strip()
        )
        raw_balance = str(row.get(columns["balance"]) or "")
        balance = _parse_balance_text(raw_balance, line_number)

        entry_row = BalanceRow(
            account=name,
            balance=balance,
            index=line_number,
            currency=_row_currency(row.get(columns["currency"]), line_number)
            if columns["currency"]
            else None,
            source_type=(str(row.get(columns["type"]) or "").strip() or None)
            if columns["type"]
            else None,
        )
        if columns["date"]:
            raw_date = str(row.get(columns["date"]) or "").strip()
            if raw_date:
                raw_dates.append((entry_row, raw_date))
        rows.append(entry_row)

    # One date format for the whole file, inferred from its samples (docs/58).
    if raw_dates:
        samples = [text for _, text in raw_dates]
        fmt = infer_date_format(samples)
        if fmt is None:
            raise NetWorthImportError(
                f"Could not determine the date format from {samples[0]!r}",
                "networth_row_invalid",
            )
        for entry_row, raw_date in raw_dates:
            try:
                entry_row.date = datetime.strptime(raw_date, fmt).date()
            except ValueError:
                raise NetWorthImportError(
                    f"row {entry_row.index}: date {raw_date!r} does not match {fmt!r}",
                    "networth_row_invalid",
                ) from None

    prepared.rows = rows
    if not rows:
        raise NetWorthImportError("CSV contains no balance rows", "networth_no_accounts")
    return prepared


def parse_networth_file(
    raw: bytes,
    *,
    file_name: str,
    account_name: str | None = None,
) -> Prepared:
    """Detect the file kind and parse it (docs/58 §B detection rules)."""
    head = raw[:8].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"{"):
        return _parse_projectionlab(raw, file_name)
    return _parse_balance_csv(raw, file_name, account_name)


# ── planning ───────────────────────────────────────────────────────────────


def _map_type(entry: BalanceRow) -> tuple[AccountType | None, str | None]:
    """The account type for one row, plus a note when it was defaulted."""
    if entry.kind == "debt":
        token = _norm(entry.source_type or "")
        if token == _norm("credit-card"):
            return AccountType.credit_card, None
        return AccountType.line_of_credit, None

    if entry.kind == "investment":
        token = _norm(entry.source_type or "")
        if token in _PL_INVESTMENT_TYPES:
            note = (
                "type imported as a taxable account: no Canadian equivalent for "
                f"`{entry.source_type}` (docs/58 §B)"
                if token in _PL_NOTE_TYPES
                else None
            )
            return _PL_INVESTMENT_TYPES[token], note
        return (
            AccountType.brokerage_non_registered,
            f"investment type `{entry.source_type}` has no fin_tracker "
            "equivalent: imported as a taxable account (docs/58 §B)",
        )

    if entry.kind == "savings":
        token = _norm(entry.source_type or "")
        if token in _CSV_TYPE_TOKENS:
            return _CSV_TYPE_TOKENS[token], None
        if entry.source_type:
            return AccountType.savings, (
                f"account type `{entry.source_type}` defaulted to savings (docs/58 §B)"
            )
        return AccountType.savings, None

    # Generic CSV: a `Type` column wins when we recognise it, else the sign
    # rule (a negative balance is a liability, a positive one is cash).
    token = _norm(entry.source_type or "")
    if token in _CSV_TYPE_TOKENS:
        return _CSV_TYPE_TOKENS[token], None
    if entry.source_type:
        return (
            AccountType.line_of_credit if entry.balance < 0 else AccountType.savings,
            f"type `{entry.source_type}` not recognised: inferred from the "
            "balance sign (docs/58 §B)",
        )
    return (AccountType.line_of_credit if entry.balance < 0 else AccountType.savings), None


def build_plan(
    session: Session, user_id: int, prepared: Prepared, default_currency: str
) -> tuple[dict[str, Any], list[_Entry]]:
    """Map the parsed file onto this tenant's accounts. Writes nothing."""
    # Defense in depth (security S-1): parse_networth_file already refuses a
    # junk row currency, but every row is re-checked here too so no caller can
    # hand build_plan a Prepared that skips the parse step — preview and
    # commit both land in this function before anything is written (docs/58 §B).
    for row in prepared.rows:
        row.currency = _row_currency(row.currency, row.index)

    existing = {
        account.name.strip().lower(): account
        for account in session.exec(select(Account).where(Account.user_id == user_id)).all()
    }
    committed: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    for txn in committed_tx_rows(session, user_id):
        if txn.account_id is not None:
            committed[txn.account_id] += txn.amount_native or _ZERO

    skipped = list(prepared.skipped)
    groups: dict[str, list[BalanceRow]] = {}
    order: list[str] = []
    for row in prepared.rows:
        key = row.account.strip().lower()
        if not key:
            # Never silent: a row with no account name is reported, not dropped.
            skipped.append(
                {
                    "kind": "account",
                    "name": f"row {row.index}",
                    "reason": "no account name in this row (docs/58 §B)",
                }
            )
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    entries: list[_Entry] = []
    warnings: list[dict[str, str]] = []
    collapsed = 0

    for key in order:
        rows = groups[key]
        winner = max(rows, key=lambda r: (r.date is not None, r.date or dt_date.min))
        winner_date = winner.date or dt_date.min
        if len(rows) > 1:
            collapsed += len(rows) - 1
            when = winner.date.isoformat() if winner.date else "the undated row"
            conflict = any(
                r is not winner
                and r.balance != winner.balance
                and (r.date or dt_date.min) == winner_date
                for r in rows
            )
            if conflict:
                warnings.append(
                    {
                        "code": "conflicting_balance",
                        "detail": (
                            f"{winner.account}: two rows carry different balances "
                            f"for the same date; the first was kept — fix the file "
                            "and re-import if that is wrong"
                        ),
                    }
                )

        mapped, note = _map_type(winner)
        entry = _Entry(
            name=winner.account.strip(),
            action="create",
            mapped_type=mapped,
            currency=winner.currency or default_currency,
            file_balance=winner.balance,
            source_type=winner.source_type,
        )
        if note:
            entry.notes.append(note)
        if len(rows) > 1:
            when = winner.date.isoformat() if winner.date else "the undated row"
            entry.notes.append(f"{len(rows) - 1} older balance(s) collapsed; imported {when}")

        match = existing.get(entry.name.lower())
        if match is not None:
            entry.action = "reuse"
            entry.existing_id = match.id
            entry.currency = match.currency
            entry.current_balance = (match.starting_balance or _ZERO) + committed.get(
                match.id, _ZERO
            )
            entry.reason = "An account with this name already exists — reused, not duplicated"
            if entry.current_balance != entry.file_balance:
                entry.notes.append(
                    "file balance differs from the account's current balance; "
                    "the existing balance was kept (manual data wins)"
                )
            if winner.currency and winner.currency != match.currency:
                entry.notes.append(
                    f"currency mismatch: file {winner.currency}, account "
                    f"{match.currency} — the account's currency was kept"
                )
        entries.append(entry)

    counts = {
        "accounts": len(entries),
        "accounts_create": sum(1 for e in entries if e.action == "create"),
        "accounts_reuse": sum(1 for e in entries if e.action == "reuse"),
        "accounts_unmapped": sum(1 for e in entries if e.action == "unmapped"),
        "balance_rows_collapsed": collapsed,
        "progress_snapshots_skipped": prepared.progress_snapshots,
        "assets_skipped": sum(1 for s in skipped if s["kind"] == "asset"),
    }

    accounts_plan = []
    for entry in entries:
        accounts_plan.append(
            {
                "name": entry.name,
                "source_type": entry.source_type,
                "mapped_type": entry.mapped_type.value if entry.mapped_type else None,
                "action": entry.action,
                "existing_id": entry.existing_id,
                "file_balance": str(entry.file_balance),
                "current_balance": str(entry.current_balance)
                if entry.current_balance is not None
                else None,
                "currency": entry.currency,
                "reason": entry.reason,
                "notes": entry.notes,
            }
        )

    plan = {
        "source_format": prepared.source_format,
        "file_name": prepared.file_name,
        "currency": default_currency,
        "counts": counts,
        "accounts": accounts_plan,
        "skipped": skipped,
        "warnings": warnings,
        "can_commit": counts["accounts_create"] + counts["accounts_reuse"] > 0,
    }
    return plan, entries


def apply_plan(session: Session, user_id: int, entries: list[_Entry]) -> dict[str, int]:
    """Create the ``create`` entries in the caller's transaction (one commit).

    ``reuse`` and ``unmapped`` entries are deliberately no-ops: an existing
    account is never overwritten by a file (merge review, docs/58 §B).
    """
    created = 0
    for entry in entries:
        if entry.action != "create" or entry.mapped_type is None:
            continue
        session.add(
            Account(
                user_id=user_id,
                name=entry.name,
                type=entry.mapped_type,
                currency=entry.currency,
                # Taxability is server-derived (docs/14 C1): never from a file.
                taxable=entry.mapped_type == AccountType.brokerage_non_registered,
                starting_balance=entry.file_balance,
            )
        )
        created += 1
    session.commit()
    return {"created": created, "reused": sum(1 for e in entries if e.action == "reuse")}
