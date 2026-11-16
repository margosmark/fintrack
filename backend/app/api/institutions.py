"""institutions — standard CRUD + validated CSV import profiles (docs/03, docs/08)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.deps import (
    Pagination,
    get_current_user,
    get_db,
    get_pagination,
    owned_or_404,
    paginate,
)
from app.core.errors import api_error
from app.models.accounts import Account
from app.models.institutions import Institution
from app.models.users import User
from app.schemas.institutions import (
    InstitutionCreate,
    InstitutionPage,
    InstitutionPublic,
    InstitutionUpdate,
)

router = APIRouter(prefix="/institutions", tags=["institutions"])


@router.get("", response_model=InstitutionPage)
def list_institutions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> InstitutionPage:
    scope = Institution.user_id == current_user.id
    stmt = select(Institution).where(scope).order_by(Institution.id)
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    return InstitutionPage(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("", response_model=InstitutionPublic, status_code=201)
def create_institution(
    payload: InstitutionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Institution:
    # payload.importer_profile is already a CsvProfile: unknown keys rejected (docs/03).
    institution = Institution(
        user_id=current_user.id,
        name=payload.name,
        importer_profile=(
            payload.importer_profile.model_dump() if payload.importer_profile else None
        ),
    )
    db.add(institution)
    db.commit()
    db.refresh(institution)
    return institution


@router.get("/{institution_id}", response_model=InstitutionPublic)
def get_institution(
    institution_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Institution:
    return owned_or_404(Institution, institution_id, current_user, db)


@router.patch("/{institution_id}", response_model=InstitutionPublic)
def update_institution(
    institution_id: int,
    payload: InstitutionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Institution:
    institution = owned_or_404(Institution, institution_id, current_user, db)
    data = payload.model_dump(exclude_unset=True)
    if "importer_profile" in data and data["importer_profile"] is not None:
        data["importer_profile"] = payload.importer_profile.model_dump()
    for field, value in data.items():
        setattr(institution, field, value)
    db.commit()
    db.refresh(institution)
    return institution


@router.delete("/{institution_id}", status_code=204)
def delete_institution(
    institution_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:

    institution = owned_or_404(Institution, institution_id, current_user, db)
    referenced = db.exec(
        select(Account.id).where(Account.institution_id == institution_id).limit(1)
    ).first()
    if referenced is not None:
        raise api_error(409, "Institution has accounts; reassign them first", "institution_in_use")
    db.delete(institution)
    db.commit()
