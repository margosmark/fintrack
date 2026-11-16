"""sync_agreements — open-banking consent records + status-page groundwork (docs/39).

What this table is NOT: it holds no credential, no token, no secret. `consent_id`
is the provider's own agreement reference (an opaque id we echo back), and
`account_refs` is a pure local<->provider account mapping. Provider credentials,
when an accredited adapter exists, live server-side outside E2EE — documented,
never implied otherwise (docs/39 § E2EE honesty, docs/33).
"""

import enum
from datetime import datetime

from sqlalchemy import JSON, Column, Enum, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class SyncAgreementStatus(enum.StrEnum):
    """Consent lifecycle (docs/39). `revoked` is terminal for the row."""

    active = "active"
    auth_required = "auth_required"
    revoked = "revoked"


class ProviderStatus(enum.StrEnum):
    """Health signal for the future status page (docs/39 item 4).

    On `accounts`, NULL means "this account is not linked to a live bank-sync
    agreement" — it is deliberately NOT a fourth health state, so a UI reads
    NULL as "no provider" rather than "unknown".
    """

    ok = "ok"
    degraded = "degraded"
    auth_required = "auth_required"


class SyncAgreement(TimestampMixin, table=True):
    __tablename__ = "sync_agreements"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "consent_id", name="uq_sync_agreement_consent"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    # Registry slug (docs/39 § Provider registry), e.g. "stub".
    provider: str
    # The provider's agreement id. Not a credential (see module docstring).
    consent_id: str
    status: SyncAgreementStatus = Field(
        default=SyncAgreementStatus.active,
        sa_column=Column(Enum(SyncAgreementStatus, native_enum=False, length=32), nullable=False),
    )
    # provider account ref -> local `accounts.id`. JSON rather than a junction
    # table: it is read and written only through `services/sync`, and tenancy +
    # export scoping ride on `user_id` exactly like every other domain table.
    # Dangling refs (an account deleted between pulls) are skipped at pull time.
    account_refs: dict[str, int] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # Status-page groundwork (docs/39 item 4): when this agreement last pulled.
    last_sync_at: datetime | None = None
    provider_status: ProviderStatus | None = Field(
        default=None,
        sa_column=Column(Enum(ProviderStatus, native_enum=False, length=32), nullable=True),
    )
    # Last provider failure, truncated for the status page (docs/39 error codes).
    last_error: str | None = None
    revoked_at: datetime | None = None
