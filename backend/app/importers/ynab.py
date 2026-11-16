"""YNAB budget export parsers: JSON plan export + register CSV (docs/38).

Pure functions over bytes — no DB, no settings (the DB-aware mapping lives in
``app/services/ynab_migration.py``).

Contract (docs/38):

* **JSON** — YNAB's budget/plan payload. Accepted wrappers:
  ``{"data": {"plan": {...}}}`` (API v1.78+ renamed ``budget`` → ``plan``),
  ``{"budget": {...}}`` (older exports and third-party tools), or the bare
  budget object. Arrays consumed: ``accounts``, ``category_groups``,
  ``categories``, ``payees``, ``transactions``, ``subtransactions``.
  Amounts are **milliunits** (1/1000 of a major unit), signed with inflow
  positive / outflow negative — fin_tracker's own sign convention, so the
  conversion is a plain ``Decimal(milli) / 1000``.
* **CSV** — YNAB's per-account register export (``Account, Flag, Check Number,
  Date, Payee, Category, Master Category, Sub Category, Memo, Outflow, Inflow,
  Cleared, Running Balance`` in the YNAB 4 export; later exports drop
  ``Master Category``/``Sub Category`` and keep a combined ``Category``).
  Headers are matched case-insensitively with any ``(...)`` suffix stripped
  (``Outflow (CAD)`` → ``outflow``).

Rows that cannot become a transaction are returned in
``YnabBudget.unmappable`` with a stable code and a human reason — never
dropped silently (AGENTS §3: reject or flag, never skip).
"""

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date as dt_date
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.importers.base import decode_raw

MILLIUNIT = Decimal("1000")

# Stable machine codes for rows that could not be normalized at parse time.
CODE_INVALID_DATE = "invalid_date"
CODE_INVALID_AMOUNT = "invalid_amount"

CLEARED_VALUES = frozenset({"uncleared", "cleared", "reconciled"})


class YnabParseError(ValueError):
    """Malformed YNAB input; mapped to the 422 ``ynab_parse_error`` envelope."""


@dataclass(frozen=True)
class YnabAccount:
    ynab_id: str
    name: str
    ynab_type: str
    on_budget: bool = True
    closed: bool = False
    deleted: bool = False
    type_assumed: bool = False  # a CSV register carries no account type


@dataclass(frozen=True)
class YnabGroup:
    ynab_id: str
    name: str
    hidden: bool = False
    deleted: bool = False


@dataclass(frozen=True)
class YnabCategory:
    ynab_id: str
    name: str
    group_id: str | None = None
    deleted: bool = False
    goal_type: str | None = None
    goal_target: int | None = None  # milliunits
    goal_target_month: str | None = None  # YYYY-MM-DD


@dataclass(frozen=True)
class YnabPayee:
    ynab_id: str
    name: str
    deleted: bool = False


@dataclass(frozen=True)
class YnabTxn:
    """One transaction row, source position included for issue reporting."""

    index: int  # 1-based position among the source's transaction rows
    date: dt_date
    amount: Decimal  # signed major units (YNAB milliunits / 1000)
    raw: str  # short source snippet, shown with any issue about this row
    ynab_id: str | None = None  # YNAB transaction id (transfer pairing)
    account_ref: str | None = None  # YNAB account id, or synthetic CSV ref
    payee_ref: str | None = None
    category_ref: str | None = None
    memo: str | None = None
    cleared: str | None = None
    transfer_account_ref: str | None = None
    transfer_txn_ref: str | None = None  # counterpart transaction id (JSON)
    split_count: int = 0  # live subtransactions attached to this row
    cleared_issue: bool = False  # a cleared value was present but unreadable


@dataclass(frozen=True)
class YnabIssue:
    """A row (or the whole file, when ``index`` is None) that needs a decision."""

    code: str
    reason: str
    index: int | None = None
    detail: str | None = None


