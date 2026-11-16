"""column_mapping pure-function tests (no DB / no network).

Exercises the heuristic mapper (split-vs-signed detection, date-format
inference, decimal-comma detection) and the LLM suggestion path including
the automatic fallback to heuristics when the LLM raises.
"""

import csv
import io
from typing import Any

from app.services import column_mapping as cm


def _csv(headers: list[str], rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode()


def _csv_no_header(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    return buf.getvalue().encode()


def test_headerless_csv_detected() -> None:
    raw = _csv_no_header([["2024-01-05", "COFFEE", "-12.50"]])
    result = cm.suggest_column_mapping(raw, None)
    assert result["headerless"] is True
    assert all(v.isdigit() for v in result["columns"].values())
    assert result["columns"]["date"] == "1"
    assert result["columns"]["amount_native"] == "3"
    assert result["columns"]["raw_description"] == "2"


def test_split_mode_detects_debit_credit_columns() -> None:
    """When debit + credit columns exist, split mode wins over amount_native."""
    raw = _csv(
        ["Date", "Description", "Debit", "Credit", "Currency"],
        [
            {
                "Date": "2024-01-01",
                "Description": "coffee",
                "Debit": "3,50",
                "Credit": "",
                "Currency": "EUR",
            }
        ],
    )
    result = cm.suggest_column_mapping(raw, None)
    cols = result["columns"]
    assert cols.get("debit") == "Debit"
    assert cols.get("credit") == "Credit"
    assert "amount_native" not in cols


def test_signed_single_amount_mode() -> None:
    """Without debit/credit columns, a single signed amount column is chosen."""
    raw = _csv(
        ["Date", "Description", "Amount"],
        [{"Date": "2024-01-01", "Description": "pay", "Amount": "12.34"}],
    )
    result = cm.suggest_column_mapping(raw, None)
    cols = result["columns"]
    assert cols.get("amount_native") == "Amount"
    assert "debit" not in cols
    assert "credit" not in cols


def test_date_format_inference() -> None:
    cases = {
        "03/15/2024": "%m/%d/%Y",
        "2024.03.15": "%Y.%m.%d",
        "15-Mar-2024": "%d-%b-%Y",
        "2024-03-15": "%Y-%m-%d",
    }
    for sample, expected in cases.items():
        raw = _csv(
            ["Date", "Description", "Amount"],
            [{"Date": sample, "Description": "x", "Amount": "1"}],
        )
        result = cm.suggest_column_mapping(raw, None)
        assert result["date_format"] == expected, sample


def test_normalize_date_format() -> None:
    """Human-readable templates are mapped to strptime directives."""
    assert cm._normalize_date_format("YYYY-MM-DD") == "%Y-%m-%d"
    assert cm._normalize_date_format("DD/MM/YYYY") == "%d/%m/%Y"
    assert cm._normalize_date_format("MM/DD/YYYY") == "%m/%d/%Y"
    assert cm._normalize_date_format("YYYY.MM.DD") == "%Y.%m.%d"
    # Already a strptime directive — passed through unchanged.
    assert cm._normalize_date_format("%Y-%m-%d") == "%Y-%m-%d"
    # Unknown template — returned as-is (error path surfaces it).
    assert cm._normalize_date_format("CUSTOM-FORMAT") == "CUSTOM-FORMAT"


def test_llm_suggest_normalizes_date_format() -> None:
    """LLM returning human date template is normalised to strptime."""
    provider = _FakeProvider(
        {
            "columns": {
                "date": "Date",
                "raw_description": "Desc",
                "amount_native": "Amount",
            },
            "date_format": "YYYY-MM-DD",
            "decimal_comma": False,
            "credits_positive": False,
            "confidence": 0.8,
            "reasoning": "test",
        }
    )
    result = cm._llm_suggest(["Date", "Desc", "Amount"], [], provider, "")
    assert result["date_format"] == "%Y-%m-%d"


def test_decimal_comma_detection() -> None:
    # The heuristic flags decimal-comma when a value has a comma and no period.
    raw_comma = _csv(
        ["Date", "Description", "Amount"],
        [{"Date": "2024-01-01", "Description": "x", "Amount": "1234,56"}],
    )
    assert cm.suggest_column_mapping(raw_comma, None)["decimal_comma"] is True

    raw_dot = _csv(
        ["Date", "Description", "Amount"],
        [{"Date": "2024-01-01", "Description": "x", "Amount": "1234.56"}],
    )
    assert cm.suggest_column_mapping(raw_dot, None)["decimal_comma"] is False


class _FakeProvider:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls = 0

    def chat(self, messages: list[dict[str, str]], schema: Any) -> dict[str, Any]:
        self.calls += 1
        return self.result


def test_llm_suggest_returns_normalized_mapping() -> None:
    provider = _FakeProvider(
        {
            "columns": {
                "date": "Txn Date",
                "raw_description": "Memo",
                "amount_native": "Amt",
            },
            "date_format": "%Y-%m-%d",
            "decimal_comma": False,
            "credits_positive": True,
            "confidence": 0.92,
            "reasoning": "llm pick",
        }
    )
    result = cm._llm_suggest(["Txn Date", "Memo", "Amt"], [], provider, "")
    assert provider.calls == 1
    assert result["columns"]["date"] == "Txn Date"
    assert result["credits_positive"] is True
    assert result["confidence"] == 0.92
    assert result["reasoning"] == "llm pick"


def test_suggest_column_mapping_uses_llm_when_available() -> None:
    provider = _FakeProvider(
        {
            "columns": {
                "date": "Date",
                "raw_description": "Desc",
                "amount_native": "Amount",
            },
            "date_format": "%Y-%m-%d",
            "decimal_comma": False,
            "credits_positive": False,
            "confidence": 0.88,
            "reasoning": "llm",
        }
    )
    raw = _csv(
        ["Date", "Desc", "Amount"],
        [{"Date": "2024-01-01", "Desc": "x", "Amount": "1"}],
    )
    result = cm.suggest_column_mapping(raw, provider)
    # confidence 0.88 comes from the LLM, not the heuristic's 0.4 floor.
    assert result["confidence"] == 0.88
    assert result["columns"]["date"] == "Date"


class _BoomProvider:
    def chat(self, messages: list[dict[str, str]], schema: Any) -> dict[str, Any]:
        raise RuntimeError("boom")


def test_suggest_column_mapping_falls_back_to_heuristic_on_llm_error() -> None:
    raw = _csv(
        ["Date", "Desc", "Amount"],
        [{"Date": "2024-01-01", "Desc": "x", "Amount": "1"}],
    )
    result = cm.suggest_column_mapping(raw, _BoomProvider())
    # heuristic fallback confidence and a real date column mapping.
    assert result["confidence"] == 0.4
    assert result["columns"].get("date") == "Date"


def test_header_only_csv_still_maps_columns() -> None:
    raw = _csv(["Date", "Description", "Amount"], [])
    result = cm.suggest_column_mapping(raw, None)
    assert result["columns"].get("date") == "Date"
    assert result["columns"].get("amount_native") == "Amount"
