"""OFX/QFX importer backed by ofxparse SGML parsing (docs/03)."""

import io
from datetime import date as dt_date
from datetime import datetime
from decimal import Decimal

from ofxparse import OfxParser

from app.importers.base import AccountView, RawTransaction


class OfxParseError(ValueError):
    """Malformed OFX/QFX input; mapped to the 422 ofx_parse_error envelope (docs/03)."""


class OfxImporter:
    @staticmethod
    def detect(raw: bytes) -> bool:
        head = raw[:2048].upper()
        return b"OFXHEADER" in head or b"<OFX>" in head

    @staticmethod
    def parse(raw: bytes, account: AccountView) -> list[RawTransaction]:
        try:
            ofx = OfxParser.parse(io.BytesIO(raw))
        except Exception as exc:
            # ofxparse raises OfxParserException — and raw ValueError /
            # LookupError on malformed headers — over attacker-controlled
            # bytes (audit L5). Translate every parser failure to one domain
            # error so the API returns a 422 envelope instead of a 500.
            raise OfxParseError(str(exc) or type(exc).__name__) from exc
        rows: list[RawTransaction] = []
        for stmt_account in ofx.accounts:
            stmt = getattr(stmt_account, "statement", None)
            for txn in (getattr(stmt, "transactions", None) or []) if stmt else []:
                when: dt_date = txn.date.date() if isinstance(txn.date, datetime) else txn.date
                payee = (txn.payee or "").strip()
                memo = (getattr(txn, "memo", "") or "").strip()
                rows.append(
                    RawTransaction(
                        date=when,
                        amount_native=Decimal(str(txn.amount)),
                        raw_description=payee if payee else memo,
                        currency=None,
                        external_ref=getattr(txn, "id", None),
                    )
                )
        return rows
