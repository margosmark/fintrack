"""LLM-assisted CSV column mapping for import profiles (docs/03).

When an institution has no importer_profile, the wizard calls this module to
suggest column mappings from CSV headers + sample rows. Falls back to
heuristic name-matching when the LLM is unavailable or returns invalid JSON.
"""

import csv
import io
import logging
import re
from typing import Any

from app.importers.base import decode_raw
from app.importers.format_presets import preset_suggestion

logger = logging.getLogger(__name__)

# ── Heuristic keywords (fallback when LLM is unavailable) ──────────────────

_DATE_KEYWORDS = {"date", "trans", "posted", "transaction", "value", "booking"}
_DESC_KEYWORDS = {"desc", "memo", "narrative", "payee", "detail", "particulars", "reference"}
_AMOUNT_KEYWORDS = {"amount", "total", "sum", "value", "balance"}
_DEBIT_KEYWORDS = {"debit", "withdrawal", "out"}
_CREDIT_KEYWORDS = {"credit", "deposit", "in"}
_CURRENCY_KEYWORDS = {"currency", "ccy", "curr"}
_POSTING_KEYWORDS = {"posting", "settlement", "settled"}

# ── JSON Schema for LLM response ──────────────────────────────────────────

COLUMN_MAPPING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "raw_description": {"type": "string"},
                "amount_native": {"type": "string"},
                "debit": {"type": "string"},
                "credit": {"type": "string"},
                "posting_date": {"type": "string"},
                "currency": {"type": "string"},
            },
        },
        "date_format": {"type": "string"},
        "decimal_comma": {"type": "boolean"},
        "credits_positive": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["columns", "date_format", "decimal_comma", "confidence", "reasoning"],
    "additionalProperties": False,
}


def _extract_sample_rows(
    raw: bytes, max_rows: int = 5
) -> tuple[list[str], list[dict[str, str]], bool]:
    """Read CSV headers and up to *max_rows* data rows as dicts.

    Returns ``(headers, rows, has_header)``. For headerless files, synthetic
    ``col_{i+1}`` names are used and ``has_header`` is False; the first data row
    is retained (no header line is consumed).
    """
    all_rows = list(csv.reader(io.StringIO(decode_raw(raw), newline="")))
    if not all_rows:
        return [], [], False
    first = all_rows[0]
    has_header = not _is_headerless(first)
    if has_header:
        headers = list(first)
        data_rows = all_rows[1 : 1 + max_rows]
    else:
        ncols = len(first)
        headers = [f"col_{i + 1}" for i in range(ncols)]
        data_rows = all_rows[:max_rows]
    rows: list[dict[str, str]] = []
    for row in data_rows:
        padded = row + [""] * (len(headers) - len(row))
        rows.append({h: padded[i] for i, h in enumerate(headers)})
    return headers, rows, has_header


def _is_headerless(first_row: list[str]) -> bool:
    """Heuristic header-vs-data guess for the first CSV row.

    A header keyword anywhere in the row means it is a header. Otherwise, if any
    cell looks like a date or number, the row is data (headerless). The manual
    wizard checkbox is the safety net for misdetection.
    """
    _ALL_KEYWORDS = (
        _DATE_KEYWORDS
        | _DESC_KEYWORDS
        | _AMOUNT_KEYWORDS
        | _DEBIT_KEYWORDS
        | _CREDIT_KEYWORDS
        | _CURRENCY_KEYWORDS
        | _POSTING_KEYWORDS
    )

    def _has_kw(c: str) -> bool:
        return any(kw in c.lower().strip() for kw in _ALL_KEYWORDS)

    if any(_has_kw(c) for c in first_row):
        return False
    _DATEISH = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
    _NUMISH = re.compile(r"[-+]?[\d.,]+$")
    return any(_DATEISH.search(c) or _NUMISH.fullmatch(c.strip()) for c in first_row)


def _infer_date_format(sample: str) -> str:
    """Infer a strptime date format from a sample value (defaults to ``%Y-%m-%d``)."""
    if re.match(r"\d{2}/\d{2}/\d{4}", sample):
        return "%m/%d/%Y"
    if re.match(r"\d{4}\.\d{2}\.\d{2}", sample):
        return "%Y.%m.%d"
    if re.match(r"\d{2}-[A-Za-z]{3}-\d{4}", sample):
        return "%d-%b-%Y"
    return "%Y-%m-%d"


