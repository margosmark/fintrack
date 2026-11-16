"""tags — CRUD shapes (docs/08, docs/02)."""

from pydantic import BaseModel, ConfigDict, Field


class TagCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


class TagUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


class TagPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str


class TagWithUsage(TagPublic):
    """`GET /tags` item (M104): the management surface needs to know how much
    a tag is used BEFORE offering a delete — the 409 `tag_in_use` guard is the
    backstop, this is the affordance. `TagPublic` itself stays three fields so
    the tag objects embedded on `TransactionPublic.tags` are unchanged."""

    transaction_count: int = 0
    rule_count: int = 0


class TagPage(BaseModel):
    items: list[TagWithUsage]
    total: int
    limit: int
    offset: int


class TagMergeRequest(BaseModel):
    """M104: fold `source_ids` into `target_id` (the typo case — "Goceries" and
    "Groceries" both exist, so a rename would 409 `duplicate_tag`)."""

    model_config = ConfigDict(extra="forbid")

    source_ids: list[int] = Field(min_length=1)
    target_id: int


class TagMergeResult(BaseModel):
    """The survivor plus every id folded into it (now gone from `tags`)."""

    model_config = ConfigDict(from_attributes=True)

    target: TagPublic
    merged_tag_ids: list[int]
