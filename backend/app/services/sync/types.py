"""Provider-facing value objects for the bank-sync seam (docs/39).

These are deliberately dumb: frozen dataclasses, no ORM, no FastAPI, no DB.
A provider implementation may only be handed a `ProviderUser` / `ProviderAccount`
projection — never a `User` / `Account` row — so the seam's data-leak surface is
pinned by the type signature, not by a reviewer's memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as dt_date
from datetime import datetime
from decimal import Decimal

from app.models.sync_agreements import ProviderStatus


@dataclass(frozen=True)
class ProviderUser:
    """The only user data that may cross the seam.

    Deliberately carries the opaque tenant id and nothing else: no email, no
    name, no currency preference. An accredited adapter authenticates with the
    consent it was issued, not with anything about the person.
    """

    id: int


@dataclass(frozen=True)
class ProviderAccount:
    """The only account data that may cross the seam on consent creation.

    `name` is excluded on purpose: it is user PII and may be E2EE ciphertext
    (`accounts.name_cipher`). Mapping local <-> provider accounts is the
    adapter's job, reported back through `AgreementGrant.account_refs`.
    """

    local_id: int
    currency: str
    account_type: str


@dataclass(frozen=True)
class ProviderInstitution:
    """An institution the provider can serve (`list_institutions`)."""

    ref: str
    name: str
    country: str = "CA"


@dataclass(frozen=True)
class AgreementGrant:
    """Provider's answer to `create_agreement`.

    `account_refs` maps the provider's opaque account reference back to the
    local `accounts.id` the caller passed in — the adapter owns that mapping,
    and every `NormalizedTx.account_ref` must be one of these keys or the row
    is dropped at pull time.
    """

    consent_id: str
    scopes: tuple[str, ...]
    account_refs: dict[str, int]


@dataclass(frozen=True)
class NormalizedTx:
    """A provider transaction in our own terms — the `RawTransaction` twin.

    Sign convention matches the importers (docs/03): signed, account currency,
    debits negative.
    """

    account_ref: str
    external_id: str
    date: dt_date
    amount_native: Decimal
    description: str
    currency: str | None = None
    posting_date: dt_date | None = None


@dataclass(frozen=True)
class NormalizedBalance:
    """An observed balance.

    Reported to the caller and NEVER written into `accounts.starting_balance`
    or any derived total — manual data wins (AGENTS §2) and a pulled balance is
    an observation, not a user assertion (docs/39 § What a pull does not do).
    """

    account_ref: str
    amount: Decimal
    currency: str
    as_of: dt_date


@dataclass(frozen=True)
class PullBatch:
    """Result of one `pull_delta` call."""

    transactions: tuple[NormalizedTx, ...] = ()
    balances: tuple[NormalizedBalance, ...] = ()
    provider_status: ProviderStatus = ProviderStatus.ok
    # Opaque cursor the adapter would use on the next call. The service records
    # it as `last_sync_at`; a provider that ignores `since` is still safe
    # because staging dedups (docs/39 § Parity).
    cursor: datetime | None = None
