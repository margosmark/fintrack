"""rules — priority-ordered categorization rules; priority ascending,
first match wins unless the rule lets processing continue (docs/02, docs/04).
"""

import enum

from sqlalchemy import Boolean, CheckConstraint, Column, Enum, String
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class RuleMatchType(enum.StrEnum):
    regex = "regex"
    exact_payee = "exact_payee"
    contains = "contains"
    amount_equals = "amount_equals"
    amount_gt = "amount_gt"
    amount_lt = "amount_lt"
    amount_between = "amount_between"


class Rule(TimestampMixin, table=True):
    __tablename__ = "rules"
    __table_args__ = (CheckConstraint("tag_action IN ('set', 'add')", name="ck_rules_tag_action"),)

    id: int | None = Field(default=None, primary_key=True)
    # indexed: every domain read scopes by user_id (docs/02 §tenancy);
    # see alembic 0050_tenancy_idx
    user_id: int = Field(foreign_key="users.id", index=True)
    match_type: RuleMatchType = Field(
        sa_column=Column(Enum(RuleMatchType, native_enum=False, length=32), nullable=False)
    )
    pattern: str
    priority: int
    category_id: int = Field(foreign_key="categories.id")
    enabled: bool = True
    tag_action: str = Field(
        default="set",
        sa_column=Column(String(10), nullable=False, server_default="set"),
    )
    stop_on_match: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default="true"),
    )
