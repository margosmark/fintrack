"""Export bundle — CSV + JSON manifest + OFX rendering for the one-click export.

M75 (docs/11 § "One-click export (M75)"). Three renderers, all pure over data
the caller already loaded, so nothing here opens a session, calls an LLM or
touches the network:

- ``render_csv``   — one RFC-4180 table per registry model, cells in JSON form
                     (ISO-8601 dates, Decimals as exact strings, ``true``/
                     ``false``, ```` for null).
- ``render_manifest`` — the JSON manifest that documents the ZIP's schema and
                     lists every file with its row count and columns.
- ``render_ofx``   — an OFX 1.02 (SGML) bank statement of one account's
                     committed transactions, parsed back by
                     ``app.importers.ofx`` (the round trip is the acceptance
                     test: 0 net-new rows after dedup).

E2EE interaction (docs/11 § "Export & the vault (E2EE)"): with
``vault="locked"`` a field that HAS a ciphertext column ships the marker
``E2EE:<v1 envelope>`` instead of its plaintext — we never fabricate a value
the session cannot read, and we never silently downgrade to a placeholder the
reader could mistake for a payee. ``vault="unlocked"`` ships the stored
plaintext (docs/33's honest status: the server can still read today's
plaintext columns, so there is no decryption step server-side).
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
MANIFEST_KIND = "fintrack-export"

# The documented marker for a ciphertext-withheld cell (docs/11, /help
# "Your data"). Greppable, unambiguous, and impossible to mistake for a payee
# because the payload after the colon is the v1 field envelope itself.
E2EE_MARKER = "E2EE:"

VAULT_STATES = frozenset({"locked", "unlocked"})

# plaintext column -> ciphertext column, per table (docs/33 Phase 2). Only
# fields that HAVE a cipher column can be withheld; everything else ships as
# stored (the server has no key and never invents one).
CIPHER_FIELDS: dict[str, dict[str, str]] = {
    "transactions": {
        "raw_description": "raw_desc_cipher",
        "normalized_payee": "payee_cipher",
        "amount_native": "amount_cipher",
        "cad_amount": "cad_amount_cipher",
    },
    "accounts": {"name": "name_cipher"},
}

# OFX 1.02 ACCTTYPE has no brokerage/registered value (the DTD allows
# CHECKING | SAVINGS | MONEYMRKT | CREDITLINE | CD), so non-bank accounts
# export as OTHER — a deliberate, documented deviation rather than a lie
# about the account kind. Our own importer ignores ACCTTYPE entirely.
_OFX_ACCOUNT_TYPES: dict[str, str] = {
    "chequing": "CHECKING",
    "savings": "SAVINGS",
    "credit_card": "CREDITCARD",
    "line_of_credit": "CREDITLINE",
}

_TWO_PLACES = Decimal("0.01")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def validate_vault(vault: str) -> str:
    """Return the vault state or raise the documented 422 envelope."""
    if vault not in VAULT_STATES:
        # Imported lazily: app.core.errors pulls settings, and this module is
        # imported by pure renderers (and their tests) before the app exists.
        from app.core.errors import api_error

        raise api_error(
            422,
            f"vault must be one of {sorted(VAULT_STATES)}, got {vault!r}",
            "export_invalid_vault",
        )
    return vault


def slugify(value: str, fallback: str) -> str:
    """Filesystem-safe slug for a download filename (`Chequing *` -> chequing)."""
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug[:48] or fallback


def _cell(value: Any) -> str:
    """One CSV cell from a JSON-rendered value (docs/11 § CSV dialect)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def render_csv(
    table: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    vault: str,
) -> tuple[bytes, int]:
    """CSV bytes for one table + the number of cells withheld behind the marker."""
    cipher_map = CIPHER_FIELDS.get(table, {})
    masked = 0
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        cells: list[str] = []
        for column in columns:
            value = row.get(column)
            cipher_column = cipher_map.get(column)
            if cipher_column and vault == "locked" and row.get(cipher_column):
                value = f"{E2EE_MARKER}{row[cipher_column]}"
                masked += 1
            cells.append(_cell(value))
        writer.writerow(cells)
    return buffer.getvalue().encode("utf-8"), masked


