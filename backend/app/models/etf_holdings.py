"""etf_holdings — top holdings of ETFs / mutual funds (M17, docs/02).

Composition rows per fund security: asset class, region, sector and
weight. `source=manual` rows are replaced only via PUT; `source=yfinance`
rows only via the import endpoint, so a market-data re-import never
destroys hand-curated composition (manual-first convention).
"""

import enum
from decimal import Decimal

from sqlalchemy import CheckConstraint, Column, Enum, Numeric
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class EtfAssetClass(enum.StrEnum):
    stock = "stock"
    bond = "bond"
    cash = "cash"
    commodity = "commodity"
    crypto = "crypto"
    other = "other"


# One canonical source enum shared with fundamentals (docs/02): the same
# provenances apply to every market-data-sourced row. M147 adds
# `issuer` — a full-holdings CSV from the fund's own issuer (iShares
# `latest-holdings.csv` et al); M147-followup adds `vanguard` — full
# holdings from Vanguard's own first-party APIs (the US fund-profile JSON
# and the Canadian `gpx/graphql` gateway, docs/07 §Fund composition
# providers). Provider rows are replaced TOGETHER by a re-import (only
# `manual` rows are permanent), so the exposure engine never
# double-counts one fund's weights across two providers. Plain
# VARCHAR(16), no DB constraint (docs/02).
class EtfHoldingSource(enum.StrEnum):
    yfinance = "yfinance"
    manual = "manual"
    issuer = "issuer"
    vanguard = "vanguard"


class EtfHolding(TimestampMixin, table=True):
    __tablename__ = "etf_holdings"
    # Matches migration 0006 (`ck_etf_holdings_weight`): a composition row
    # without a positive weight is meaningless.
    __table_args__ = (
        CheckConstraint("weight_pct > 0 AND weight_pct <= 100", name="ck_etf_holdings_weight"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    security_id: int = Field(foreign_key="securities.id")
    holding_security_id: int | None = Field(default=None, foreign_key="securities.id")
    holding_symbol: str | None = Field(default=None, max_length=20)
    holding_name: str = Field(max_length=160)
    asset_class: EtfAssetClass = Field(
        default=EtfAssetClass.other,
        sa_column=Column(
            Enum(EtfAssetClass, native_enum=False, length=16),
            nullable=False,
            server_default="other",
        ),
    )
    region: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=80)
    sector: str | None = Field(default=None, max_length=80)
    weight_pct: Decimal = Field(sa_column=Column(Numeric(7, 4), nullable=False))
    source: EtfHoldingSource = Field(
        sa_column=Column(Enum(EtfHoldingSource, native_enum=False, length=16), nullable=False)
    )
