"""watchlist — securities tracked but not held (W11, user request).

A user follows symbols they do not (yet) own; watchlist rows gate the
price-refresh fan-out and the Investments page watchlist card. UNIQUE per
(user, security) — duplicates answer 409 {"code": "already_watched"}.
"""

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class WatchlistItem(TimestampMixin, table=True):
    __tablename__ = "watchlist"
    __table_args__ = (
        UniqueConstraint("user_id", "security_id", name="uq_watchlist_user_security"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    security_id: int = Field(foreign_key="securities.id")