def render_manifest(
    *,
    generated_at: datetime,
    user_id: int,
    email: str,
    vault: str,
    visibility: str,
    files: list[dict[str, Any]],
    masked_cells: int,
    excluded_tables: dict[str, str],
    filters: dict[str, str],
) -> bytes:
    """The JSON manifest — the documented schema of everything else in the ZIP."""
    manifest = {
        "v": MANIFEST_VERSION,
        "kind": MANIFEST_KIND,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "user": {"id": user_id, "email": email},
        "vault": vault,
        "visibility": visibility,
        "format": {
            "csv_encoding": "utf-8",
            "csv_delimiter": ",",
            "csv_line_terminator": "\\n",
            "csv_null": "",
            "boolean": "true | false",
            "date": "ISO-8601 (YYYY-MM-DD)",
            "decimal": "exact string (no float rounding)",
        },
        "markers": {
            "e2ee": (
                f"{E2EE_MARKER}<v1 base64(iv).base64(ciphertext)> — plaintext "
                "withheld for a cipher-backed field while the vault is locked"
            )
        },
        "masked_cells": masked_cells,
        "visibility_filters": filters,
        "excluded_tables": excluded_tables,
        "files": files,
        "ofx": {
            "endpoint": "GET /api/v1/export/ofx?account_id={account_id}",
            "note": (
                "one OFX 1.02 statement per account; NOT inside this ZIP "
                "(docs/11 step 2 pins the ZIP to CSVs + this manifest)"
            ),
        },
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")


def _ofx_amount(value: Any) -> str:
    """`-12.34` — two places, always signed (OFX TRNAMT is signed)."""
    try:
        return f"{Decimal(str(value)).quantize(_TWO_PLACES):f}"
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - Numeric(18,2)
        raise ValueError(f"amount {value!r} is not a decimal") from exc


def _ofx_escape(text: str) -> str:
    """OFX 1.02 is SGML: raw `<`/`&` would terminate the tag early.

    ofxparse decodes the three entities back to the exact characters, so the
    round trip reproduces the stored description (and therefore the dedup
    hash) byte for byte.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_ofx(
    *,
    account_id: int,
    account_type: str,
    currency: str,
    rows: list[dict[str, Any]],
    vault: str,
) -> bytes:
    """OFX 1.02 SGML statement of ONE account's committed transactions.

    `rows` are JSON-rendered transaction dicts ordered by (date, id). The
    description written to `<NAME>` is the transaction's raw_description
    collapsed to single spaces — whitespace-neutral, so
    `build_dedup_hash` recomputes to the stored hash on re-import.
    """
    today = date.today().isoformat()
    # Rows are JSON-rendered (docs/11 CSV dialect), so `date` is an ISO string
    # — lexicographic min/max over YYYY-MM-DD is chronological order.
    dates = sorted(str(row["date"]) for row in rows if row.get("date"))
    start = dates[0] if dates else today
    end = dates[-1] if dates else today

    lines = [
        "OFXHEADER:100",
        "DATA:OFXSGML",
        "VERSION:102",
        "SECURITY:NONSEC",
        "ENCODING:UTF-8",
        "CHARSET:NONE",
        "COMPRESSION:NONE",
        "OLDFILEUID:NONE",
        "NEWFILEUID:NONE",
        "",
        "<OFX>",
        "<BANKMSGSRSV1>",
        "<STMTTRNRS>",
        "<TRNUID>1",
        "<STMTRS>",
        f"<CURDEF>{_ofx_escape(currency)}",
        "<BANKACCTFROM>",
        f"<ACCTID>fintrack-{account_id}",
        f"<ACCTTYPE>{_OFX_ACCOUNT_TYPES.get(account_type, 'OTHER')}",
        "</BANKACCTFROM>",
        "<BANKTRANLIST>",
        f"<DTSTART>{start.replace('-', '')}",
        f"<DTEND>{end.replace('-', '')}",
    ]
    for row in rows:
        description = str(row.get("raw_description") or "")
        cipher = row.get("raw_desc_cipher")
        if vault == "locked" and cipher:
            description = f"{E2EE_MARKER}{cipher}"
        # Collapse whitespace: the importer rebuilds the dedup hash from this
        # string with the same `' '.join(split())` rule, so single-spacing is
        # neutral for dedup while keeping the value inside one SGML line.
        description = " ".join(description.split())
        amount = _ofx_amount(row.get("amount_native"))
        lines.extend(
            [
                "<STMTTRN>",
                "<TRNTYPE>CREDIT" if not amount.startswith("-") else "<TRNTYPE>DEBIT",
                f"<DTPOSTED>{str(row.get('date', '')).replace('-', '')}",
                f"<TRNAMT>{amount}",
                f"<FITID>fintrack-{account_id}-{row.get('id')}",
                f"<NAME>{_ofx_escape(description)}",
                "</STMTTRN>",
            ]
        )
    lines.extend(
        [
            "</BANKTRANLIST>",
            "</STMTRS>",
            "</STMTTRNRS>",
            "</BANKMSGSRSV1>",
            "</OFX>",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """ZIP bytes (deflate) for the given (path, payload) entries, in order."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, payload in entries:
            # Fixed timestamp: a deterministic archive makes byte-comparing two
            # exports of the same rows possible (and tests much simpler).
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def export_zip_filename(today: date | None = None) -> str:
    """`fin-tracker-export-<YYYY-MM-DD>.zip` (docs/11 § file list)."""
    return f"fin-tracker-export-{(today or datetime.now(UTC).date()).isoformat()}.zip"
