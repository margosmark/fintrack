"""institutions — bank/brokerage containers with a CSV import profile (docs/02, docs/03)."""

from sqlalchemy import JSON, Column
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class Institution(TimestampMixin, table=True):
    __tablename__ = "institutions"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str
    importer_profile: dict | None = Field(default=None, sa_column=Column(JSON))
