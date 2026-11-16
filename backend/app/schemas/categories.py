"""categories — CRUD shapes (docs/08, docs/02)."""

from pydantic import BaseModel, ConfigDict, Field


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    parent_id: int | None = None


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    parent_id: int | None = None


class CategoryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    parent_id: int | None
    is_system: bool


class CategoryPage(BaseModel):
    items: list[CategoryPublic]
    total: int
    limit: int
    offset: int
