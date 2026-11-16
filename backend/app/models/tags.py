"""tags — user-created labels for transactions beyond the single category FK (docs/02)."""

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class Tag(TimestampMixin, table=True):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tags_user_name"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    name: str = Field(max_length=100)
