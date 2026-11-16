"""vault — wrapped-DEK sync across devices (E2EE Phase 4, docs/33).

The server stores ONE opaque, passphrase-locked key blob per browser profile.
It never sees the passphrase, the KEK or the DEK, and it never decodes
`wrapped_blob` — the only things it enforces are size, shape and tenancy.
Revocation is a soft delete that drops the blob, so the copy the database held
stops existing (ck_vault_envelopes_state).

Contract (docs/08 § Vault key sync (M53)): every failure is a {detail, code}
envelope; tenancy failures are 404 `not_found` with no existence hint.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import get_current_user, get_db
from app.core.errors import api_error
from app.models.users import User
from app.models.vault import VaultEnvelope
from app.schemas.vault import (
    VaultEnvelopeCreate,
    VaultEnvelopeList,
    VaultEnvelopePublic,
    VaultEnvelopeUpdate,
)

router = APIRouter(prefix="/vault", tags=["vault"])

# Pinned in docs/33 Phase 4 + docs/08: the endpoint can never become
# unbounded storage, and one person does not accumulate endless rows.
MAX_ACTIVE_ENVELOPES = 10
# Same ceiling as the Phase-2 cipher-column check (`invalid_cipher`).
MAX_BLOB_CHARS = 8000


def _assert_blob_size(blob: str) -> None:
    if len(blob) > MAX_BLOB_CHARS:
        raise api_error(
            422,
            f"wrapped_blob exceeds {MAX_BLOB_CHARS} characters",
            "envelope_too_large",
        )


def _active_envelopes(db: Session, user_id: int) -> list[VaultEnvelope]:
    """Active rows for one user, oldest first — enrollment picks items[0]."""
    return list(
        db.exec(
            select(VaultEnvelope)
            .where(VaultEnvelope.user_id == user_id, VaultEnvelope.revoked_at.is_(None))
            .order_by(VaultEnvelope.created_at, VaultEnvelope.id)
        ).all()
    )


def _owned_envelope(db: Session, envelope_id: int, user: User) -> VaultEnvelope:
    row = db.get(VaultEnvelope, envelope_id)
    # Foreign and unknown ids are deliberately indistinguishable (404 both).
    if row is None or row.user_id != user.id:
        raise api_error(404, "Vault envelope not found", "not_found")
    return row


@router.get("/envelopes", response_model=VaultEnvelopeList)
def list_envelopes(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    device_id: Annotated[str, Query(min_length=32, max_length=64, pattern=r"^[0-9a-f]{32}$")],
) -> VaultEnvelopeList:
    items = _active_envelopes(db, current_user.id)
    active_for_device = any(row.device_id == device_id for row in items)
    rows_for_device = db.exec(
        select(func.count())
        .select_from(VaultEnvelope)
        .where(
            VaultEnvelope.user_id == current_user.id,
            VaultEnvelope.device_id == device_id,
        )
    ).one()
    # The revocation signal: "you had a row here and none is active any more".
    # A brand-new device has NO rows at all and must still be able to enroll.
    revoked_own_device = not active_for_device and int(rows_for_device) > 0
    return VaultEnvelopeList(
        items=[VaultEnvelopePublic.model_validate(row) for row in items],
        revoked_own_device=revoked_own_device,
        active_count=len(items),
    )


@router.post("/envelopes", response_model=VaultEnvelopePublic, status_code=201)
def create_envelope(
    payload: VaultEnvelopeCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> VaultEnvelope:
    _assert_blob_size(payload.wrapped_blob)
    existing = db.exec(
        select(VaultEnvelope).where(
            VaultEnvelope.user_id == current_user.id,
            VaultEnvelope.device_id == payload.device_id,
            VaultEnvelope.revoked_at.is_(None),
        )
    ).first()
    if existing is not None:
        raise api_error(409, "This device is already enrolled", "device_exists")
    if len(_active_envelopes(db, current_user.id)) >= MAX_ACTIVE_ENVELOPES:
        raise api_error(
            409,
            f"Device limit reached ({MAX_ACTIVE_ENVELOPES} active devices)",
            "too_many_devices",
        )
    row = VaultEnvelope(
        user_id=current_user.id,
        device_id=payload.device_id,
        label=payload.label,
        wrapped_blob=payload.wrapped_blob,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/envelopes/{envelope_id}", response_model=VaultEnvelopePublic)
def replace_envelope(
    envelope_id: int,
    payload: VaultEnvelopeUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> VaultEnvelope:
    row = _owned_envelope(db, envelope_id, current_user)
    if row.revoked_at is not None:
        # A revoked row is a tombstone: re-enrollment is a POST (new row).
        raise api_error(409, "This envelope was revoked", "envelope_revoked")
    _assert_blob_size(payload.wrapped_blob)
    # `label` is applied only when present in the body (exclude_unset), so a
    # rotation PUT that omits it does not erase the device's name.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/envelopes/{envelope_id}/revoke", response_model=VaultEnvelopePublic)
def revoke_envelope(
    envelope_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> VaultEnvelope:
    row = _owned_envelope(db, envelope_id, current_user)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        # Drop the copy the database held — the CHECK keeps the pair honest.
        row.wrapped_blob = None
        db.add(row)
        db.commit()
        db.refresh(row)
    # Idempotent: revoking twice returns the same tombstone.
    return row