@dataclass
class YnabBudget:
    source_format: str  # json | csv
    name: str
    currency: str | None = None
    accounts: list[YnabAccount] = field(default_factory=list)
    groups: list[YnabGroup] = field(default_factory=list)
    categories: list[YnabCategory] = field(default_factory=list)
    payees: list[YnabPayee] = field(default_factory=list)
    transactions: list[YnabTxn] = field(default_factory=list)
    # Rows rejected while normalizing (bad date/amount) + file-level notes.
    unmappable: list[YnabIssue] = field(default_factory=list)
    notes: list[YnabIssue] = field(default_factory=list)
    date_format: str | None = None  # CSV only: the format actually applied
    source_rows: int = 0  # transaction rows read from the file


def looks_like_json(raw: bytes) -> bool:
    return raw.lstrip()[:1] == b"{"


def parse_ynab(
    raw: bytes,
    *,
    date_format: str | None = None,
    account_type: str | None = None,
    currency: str | None = None,
) -> YnabBudget:
    """Parse a YNAB export. Raises ``YnabParseError`` for anything else."""
    if looks_like_json(raw):
        return _parse_json(raw)
    return _parse_csv(raw, date_format=date_format, account_type=account_type, currency=currency)


# --------------------------------------------------------------------------- JSON


def _unwrap(payload: object) -> dict | None:
    """Drill ``data`` → ``plan``/``budget`` wrappers down to the budget object."""
    node = payload
    for _ in range(3):
        if not isinstance(node, dict):
            return None
        if isinstance(node.get("data"), dict):
            node = node["data"]
            continue
        inner = node.get("plan") if isinstance(node.get("plan"), dict) else node.get("budget")
        if isinstance(inner, dict):
            node = inner
            continue
        break
    if isinstance(node, dict) and any(
        k in node for k in ("accounts", "transactions", "categories")
    ):
        return node
    return None


