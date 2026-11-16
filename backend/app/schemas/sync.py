"""sync — API shapes for the open-banking seam (docs/39, docs/08)."""

from datetime import date as dt_date
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.sync_agreements import ProviderStatus, SyncAgreement, SyncAgreementStatus
from app.schemas.imports import PreviewRow


class ProviderPublic(BaseModel):
    slug: str
    display_name: str


class ProviderInstitutionPublic(BaseModel):
    ref: str
    name: str
    country: str


class CreateAgreementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    account_ids: list[int]


class AgreementPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    consent_id: str
    status: SyncAgreementStatus
    # Local account ids this consent covers (values of `account_refs`).
    account_ids: list[int]
    scopes: list[str]
    provider_status: ProviderStatus | None
    last_sync_at: datetime | None
    last_error: str | None
    created_at: datetime | None
    revoked_at: datetime | None

    @model_validator(mode="before")
    @classmethod
    def _derive_account_ids(cls, data: Any) -> Any:
        """`account_ids` has no ORM attribute: it is the VALUES of `account_refs`.

        This runs first whenever an ORM row is validated, which is what lets
        `AgreementPublic` honestly claim `from_attributes` like every other
        Public schema in the repo — and keeps `agreement_public()` a one-liner
        instead of a second hand-rolled builder that could drift.
        """
        if isinstance(data, SyncAgreement):
            return {
                "id": data.id,
                "provider": data.provider,
                "consent_id": data.consent_id,
                "status": data.status,
                "account_ids": sorted(set(data.account_refs.values())),
                "scopes": list(data.scopes),
                "provider_status": data.provider_status,
                "last_sync_at": data.last_sync_at,
                "last_error": data.last_error,
                "created_at": data.created_at,
                "revoked_at": data.revoked_at,
            }
        return data


def agreement_public(agreement: SyncAgreement) -> AgreementPublic:
    """One builder for the projection — the schema derives `account_ids` itself
    (see `_derive_account_ids`), so `model_validate` and this helper cannot
    drift apart."""
    return AgreementPublic.model_validate(agreement)


class SyncBalancePublic(BaseModel):
    """Observed balance — reported by a pull, never written to any balance field."""

    account_id: int
    amount: str
    currency: str
    as_of: dt_date


class PullResponse(BaseModel):
    agreement: AgreementPublic
    # One staged `pending_review` batch per account that received rows; empty
    # when the pull produced nothing new (nothing is staged then either).
    staged_batch_ids: list[int]
    preview: list[PreviewRow]
    rows: int
    imported: int
    duplicates_skipped: int
    unresolved: int
    balances: list[SyncBalancePublic]
    # Rows the provider addressed outside this consent's account refs.
    dropped_rows: int
