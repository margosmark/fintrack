"""sync — open-banking readiness endpoints: consent, pull, status, revoke (docs/39).

Every mutating route is rate-limited human-paced (docs/39 § Rate limits) and
inherits the demo write boundary from `get_current_user`. Reads stay open for
the demo tenant exactly like the rest of the API.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.errors import api_error
from app.core.ratelimit import SYNC_MUTATE_LIMIT, human_paced_limit
from app.models.users import User
from app.schemas.imports import PreviewRow
from app.schemas.sync import (
    AgreementPublic,
    CreateAgreementBody,
    ProviderInstitutionPublic,
    ProviderPublic,
    PullResponse,
    SyncBalancePublic,
    agreement_public,
)
from app.services.sync import service
from app.services.sync.provider import list_providers

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/providers", response_model=list[ProviderPublic])
def providers(
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ProviderPublic]:
    """Registered providers (docs/39 § Provider registry)."""
    return [ProviderPublic(slug=p.slug, display_name=p.display_name) for p in list_providers()]


@router.get("/providers/{slug}/institutions", response_model=list[ProviderInstitutionPublic])
def institutions(
    slug: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ProviderInstitutionPublic]:
    """Institutions the provider serves — a future picker's data source."""
    return [
        ProviderInstitutionPublic(ref=row.ref, name=row.name, country=row.country)
        for row in service.list_institutions(db, slug)
    ]


@router.post("/agreements", response_model=AgreementPublic, status_code=201)
@human_paced_limit(SYNC_MUTATE_LIMIT)
def create_agreement(
    request: Request,
    body: CreateAgreementBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AgreementPublic:
    """Register consent covering `account_ids` (docs/39 § Consent)."""
    agreement = service.create_agreement(db, current_user, body.provider, body.account_ids)
    return agreement_public(agreement)


@router.get("/agreements", response_model=list[AgreementPublic])
def list_agreements(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[AgreementPublic]:
    return [agreement_public(row) for row in service.list_agreements(db, current_user)]


@router.get("/agreements/{agreement_id}", response_model=AgreementPublic)
def get_agreement(
    agreement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AgreementPublic:
    return agreement_public(service.get_agreement(db, current_user, agreement_id))


@router.post("/agreements/{agreement_id}/pull", response_model=PullResponse)
@human_paced_limit(SYNC_MUTATE_LIMIT)
def pull(
    request: Request,
    agreement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PullResponse:
    """Pull deltas and stage them into `pending_review` batches (docs/39 § Pull)."""
    # M181 (docs/33 § "OUTSIDE THE ENVELOPE (M79)"): a pull stages the SAME
    # mandatory-plaintext `transactions` columns a file upload does, and it runs
    # server-side, so it can never produce the ciphers `encryption_required`
    # demands. Fail closed BEFORE `service.pull` — no provider call is made and
    # nothing is staged. Keying on the CALLER's own flag leaks nothing about
    # which agreement ids exist. Consent setup and `revoke` stay available: a
    # pull is disabled, not the agreement.
    if current_user.encryption_required:
        raise api_error(
            422,
            "Pulling transactions from a bank provider is unavailable while "
            "client-side encryption is enabled: this path runs server-side and "
            "cannot safely write encrypted data, so nothing was saved",
            "encryption_required",
        )
    outcome = service.pull(db, current_user, agreement_id)
    return PullResponse(
        agreement=agreement_public(outcome.agreement),
        staged_batch_ids=outcome.batch_ids,
        preview=[
            PreviewRow(
                id=row.txn_id,
                date=row.raw.date,
                amount_native=row.raw.amount_native,
                currency=row.currency,
                raw_description=row.raw.raw_description,
                duplicate=row.duplicate,
                cad_amount=row.cad_amount,
            )
            for row in outcome.rows
        ],
        rows=len(outcome.rows),
        imported=outcome.imported,
        duplicates_skipped=outcome.duplicates_skipped,
        unresolved=outcome.unresolved,
        balances=[
            SyncBalancePublic(
                account_id=bal.account_id,
                amount=str(bal.amount),
                currency=bal.currency,
                as_of=bal.as_of,
            )
            for bal in outcome.balances
        ],
        dropped_rows=outcome.dropped_rows,
    )


@router.post("/agreements/{agreement_id}/revoke", response_model=AgreementPublic)
@human_paced_limit(SYNC_MUTATE_LIMIT)
def revoke(
    request: Request,
    agreement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AgreementPublic:
    """Disconnect: local revocation always wins, provider failure is recorded."""
    return agreement_public(service.revoke(db, current_user, agreement_id))
