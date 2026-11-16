"""categories — self-referencing hierarchy; per-user copies of the system defaults (docs/02)."""

from sqlmodel import Field

from app.models.mixins import TimestampMixin


class Category(TimestampMixin, table=True):
    __tablename__ = "categories"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str
    parent_id: int | None = Field(default=None, foreign_key="categories.id")
    # System rows are seeded at register time; they may be renamed but not deleted.
    is_system: bool = False
