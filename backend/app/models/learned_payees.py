"""learned_payees — suggestion-accept learning map (docs/02, docs/04).

Written ONLY by the suggestion-accept path; no CRUD routes.
"""

from sqlmodel import Field, UniqueConstraint

from app.models.mixins import TimestampMixin


class LearnedPayee(TimestampMixin, table=True):
    __tablename__ = "learned_payees"
    __table_args__ = (
        UniqueConstraint("user_id", "normalized_payee", name="uq_learned_payees_user_payee"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    normalized_payee: str
    category_id: int = Field(foreign_key="categories.id")
    hit_count: int = 1
