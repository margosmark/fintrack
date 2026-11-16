"""institutions — CRUD shapes (docs/08) with validated CSV import profiles (docs/03)."""

from pydantic import BaseModel, ConfigDict, Field

from app.importers.csv_profile import CsvProfile


class InstitutionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    importer_profile: CsvProfile | None = None


class InstitutionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    importer_profile: CsvProfile | None = None


class InstitutionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    importer_profile: dict | None = None


class InstitutionPage(BaseModel):
    items: list[InstitutionPublic]
    total: int
    limit: int
    offset: int
