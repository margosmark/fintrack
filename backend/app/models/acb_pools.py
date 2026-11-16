"""acb_pools — non-registered ACB cache; derived state rebuilt ONLY via app/tax/acb.py (docs/02)."""

from decimal import Decimal

from sqlalchemy import Numeric, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class AcbPool(TimestampMixin, table=True):
    __tablename__ = "acb_pools"
    __table_args__ = (
        UniqueConstraint("account_id", "security_id", name="uq_acb_pools_account_security"),
    )

    id: int | None = Field(default=None, primary_key=True)
    # indexed: every domain read scopes by user_id (docs/02 §tenancy);
    # see alembic 0050_tenancy_idx
    user_id: int = Field(foreign_key="users.id", index=True)
    account_id: int = Field(foreign_key="accounts.id")
    security_id: int = Field(foreign_key="securities.id")
    quantity: Decimal = Field(sa_type=Numeric(18, 6))
    total_acb_cad: Decimal = Field(sa_type=Numeric(18, 2))