# Human-readable date format templates → strptime directives.
# The LLM often returns human templates (e.g. "YYYY-MM-DD") instead of
# strptime directives ("%Y-%m-%d").  This mapping normalises the most
# common patterns so _llm_suggest always stores valid strptime strings.
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
        return fmt  # already strptime
    return _HUMAN_TO_STRPTIME.get(fmt, fmt)


def _heuristic_mapping(headers: list[str], rows: list[dict[str, str]]) -> dict[str, Any]:
    """Best-effort column mapping from header names and sample values."""
    lower_map = {h.lower().strip(): h for h in headers}

    def _match(keywords: set[str]) -> str | None:
        for kw in keywords:
            for lh, orig in lower_map.items():
                if kw in lh:
                    return orig
        return None

    date_col = _match(_DATE_KEYWORDS)
    desc_col = _match(_DESC_KEYWORDS)

    # Detect split vs signed: look for separate debit/credit columns.
    debit_col = _match(_DEBIT_KEYWORDS)
    credit_col = _match(_CREDIT_KEYWORDS)

    columns: dict[str, str] = {}
    if date_col:
        columns["date"] = date_col
    if desc_col:
        columns["raw_description"] = desc_col

    if debit_col and credit_col:
        columns["debit"] = debit_col
        columns["credit"] = credit_col
    else:
        amount_col = _match(_AMOUNT_KEYWORDS)
        if amount_col:
            columns["amount_native"] = amount_col

    currency_col = _match(_CURRENCY_KEYWORDS)
    if currency_col:
        columns["currency"] = currency_col

    posting_col = _match(_POSTING_KEYWORDS)
    if posting_col and posting_col != date_col:
        columns["posting_date"] = posting_col

    # Detect date format from first non-empty date value.
    date_format = "%Y-%m-%d"
    if date_col and rows:
        sample = next((r.get(date_col, "") for r in rows if r.get(date_col, "")), "")
        date_format = _infer_date_format(sample)

    # Detect decimal comma from amount values.
    decimal_comma = False
    amount_cols = [columns.get("amount_native"), columns.get("debit"), columns.get("credit")]
    for col in amount_cols:
        if col:
            for row in rows:
                val = row.get(col, "")
                if val and "," in val and "." not in val:
                    decimal_comma = True
                    break
            if decimal_comma:
                break

    return {
        "columns": columns,
        "date_format": date_format,
        "decimal_comma": decimal_comma,
        "credits_positive": False,
        "confidence": 0.4,
        "reasoning": "Heuristic fallback based on column name patterns.",
    }


def _heuristic_positional_mapping(headers: list[str], rows: list[dict[str, str]]) -> dict[str, Any]:
    """Deterministic positional mapping for headerless CSVs (offline; skips LLM)."""
    ncols = len(headers)
    _DATEISH = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
    _NUMISH = re.compile(r"[-+]?[\d.,]+$")

    date_scores: list[int] = [0] * ncols
    num_scores: list[int] = [0] * ncols
    text_scores: list[int] = [0] * ncols
    for row in rows:
        for i in range(ncols):
            val = row.get(headers[i], "").strip()
            if not val:
                continue
            if _DATEISH.search(val) or _infer_date_format(val) != "%Y-%m-%d":
                date_scores[i] += 1
            elif _NUMISH.fullmatch(val):
                num_scores[i] += 1
            else:
                text_scores[i] += 1

    # date = column with the highest date_score (require a positive score).
    columns: dict[str, str] = {}
    if any(s > 0 for s in date_scores):
        date_idx = max(range(ncols), key=lambda i: date_scores[i])
        if date_scores[date_idx] > 0:
            columns["date"] = str(date_idx + 1)

    used = {int(columns["date"]) - 1} if "date" in columns else set()

    # amount_native = highest num_score among non-date columns (require > 0).
    if any(s > 0 for s in num_scores):
        candidates = [i for i in range(ncols) if i not in used]
        amount_idx = max(candidates, key=lambda i: num_scores[i])
        if num_scores[amount_idx] > 0:
            columns["amount_native"] = str(amount_idx + 1)
            used.add(amount_idx)

    # raw_description = highest text_score among text-leaning unchosen columns,
    # else the first remaining unchosen column.
    text_candidates = [
        i
        for i in range(ncols)
        if i not in used and date_scores[i] == 0 and num_scores[i] == 0 and text_scores[i] > 0
    ]
    if text_candidates:
        desc_idx = max(text_candidates, key=lambda i: text_scores[i])
    else:
        desc_idx = next((i for i in range(ncols) if i not in used), None)
    if desc_idx is not None:
        columns["raw_description"] = str(desc_idx + 1)

    date_format = "%Y-%m-%d"
    if "date" in columns:
        date_idx = int(columns["date"]) - 1
        sample = next(
            (
                r.get(headers[date_idx], "").strip()
                for r in rows
                if r.get(headers[date_idx], "").strip()
            ),
            "",
        )
        date_format = _infer_date_format(sample)

    decimal_comma = False
    if "amount_native" in columns:
        amount_idx = int(columns["amount_native"]) - 1
        for row in rows:
            val = row.get(headers[amount_idx], "")
            if val and "," in val and "." not in val:
                decimal_comma = True
                break

    return {
        "columns": columns,
        "date_format": date_format,
        "decimal_comma": decimal_comma,
        "credits_positive": False,
        "headerless": True,
        "confidence": 0.4,
        "reasoning": "No header row detected; columns mapped by position from sample data.",
    }


