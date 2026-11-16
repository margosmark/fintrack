"""Importer protocol, RawTransaction contract, and dedup hashing (docs/03)."""

import hashlib
from dataclasses import dataclass
from datetime import date as dt_date
from decimal import Decimal
from typing import Protocol

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class RawTransaction:
    """Parser-neutral output contract consumed by the batch pipeline (docs/03)."""

    date: dt_date
    amount_native: Decimal  # signed, account currency; debits negative
    raw_description: str  # verbatim from the file
    currency: str | None = None  # None -> account.currency
    posting_date: dt_date | None = None
    external_ref: str | None = None  # e.g. OFX FITID — debugging aid, not persisted


@dataclass(frozen=True)
class AccountView:
    """Minimal account projection importers rely on.

    Importers never touch the DB: they see the account's currency and, for the
    generic-CSV importer, the institution's raw ``importer_profile`` payload.
    """

    currency: str
    importer_profile: dict | str | None = None


class Importer(Protocol):
    @staticmethod
    def detect(raw: bytes) -> bool: ...

    @staticmethod
    def parse(raw: bytes, account: AccountView) -> list[RawTransaction]: ...


def decode_raw(raw: bytes) -> str:
    """UTF-8 with a latin-1 fallback on UnicodeDecodeError (docs/03 encoding rule)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def build_dedup_hash(account_id: int, txn: RawTransaction) -> str:
    """SHA256 hex over the pinned canonical string (w2-contract #4)."""
    amount = txn.amount_native.quantize(TWO_PLACES)
    if amount == 0:
        # A sub-cent amount quantizes to -0.00, whose canonical STRING ("-0.00")
        # differs from "0.00" while being the same money — one transaction, not
        # two. Only |amount| < half a cent reaches here, so no existing hash of
        # a real row moves (property-pinned in test_property_dedup_hash).
        amount = abs(amount)
    canonical = (
        f"{account_id}|{txn.date.isoformat()}|{amount}|{' '.join(txn.raw_description.split())}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawTrade:
    """Investment-import parser-neutral output contract."""

    account_id_source: str  # CSV account_id column (matched to Account by name/lookup)
    action: str  # TradeAction enum value
    date: dt_date
    symbol: str  # ticker, e.g. "VEQT"
    security_name: str  # full name from CSV
    currency: str  # listing currency
    quantity: Decimal  # signed: buy/drip positive, sell negative, cash-only 0
    price_native: Decimal  # unit price; gross cash amount for cash-only actions
    commission: Decimal  # only for buy/sell/drip
    net_cash: Decimal  # net cash amount from CSV (for validation/debug)
    description: str  # raw description from CSV


def normalized_payee(raw_description: str) -> str:
    """Initial payee normalization: collapsed whitespace; refined later by docs/04."""
    return " ".join(raw_description.split())