def _as_int(value: object) -> int | None:
    """YNAB milliunits: an int, or a float that is exactly integral."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    return None


def _parse_json(raw: bytes) -> YnabBudget:
    try:
        payload = json.loads(decode_raw(raw))
    except json.JSONDecodeError as exc:
        raise YnabParseError(f"File is not valid JSON: {exc.msg} at line {exc.lineno}") from exc
    budget_node = _unwrap(payload)
    if budget_node is None:
        raise YnabParseError(
            "JSON does not look like a YNAB budget export — expected a `plan`/`budget` "
            "object carrying `accounts`, `categories` or `transactions`"
        )

    fmt = budget_node.get("date_format")
    currency_format = budget_node.get("currency_format")
    currency_code = None
    if isinstance(currency_format, dict):
        iso = currency_format.get("iso_code")
        if isinstance(iso, str) and iso:
            currency_code = iso.upper()

    accounts: list[YnabAccount] = []
    for i, item in enumerate(_as_list(budget_node.get("accounts"), "accounts")):
        account_id = _req_str(item, "id", i, "accounts")
        accounts.append(
            YnabAccount(
                ynab_id=account_id,
                name=str(item.get("name") or "").strip(),
                ynab_type=str(item.get("type") or "").strip(),
                on_budget=bool(item.get("on_budget", True)),
                closed=bool(item.get("closed", False)),
                deleted=bool(item.get("deleted", False)),
            )
        )

    groups: list[YnabGroup] = []
    for i, item in enumerate(_as_list(budget_node.get("category_groups"), "category_groups")):
        groups.append(
            YnabGroup(
                ynab_id=_req_str(item, "id", i, "category_groups"),
                name=str(item.get("name") or "").strip(),
                hidden=bool(item.get("hidden", False)),
                deleted=bool(item.get("deleted", False)),
            )
        )

    categories: list[YnabCategory] = []
    for i, item in enumerate(_as_list(budget_node.get("categories"), "categories")):
        goal_target = item.get("goal_target")
        categories.append(
            YnabCategory(
                ynab_id=_req_str(item, "id", i, "categories"),
                name=str(item.get("name") or "").strip(),
                group_id=item.get("category_group_id") or None,
                deleted=bool(item.get("deleted", False)),
                goal_type=item.get("goal_type") or None,
                goal_target=_as_int(goal_target) if goal_target is not None else None,
                goal_target_month=item.get("goal_target_month") or None,
            )
        )

    payees: list[YnabPayee] = []
    for i, item in enumerate(_as_list(budget_node.get("payees"), "payees")):
        payees.append(
            YnabPayee(
                ynab_id=_req_str(item, "id", i, "payees"),
                name=str(item.get("name") or "").strip(),
                deleted=bool(item.get("deleted", False)),
            )
        )

    # Live subtransaction counts drive the "split detail dropped" warning.
    split_counts: dict[str, int] = {}
    for item in _as_list(budget_node.get("subtransactions"), "subtransactions"):
        if item.get("deleted"):
            continue
        parent = item.get("transaction_id")
        if isinstance(parent, str) and parent:
            split_counts[parent] = split_counts.get(parent, 0) + 1

    transactions: list[YnabTxn] = []
    unmappable: list[YnabIssue] = []
    skipped_deleted = 0
    rows = _as_list(budget_node.get("transactions"), "transactions")
    for position, item in enumerate(rows, start=1):
        if item.get("deleted"):
            skipped_deleted += 1
            continue
        raw_snippet = _snippet(item)
        txn_id = item.get("id") if isinstance(item.get("id"), str) else None
        when, issue = _json_date(item.get("date"), position, raw_snippet)
        if issue is not None:
            unmappable.append(issue)
            continue
        amount = _as_int(item.get("amount"))
        if amount is None:
            unmappable.append(
                YnabIssue(
                    code=CODE_INVALID_AMOUNT,
                    index=position,
                    reason=(
                        f"Amount `{item.get('amount')!r}` is missing or is not a whole number "
                        "of milliunits"
                    ),
                    detail=raw_snippet,
                )
            )
            continue
        transactions.append(
            YnabTxn(
                index=position,
                date=when,
                amount=Decimal(amount) / MILLIUNIT,
                raw=raw_snippet,
                ynab_id=txn_id,
                account_ref=item.get("account_id") or None,
                payee_ref=item.get("payee_id") or None,
                category_ref=item.get("category_id") or None,
                memo=item.get("memo") or None,
                cleared=_cleared(item.get("cleared")),
                cleared_issue="cleared" in item
                and item.get("cleared") is not None
                and _cleared(item.get("cleared")) is None,
                transfer_account_ref=item.get("transfer_account_id") or None,
                transfer_txn_ref=item.get("transfer_transaction_id") or None,
                split_count=split_counts.get(txn_id or "", 0),
            )
        )

    notes: list[YnabIssue] = []
    if skipped_deleted:
        notes.append(
            YnabIssue(
                code="deleted_rows_skipped",
                reason=(
                    f"{skipped_deleted} transaction(s) marked deleted in YNAB were excluded — "
                    "YNAB deletes are real deletions, not hidden rows"
                ),
            )
        )
    scheduled = [
        item
        for item in _as_list(budget_node.get("scheduled_transactions"), "scheduled_transactions")
        if not item.get("deleted")
    ]
    if scheduled:
        notes.append(
            YnabIssue(
                code="scheduled_skipped",
                reason=(
                    f"{len(scheduled)} scheduled/future transaction(s) were not imported — "
                    "fin_tracker has no scheduled-transaction concept (docs/38 gaps table)"
                ),
            )
        )

    return YnabBudget(
        source_format="json",
        name=str(budget_node.get("budget_name") or budget_node.get("name") or "YNAB budget"),
        currency=currency_code,
        accounts=accounts,
        groups=groups,
        categories=categories,
        payees=payees,
        transactions=transactions,
        unmappable=unmappable,
        notes=notes,
        date_format=str(fmt) if isinstance(fmt, str) else None,
        source_rows=len(rows),
    )


def _json_date(value: object, index: int, raw: str) -> tuple[dt_date, YnabIssue | None]:
    if not isinstance(value, str) or not value.strip():
        return dt_date.min, YnabIssue(
            code=CODE_INVALID_DATE,
            index=index,
            reason="Date is missing (YNAB exports dates as YYYY-MM-DD)",
            detail=raw,
        )
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date(), None
    except ValueError:
        return dt_date.min, YnabIssue(
            code=CODE_INVALID_DATE,
            index=index,
            reason=f"Date `{value}` is not YYYY-MM-DD",
            detail=raw,
        )


def _cleared(value: object) -> str | None:
    if isinstance(value, str) and value.strip().lower() in CLEARED_VALUES:
        return value.strip().lower()
    return None


def _as_list(value: object, key: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise YnabParseError(f"`{key}` must be a list")
    return [item for item in value if isinstance(item, dict)]


def _req_str(item: dict, key: str, index: int, collection: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise YnabParseError(f"{collection}[{index}] is missing `{key}`")
    return value


def _snippet(item: dict, limit: int = 200) -> str:
    keep = {
        k: item.get(k) for k in ("date", "amount", "payee_id", "account_id", "memo") if k in item
    }
    try:
        text = json.dumps(keep, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        text = str(keep)
    return text[:limit]


# ---------------------------------------------------------------------------- CSV

# Header aliases → canonical field. Compared after lowercasing and stripping a
# trailing "(...)" suffix, so "Outflow (CAD)" matches "outflow".
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "transaction date", "trans. date"),
    "payee": ("payee", "payee name", "description"),
    "memo": ("memo", "notes", "note"),
    "outflow": ("outflow", "debit", "withdrawal"),
    "inflow": ("inflow", "credit", "deposit"),
    "account": ("account", "account name"),
    # `subcategory` wins over `category` when a YNAB 4 export ships both.
    "subcategory": ("sub category", "subcategory", "sub-category"),
    "category": ("category",),
    "group": ("master category", "category group", "group"),
    "cleared": ("cleared", "cleared status", "status"),
    "flag": ("flag", "flag colour", "flag color"),
    "balance": ("running balance", "balance"),
    "check_number": ("check number", "check #"),
}

_REQUIRED_COLUMNS = ("date",)


def _norm_header(header: str) -> str:
    return re.sub(r"\s*\(.*?\)\s*$", "", header.replace("\ufeff", "")).strip().lower()


def _column_index(header_row: list[str]) -> dict[str, int]:
    normalized = [_norm_header(cell) for cell in header_row]
    mapping: dict[str, int] = {}
    for field_name, aliases in _HEADER_ALIASES.items():
        for position, cell in enumerate(normalized):
            if cell in aliases:
                mapping[field_name] = position
                break
    return mapping


def _parse_csv(
    raw: bytes,
    *,
    date_format: str | None,
    account_type: str | None,
    currency: str | None,
) -> YnabBudget:
    text = decode_raw(raw)
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise YnabParseError(f"Could not read CSV: {exc}") from exc
    rows = [row for row in rows if row and any(cell.strip() for cell in row)]
    if not rows:
        raise YnabParseError("CSV is empty")

    columns = _column_index(rows[0])
    if "date" not in columns:
        raise YnabParseError("CSV is missing a `Date` column")
    if "outflow" not in columns and "inflow" not in columns:
        raise YnabParseError("CSV is missing `Outflow`/`Inflow` columns")

    data_rows = rows[1:]
    samples = [
        _cell(row, columns["date"]).strip()
        for row in data_rows
        if _cell(row, columns["date"]).strip()
    ]
    applied_format, assumed = _resolve_date_format(samples, date_format)

    accounts: dict[str, YnabAccount] = {}
    groups: dict[str, YnabGroup] = {}
    categories: dict[str, YnabCategory] = {}
    payees: dict[str, YnabPayee] = {}
    transactions: list[YnabTxn] = []
    unmappable: list[YnabIssue] = []
    notes: list[YnabIssue] = []
    if assumed:
        notes.append(
            YnabIssue(
                code="date_format_assumed",
                reason=(
                    f"Every CSV date is ambiguous (both parts ≤ 12), so {applied_format} was "
                    "assumed — YNAB's default export order. Re-run with `date_format` set if "
                    "your budget uses another order (docs/38)."
                ),
            )
        )
    if account_type:
        notes.append(
            YnabIssue(
                code="account_type_supplied",
                reason=f"Account type `{account_type}` supplied for the CSV's accounts",
            )
        )

    for position, row in enumerate(data_rows, start=1):
        snippet = ",".join(row)[:200]
        raw_date = _cell(row, columns["date"]).strip()
        when = _strptime(raw_date, applied_format)
        if when is None:
            unmappable.append(
                YnabIssue(
                    code=CODE_INVALID_DATE,
                    index=position,
                    reason=f"Date `{raw_date}` does not parse as {applied_format}",
                    detail=snippet,
                )
            )
            continue
        amount = _row_amount(row, columns, position, snippet)
        if isinstance(amount, YnabIssue):
            unmappable.append(amount)
            continue

        account_name = _cell(row, columns.get("account", -1)).strip()
        account_ref = None
        if account_name:
            account_ref = f"acct:{account_name}"
            if account_ref not in accounts:
                accounts[account_ref] = YnabAccount(
                    ynab_id=account_ref,
                    name=account_name,
                    ynab_type=account_type or "checking",
                    type_assumed=not bool(account_type),
                )

        group_name = _cell(row, columns.get("group", -1)).strip()
        category_cell = (
            _cell(row, columns.get("subcategory", -1)).strip()
            or _cell(row, columns.get("category", -1)).strip()
        )
        category_ref, _group_ref = _csv_category(
            category_cell, group_name, groups=groups, categories=categories
        )

        payee_name = _cell(row, columns.get("payee", -1)).strip()
        payee_ref = None
        if payee_name:
            payee_ref = f"payee:{payee_name}"
            if payee_ref not in payees:
                payees[payee_ref] = YnabPayee(ynab_id=payee_ref, name=payee_name)

        cleared_cell = _cell(row, columns.get("cleared", -1)).strip().lower()
        cleared = cleared_cell if cleared_cell in CLEARED_VALUES else None
        transactions.append(
            YnabTxn(
                index=position,
                date=when,
                amount=amount,
                raw=snippet,
                account_ref=account_ref,
                payee_ref=payee_ref,
                category_ref=category_ref,
                memo=_cell(row, columns.get("memo", -1)).strip() or None,
                cleared=cleared,
                cleared_issue=bool(cleared_cell) and cleared is None,
            )
        )

    return YnabBudget(
        source_format="csv",
        name="YNAB register export",
        currency=currency.upper() if currency else None,
        accounts=list(accounts.values()),
        groups=list(groups.values()),
        categories=list(categories.values()),
        payees=list(payees.values()),
        transactions=transactions,
        unmappable=unmappable,
        notes=notes,
        date_format=applied_format,
        source_rows=len(data_rows),
    )


def _csv_category(
    category_cell: str,
    group_name: str,
    groups: dict[str, YnabGroup],
    categories: dict[str, YnabCategory],
) -> tuple[str | None, str | None]:
    """Build/resolve a synthetic category (+group) from CSV cell values."""
    if not category_cell:
        return None, None
    # YNAB 4 splits "Master Category" / "Sub Category"; newer exports combine
    # them as "Master: Sub" in one cell. Support both (docs/38).
    master, sub = group_name, category_cell
    if not group_name and ": " in category_cell:
        master, sub = (part.strip() for part in category_cell.split(":", 1))
    elif group_name and ": " in category_cell:
        sub = category_cell.split(":", 1)[1].strip()
    group_ref = None
    if master:
        group_ref = f"grp:{master}"
        if group_ref not in groups:
            groups[group_ref] = YnabGroup(ynab_id=group_ref, name=master)
    category_ref = f"cat:{sub}"
    if category_ref not in categories:
        categories[category_ref] = YnabCategory(ynab_id=category_ref, name=sub, group_id=group_ref)
    return category_ref, group_ref


def _cell(row: list[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return row[index]


def _row_amount(
    row: list[str], columns: dict[str, int], index: int, snippet: str
) -> Decimal | YnabIssue:
    """Signed amount for a CSV row, or the issue that makes it unmappable."""
    outflow = _cell(row, columns.get("outflow", -1)).strip()
    inflow = _cell(row, columns.get("inflow", -1)).strip()
    if not outflow and not inflow:
        return YnabIssue(
            code=CODE_INVALID_AMOUNT,
            index=index,
            reason="Both Outflow and Inflow are empty — the row has no amount",
            detail=snippet,
        )
    if outflow and inflow:
        return YnabIssue(
            code=CODE_INVALID_AMOUNT,
            index=index,
            reason="Row has both an Outflow and an Inflow value",
            detail=snippet,
        )
    cell = outflow or inflow
    amount = _parse_decimal(cell)
    if amount is None:
        return YnabIssue(
            code=CODE_INVALID_AMOUNT,
            index=index,
            reason=f"Amount `{cell}` is not a number",
            detail=snippet,
        )
    if -amount.as_tuple().exponent > 3:
        return YnabIssue(
            code=CODE_INVALID_AMOUNT,
            index=index,
            reason=f"Amount `{cell}` has more than 3 decimal places",
            detail=snippet,
        )
    return -amount if outflow else amount


def _parse_decimal(cell: str) -> Decimal | None:
    """Read a money cell: strip symbols, resolve `1,234.56` vs `1.234,56` vs `12,34`."""
    text = cell.replace("\u00a0", "").replace(" ", "").replace("$", "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace("\u2212", "-")
    if text.startswith("+"):
        text = text[1:]
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        grouped = len(parts) > 1 and all(len(p) == 3 for p in parts[1:]) and len(parts[0]) <= 3
        text = "".join(parts) if grouped else text.replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def _strptime(value: str, fmt: str) -> dt_date | None:
    if not value:
        return None
    if fmt == "iso":
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    try:
        return datetime.strptime(value, fmt).date()
    except ValueError:
        return None


def _resolve_date_format(samples: list[str], explicit: str | None) -> tuple[str, bool]:
    """Pick a strptime format (or ``"iso"``) from the file's own evidence.

    Returns ``(format, assumed)`` — ``assumed=True`` means no row disambiguated
    the day/month order and YNAB's default (month first) was taken, which the
    caller must surface as a note rather than applying silently.
    """
    if explicit:
        if not samples or all(_strptime(value, explicit) is not None for value in samples):
            return explicit, False
        return explicit, False  # failures become per-row invalid_date issues
    if samples and all(_strptime(value, "iso") is not None for value in samples):
        return "iso", False

    separators = {m.group(1) for m in (re.match(r"^\s*\d{1,4}([/.-])", s) for s in samples) if m}
    sep = separators.pop() if len(separators) == 1 else "/"
    triples = [[part for part in re.split(r"[/.-]", sample.strip()) if part] for sample in samples]
    triples = [parts for parts in triples if len(parts) == 3]
    if not triples:
        # Nothing parseable in any row: every row reports its own invalid_date.
        return "iso", True
    year_first = [len(parts[0]) == 4 for parts in triples]
    if any(year_first) and not all(year_first):
        raise YnabParseError("CSV mixes year-first and day-first dates — export one format")
    day_votes = month_votes = 0
    if not all(year_first):
        for first, second, _third in triples:
            try:
                a, b = int(first), int(second)
            except ValueError:
                continue
            if a > 12 and b <= 12:
                day_votes += 1
            elif b > 12 and a <= 12:
                month_votes += 1
    if day_votes and month_votes:
        raise YnabParseError("CSV mixes day-first and month-first dates — export one format")
    if all(year_first):
        return f"%Y{sep}%m{sep}%d", False
    year = "%Y" if all(len(parts[2]) == 4 for parts in triples) else "%y"
    if day_votes:
        return f"%d{sep}%m{sep}{year}", False
    if month_votes:
        return f"%m{sep}%d{sep}{year}", False
    return f"%m{sep}%d{sep}{year}", True