def suggest_column_mapping(
    raw: bytes,
    provider: Any | None,
    model: str = "",
) -> dict[str, Any]:
    """Suggest CSV column mappings using LLM or heuristic fallback.

    Returns a dict matching ``SuggestProfileResponse`` shape.
    ``provider`` is a ``LlmProvider`` instance or ``None``.
    """
    headers, rows, has_header = _extract_sample_rows(raw)
    if not headers:
        return {
            "columns": {},
            "date_format": "%Y-%m-%d",
            "decimal_comma": False,
            "confidence": 0.0,
            "reasoning": "No CSV data found.",
        }

    # Headerless files map by position deterministically; skip the LLM entirely.
    if not has_header:
        return _heuristic_positional_mapping(headers, rows)

    # Known export formats (docs/58 §A): a deterministic, named suggestion
    # before the LLM — still a suggestion, still confirmed by the user.
    preset = preset_suggestion(headers, rows)
    if preset is not None:
        return preset

    # Try LLM first
    if provider is not None:
        try:
            return _llm_suggest(headers, rows, provider, model)
        except Exception:
            logger.warning("LLM column mapping failed, falling back to heuristic", exc_info=True)

    return _heuristic_mapping(headers, rows)


def _llm_suggest(
    headers: list[str],
    rows: list[dict[str, str]],
    provider: Any,
    model: str,
) -> dict[str, Any]:
    """Call the LLM to suggest column mappings."""
    rows_json = "\n".join(" | ".join(f"{h}: {r.get(h, '')}" for h in headers) for r in rows)

    system_prompt = (
        "You are a CSV column mapper for a financial transaction importer. "
        "Given CSV headers and sample rows, identify which columns map to the "
        "target fields. Return a JSON object matching the schema provided.\n\n"
        "Target fields:\n"
        "- date: the transaction date column (REQUIRED)\n"
        "- raw_description: the description/memo/narrative column (REQUIRED)\n"
        "- amount_native: single signed amount column (use when debits/credits are not split)\n"
        "- debit: debit amount column (use in split mode)\n"
        "- credit: credit amount column (use in split mode)\n"
        "- posting_date: settlement/posting date if present\n"
        "- currency: currency code column if present\n\n"
        "Rules:\n"
        "- Use amount_native for single-amount mode, OR debit+credit for split mode. Never both.\n"
        "- Detect the date format from sample values.\n"
        "- Detect if amounts use decimal comma (e.g., 1.234,56).\n"
        "- credits_positive: true if credits are positive in the file.\n"
        "- Set confidence 0.0-1.0 based on how certain you are.\n"
        "- Provide brief reasoning for your choices."
    )

    user_prompt = (
        f"CSV headers: {headers}\n\n"
        f"Sample rows:\n{rows_json}\n\n"
        "Suggest column mappings for this CSV."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    result = provider.chat(messages, COLUMN_MAPPING_SCHEMA)

    # Validate required fields
    columns = result.get("columns", {})
    if not isinstance(columns, dict):
        raise ValueError("LLM returned invalid columns shape")

    return {
        "columns": columns,
        "date_format": _normalize_date_format(result.get("date_format", "%Y-%m-%d")),
        "decimal_comma": bool(result.get("decimal_comma", False)),
        "credits_positive": bool(result.get("credits_positive", False)),
        "confidence": float(result.get("confidence", 0.5)),
        "reasoning": str(result.get("reasoning", "")),
    }
