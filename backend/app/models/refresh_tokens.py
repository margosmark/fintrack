"""refresh_tokens — auth plumbing; SHA-256 hashes only, rotated on every refresh (docs/02)."""

from datetime import datetime

from sqlalchemy import DateTime, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class RefreshToken(TimestampMixin, table=True):
    __tablename__ = "refresh_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token_hash: str
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    revoked_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
