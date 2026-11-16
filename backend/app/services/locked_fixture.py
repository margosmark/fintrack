"""Locked-fixture tenant (M136, docs/33 §Server-first computing §0-a).

A THROWAWAY tenant that is locked by CONSTRUCTION, so every wave-13a gate can
exercise the locked paths without a browser session having to enroll a vault
first:

    user.encryption_required = TRUE      → the write guards (G1-G3) are live
    user has cipher rows, NO envelope    → `useVaultEnrollment()` answers
                                           "enrolled" (the write guard alone
                                           proves a vault story) while every
                                           browser starts LOCKED: there is no
                                           wrapped bundle to unlock with.

That second half is the point. The demo account stays plaintext forever
(docs/33 Product decision), so it can never show a locked surface; and a
tenant that DID enroll an envelope would unlock in the browser that holds its
IndexedDB copy, which makes "load the locked path" a per-device dance. No
envelope ⇒ no device anywhere can unlock ⇒ the fixture loads locked in every
browser, every time, with zero setup.

`ensure_locked_fixture_tenant` is idempotent and seeded with a tiny,
deterministic dataset (2 accounts, 8 transactions — every row carrying a
cipher bundle, one deliberately plaintext legacy row for the mixed-mode
contract of docs/33 A16). It never touches `fx_rates` (the one shared cache,
H7) and never touches the demo tenant.

Not called from the lifespan: a fixture tenant has no business existing in
production. It is seeded on demand — from `scripts/seed_locked_fixture.py`,
from the wave's own test run (docs/33 §locked-fixture tenant), or by an
operator pointing a dev database at it.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from decimal import Decimal

from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.transactions import Transaction
from app.models.users import User

logger = logging.getLogger(__name__)

FIXTURE_EMAIL = "locked-fixture@fintracker.local"
"""The throwaway tenant's login. Distinct from the demo on purpose: a reset,
a wipe, or a demo-mode guard must never be able to reach this tenant."""

FIXTURE_PASSWORD = "locked-fixture-only"  # noqa: S105 -- dev-only fixture tenant, see docstring below
"""A password with no security value — this tenant exists to be looked at
while locked. It is a dev-database fixture, never deployed, and the lifespan
never seeds it (see the module docstring)."""

# Ciphertext-shaped placeholders. They are NEVER decrypted: the fixture has no
# envelope, so no session can hold a DEK for it. They exist so the locked
# surfaces have real cipher columns to withhold — `payee_cipher` present is
# exactly what the client matcher tests (`row.payee_cipher != null`) and the
# A18 withheld-count read.
_CT = "v1.fixture-ciphertext"
_TOKEN = "v1.fixture-token"  # noqa: S105 -- ciphertext-shaped placeholder, never decrypted


def _dedup(account_id: int, day: date, payee: str, amount: str) -> str:
    """The same shape `importers/base.py` hashes, minus the salt dance — the
    fixture rows are written once and never re-imported."""
    return hashlib.sha256(f"{account_id}|{day.isoformat()}|{amount}|{payee}".encode()).hexdigest()


def ensure_locked_fixture_tenant(session: Session) -> User | None:
    """Idempotently create the locked fixture tenant and its dataset.

    Returns None when `SEED_DEMO_ACCOUNT` is off — the same kill switch the
    demo uses, because both are seeding paths that must not run in a
    production-shaped environment (docs/30 §Demo dataset; docs/33).

    ONE transaction, same rule as the demo seed: a failure anywhere rolls the
    whole fixture back rather than leaving a half-encrypted tenant whose
    `encryption_required` is on and whose rows are inconsistent.
    """
    if not get_settings().seed_demo_account:
        return None

    user = session.exec(select(User).where(User.email == FIXTURE_EMAIL)).first()
    if user is None:
        user = User(
            email=FIXTURE_EMAIL,
            hashed_password=hash_password(FIXTURE_PASSWORD),
            # THE switch the whole fixture exists to flip: write guards on.
            encryption_required=True,
        )
        session.add(user)
        session.flush()
        for name in ("Groceries", "Transit", "Income"):
            session.add(Category(user_id=user.id, name=name, is_system=True))
        session.flush()

        chequing = Account(
            user_id=user.id,
            name="Fixture Chequing",
            # The account-name write guard (accounts.py) is the SAME rule as
            # the transaction one, so the fixture satisfies it honestly.
            name_cipher=_CT,
            type=AccountType.chequing,
            currency="CAD",
        )
        savings = Account(
            user_id=user.id,
            name="Fixture Savings",
            name_cipher=_CT,
            type=AccountType.savings,
            currency="CAD",
        )
        session.add_all([chequing, savings])
        session.flush()

        groceries = session.exec(
            select(Category).where(Category.user_id == user.id, Category.name == "Groceries")
        ).first()
        transit = session.exec(
            select(Category).where(Category.user_id == user.id, Category.name == "Transit")
        ).first()

        rows = [
            # (account, day, payee, amount, category, ciphered)
            (chequing, date(2026, 9, 2), "FRESH CO-OP", "-84.20", groceries, True),
            (chequing, date(2026, 9, 5), "TTC TOP-UP", "-56.00", transit, True),
            (chequing, date(2026, 9, 12), "FRESH CO-OP", "-91.05", groceries, True),
            (chequing, date(2026, 9, 18), "METRO #1188", "-42.10", groceries, True),
            (chequing, date(2026, 9, 26), "PAYROLL DEPOSIT", "3200.00", None, True),
            (savings, date(2026, 9, 3), "TRANSFER FROM CHEQUING", "500.00", None, True),
            (savings, date(2026, 9, 15), "INTEREST", "1.42", None, True),
            # The ONE deliberately plaintext row: docs/33 A16's mixed-mode
            # contract (legacy v0 rows show plaintext beside cipher rows), so
            # a locked surface has something the lock does NOT withhold — the
            # honest partial state ("Showing 1 of N") has a real row to show.
            (savings, date(2026, 9, 20), "LEGACY PLAINTEXT ROW", "0.01", None, False),
        ]
        for account, day, payee, amount, category, ciphered in rows:
            session.add(
                Transaction(
                    user_id=user.id,
                    account_id=account.id,
                    date=day,
                    raw_description=payee,
                    normalized_payee=payee,
                    amount_native=Decimal(amount),
                    currency="CAD",
                    cad_amount=Decimal(amount),
                    category_id=category.id if category is not None else None,
                    dedup_hash=_dedup(account.id, day, payee, amount),
                    payee_cipher=_CT if ciphered else None,
                    raw_desc_cipher=_CT if ciphered else None,
                    amount_cipher=_CT if ciphered else None,
                    cad_amount_cipher=_CT if ciphered else None,
                    payee_token=_TOKEN if ciphered else None,
                )
            )
        session.commit()
        session.refresh(user)
        logger.info(
            "Locked fixture tenant seeded: %s (encryption_required=TRUE, no envelope)",
            FIXTURE_EMAIL,
        )
    return user
