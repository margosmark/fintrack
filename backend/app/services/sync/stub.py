"""Stub open-banking provider — the drop-in target the accredited adapter replaces.

No network, no credentials, no third party: a deterministic in-memory dataset
that exercises the FULL seam (consent -> pull -> delta -> revoke) so the
aggregator adapter is a config change rather than a re-architecture
(docs/39 § Stub provider).

Account addressing: the stub mints `{slug}-acc-{index}` per requested account,
where `index` is the position in the `create_agreement` call. Fixture rows whose
`account_ref` is not in the grant are dropped — that is the scoping rule the
real adapter has to honour too.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date as dt_date
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models.sync_agreements import ProviderStatus
from app.services.sync.provider import ProviderAuthRequired, ProviderRevoked
from app.services.sync.types import (
    AgreementGrant,
    NormalizedBalance,
    NormalizedTx,
    ProviderAccount,
    ProviderInstitution,
    ProviderUser,
    PullBatch,
)

# --- default dataset (also mirrored by backend/tests/fixtures/sync_stub_*.json) ---

DEFAULT_INSTITUTIONS: tuple[ProviderInstitution, ...] = (
    ProviderInstitution(ref="stub-bank", name="Stub Bank (test provider)"),
    ProviderInstitution(ref="stub-credit-union", name="Stub Credit Union (test provider)"),
)

DEFAULT_TRANSACTIONS: tuple[NormalizedTx, ...] = (
    NormalizedTx(
        account_ref="stub-acc-0",
        external_id="TX-1001",
        date=dt_date(2025, 11, 3),
        amount_native=Decimal("-42.35"),
        description="CORNER STORE #1234 TORONTO ON",
        currency="CAD",
    ),
    NormalizedTx(
        account_ref="stub-acc-0",
        external_id="TX-1002",
        date=dt_date(2025, 11, 4),
        amount_native=Decimal("2500.00"),
        description="ACME CORP PAYROLL DEPOSIT",
        currency="CAD",
    ),
    NormalizedTx(
        account_ref="stub-acc-0",
        external_id="TX-1003",
        date=dt_date(2025, 11, 5),
        amount_native=Decimal("-12.99"),
        description="NETFLIX SUBSCRIPTION",
        currency=None,  # falls back to the account currency (docs/03)
        posting_date=dt_date(2025, 11, 6),
    ),
    NormalizedTx(
        account_ref="stub-acc-1",
        external_id="TX-1004",
        date=dt_date(2025, 11, 6),
        amount_native=Decimal("-60.00"),
        description="SHELL STATION 4471 MISSISSAUGA ON",
        currency="CAD",
    ),
    # Addressed to an account no grant ever covers: must never be pulled.
    NormalizedTx(
        account_ref="stub-acc-9",
        external_id="TX-9999",
        date=dt_date(2025, 11, 7),
        amount_native=Decimal("-5.00"),
        description="UNMAPPED ACCOUNT ROW",
        currency="CAD",
    ),
)

DEFAULT_BALANCES: tuple[NormalizedBalance, ...] = (
    NormalizedBalance(
        account_ref="stub-acc-0",
        amount=Decimal("1500.00"),
        currency="CAD",
        as_of=dt_date(2025, 11, 6),
    ),
    NormalizedBalance(
        account_ref="stub-acc-1",
        amount=Decimal("300.00"),
        currency="CAD",
        as_of=dt_date(2025, 11, 6),
    ),
)

SCOPES: tuple[str, ...] = ("accounts", "transactions")


class _StubGrant:
    """The provider's own record of a consent — the defence-in-depth half of
    "revoke stops pulls": even if our DB still said `active`, the stub refuses."""

    __slots__ = ("consent_id", "refs", "revoked")

    def __init__(self, consent_id: str, refs: dict[str, int], revoked: bool = False) -> None:
        self.consent_id = consent_id
        self.refs = refs
        self.revoked = revoked


class StubProvider:
    """Deterministic `Provider` implementation (docs/39)."""

    def __init__(
        self,
        *,
        slug: str = "stub",
        display_name: str = "Stub Bank (test provider)",
        institutions: Sequence[ProviderInstitution] = DEFAULT_INSTITUTIONS,
        transactions: Sequence[NormalizedTx] = DEFAULT_TRANSACTIONS,
        balances: Sequence[NormalizedBalance] = DEFAULT_BALANCES,
    ) -> None:
        self.slug = slug
        self.display_name = display_name
        self.institutions = tuple(institutions)
        self.transactions = tuple(transactions)
        self.balances = tuple(balances)
        self._grants: dict[int, _StubGrant] = {}
        self._seq = 0
        # Test knobs (reset() clears them): simulate a degraded pull or a
        # transport failure without monkeypatching the class.
        self.pull_status = ProviderStatus.ok
        self.next_pull_error: Exception | None = None

    # --- fixtures -----------------------------------------------------------

    @classmethod
    def from_fixture(cls, path: Path | str, **kwargs: Any) -> StubProvider:
        """Build a provider from a JSON fixture (fixtures -> NormalizedTx)."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            institutions=tuple(
                ProviderInstitution(
                    ref=row["ref"], name=row["name"], country=row.get("country", "CA")
                )
                for row in payload.get("institutions", [])
            ),
            transactions=tuple(
                NormalizedTx(
                    account_ref=row["account_ref"],
                    external_id=row["external_id"],
                    date=dt_date.fromisoformat(row["date"]),
                    amount_native=Decimal(row["amount_native"]),
                    description=row["description"],
                    currency=row.get("currency"),
                    posting_date=(
                        dt_date.fromisoformat(row["posting_date"])
                        if row.get("posting_date")
                        else None
                    ),
                )
                for row in payload.get("transactions", [])
            ),
            balances=tuple(
                NormalizedBalance(
                    account_ref=row["account_ref"],
                    amount=Decimal(row["amount"]),
                    currency=row["currency"],
                    as_of=dt_date.fromisoformat(row["as_of"]),
                )
                for row in payload.get("balances", [])
            ),
            **kwargs,
        )

    def reset(self) -> None:
        """Forget every grant and clear the test knobs (tests use this)."""
        self._grants.clear()
        self._seq = 0
        self.pull_status = ProviderStatus.ok
        self.next_pull_error = None

    # --- Provider protocol --------------------------------------------------

    def list_institutions(self) -> Sequence[ProviderInstitution]:
        return self.institutions

    def create_agreement(
        self, user: ProviderUser, accounts: Sequence[ProviderAccount]
    ) -> AgreementGrant:
        self._seq += 1
        refs = {f"{self.slug}-acc-{index}": acct.local_id for index, acct in enumerate(accounts)}
        consent_id = f"{self.slug}-consent-{user.id}-{self._seq}"
        self._grants[user.id] = _StubGrant(consent_id=consent_id, refs=refs)
        return AgreementGrant(consent_id=consent_id, scopes=SCOPES, account_refs=refs)

    def pull_delta(self, user: ProviderUser, since: datetime | None) -> PullBatch:
        if self.next_pull_error is not None:
            raise self.next_pull_error
        grant = self._grants.get(user.id)
        if grant is None:
            raise ProviderAuthRequired("stub: no consent on record for this user")
        if grant.revoked:
            raise ProviderRevoked("stub: consent was revoked")
        lower = since.date() if since is not None else None
        txns = tuple(
            txn
            for txn in self.transactions
            if txn.account_ref in grant.refs and (lower is None or txn.date > lower)
        )
        balances = tuple(bal for bal in self.balances if bal.account_ref in grant.refs)
        return PullBatch(
            transactions=txns,
            balances=balances,
            provider_status=self.pull_status,
            cursor=None,
        )

    def revoke(self, user: ProviderUser) -> None:
        grant = self._grants.get(user.id)
        if grant is None:
            self._grants[user.id] = _StubGrant(consent_id="", refs={}, revoked=True)
            return
        grant.revoked = True


_DEFAULT = StubProvider()


def default_stub() -> StubProvider:
    """The singleton registered by `app.services.sync` under slug `stub`."""
    return _DEFAULT
