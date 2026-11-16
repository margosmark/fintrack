"""rule_tags — many-to-many join between rules and tags (docs/02)."""

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class RuleTag(TimestampMixin, table=True):
    __tablename__ = "rule_tags"
    __table_args__ = (UniqueConstraint("rule_id", "tag_id", name="uq_rule_tags_rule_tag"),)

    id: int | None = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="rules.id")
    tag_id: int = Field(foreign_key="tags.id")
