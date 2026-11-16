"""Shared created_at / updated_at mixin (docs/02 conventions).

Every domain table carries both columns: timezone-aware, server default now(),
updated_at bumped on UPDATE via a SQL-level onupdate expression.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlmodel import Field, SQLModel


class TimestampMixin(SQLModel):
    created_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
    )
