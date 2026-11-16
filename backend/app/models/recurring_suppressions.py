"""recurring_suppressions — "never ask me about this group again" (M96, docs/02).

A declined recurring-candidate group writes one row here. The detection read
path (`GET /transactions/recurring/candidates`) subtracts these keys before
answering, so a user's "no" is final for that group — no nagging, no re-prompt
on the next visit, no state to dismiss twice.

`group_key` is `"<merchant_key>|<cadence>"` (docs/09 §Recurring prompt): it is
deliberately amount-free so an amount drift inside the ±15% band cannot mint a
new key and re-prompt a group the user already answered. `user_id` +
`group_key` is UNIQUE, which also makes the decline idempotent.
"""

from sqlmodel import Field, UniqueConstraint

from app.models.mixins import TimestampMixin


class RecurringSuppression(TimestampMixin, table=True):
    __tablename__ = "recurring_suppressions"
    __table_args__ = (
        UniqueConstraint("user_id", "group_key", name="uq_recurring_suppressions_user_group"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    group_key: str = Field(max_length=120)
