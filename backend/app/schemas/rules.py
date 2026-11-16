"""rules — CRUD shapes (docs/08, docs/02)."""

from datetime import date as dt_date

from pydantic import BaseModel, ConfigDict, Field

from app.models.rules import RuleMatchType
from app.schemas.tags import TagPublic


class RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_type: RuleMatchType
    pattern: str = Field(min_length=1)
    priority: int
    category_id: int
    enabled: bool = True
    tag_ids: list[int] = []
    tag_action: str = Field(default="set", pattern=r"^(set|add)$")
    stop_on_match: bool = True


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_type: RuleMatchType | None = None
    pattern: str | None = Field(default=None, min_length=1)
    priority: int | None = None
    category_id: int | None = None
    enabled: bool | None = None
    tag_ids: list[int] | None = None
    tag_action: str | None = Field(default=None, pattern=r"^(set|add)$")
    stop_on_match: bool | None = None


class RulePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    match_type: RuleMatchType
    pattern: str
    priority: int
    category_id: int
    enabled: bool
    tag_action: str = "set"
    stop_on_match: bool = True
    tags: list[TagPublic] = []


class RulePage(BaseModel):
    items: list[RulePublic]
    total: int
    limit: int
    offset: int


# M48-S3: the `POST /rules/preview` route is GONE — preview scores rows the
# browser holds and runs in `frontend/src/lib/s3Engine.ts::previewRule`
# (docs/31). These three models stay because they are the reference contract
# the TS port mirrors field-for-field: the golden fixture
# `frontend/tests/fixtures/s3-vectors.json` pins their exact request
# validation envelopes and response shape.
# M137 P1-2 (docs/33 §Server-first computing): `app/api/rules.py` mounts
# `RulePreviewRequest`/`RulePreviewResponse` again — the re-mount reuses this
# contract unchanged so the client twin and the server route are still
# field-for-field comparable during the parity pass.


class RulePreviewRequest(BaseModel):
    """Dry-run a rule draft (docs/31): match fields + scope, never writes."""

    model_config = ConfigDict(extra="forbid")

    match_type: RuleMatchType
    pattern: str = Field(min_length=1)
    from_: dt_date | None = Field(default=None, alias="from")
    to: dt_date | None = None
    only_unassigned: bool = True
    limit: int = Field(default=25, ge=1, le=100)


class RulePreviewItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    date: dt_date
    payee: str
    amount_native: str
    category_id: int | None
    category_name: str | None


class RulePreviewResponse(BaseModel):
    matched_count: int
    assigned_skipped: int = 0
    items: list[RulePreviewItem]


class RuleApplyRequest(BaseModel):
    """Targeted backfill of one rule (docs/31)."""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(pattern=r"^(all|range|forward)$")
    from_: dt_date | None = Field(default=None, alias="from")
    to: dt_date | None = None
    overwrite: bool = False


class RuleApplyResponse(BaseModel):
    rule_id: int
    mode: str
    matched_count: int
    applied_count: int
    note: str | None = None
