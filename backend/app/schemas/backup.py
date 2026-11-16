"""Backup schemas — scheduled client-side-encrypted backup (M56, docs/11).

Request models are deliberately LOOSE where the spec pins a *format*: the
domain checks live in the handler so the error envelope carries the spec's
code (`backup_invalid_period`, `backup_invalid_schedule`, …) instead of a
generic `validation_error`. Pydantic still owns types, and `extra="forbid"`
is on every model — a typo'd field is a loud 422, never silently dropped
(same rule as the vault router).
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.users import BackupFrequency


class ScheduleRead(BaseModel):
    """The five schedule columns as stored (docs/11 § Schedule model)."""

    frequency: BackupFrequency
    hour: int
    weekday: int
    day_of_month: int
    keep_last_n: int


class ScheduleUpdate(BaseModel):
    """Full replace of the schedule — partial patches cannot express intent."""

    model_config = ConfigDict(extra="forbid")

    # Plain `str`, not the enum: an unknown value must fail as
    # 422 `backup_invalid_schedule` (the spec's code), not as a Pydantic
    # validation_error (docs/11 error table).
    frequency: str
    hour: int
    weekday: int
    day_of_month: int
    keep_last_n: int


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: str
    # No max_length / no pattern here: oversize and a bad format are domain
    # errors (`backup_too_large`, `backup_invalid_period`) raised in the handler.
    ciphertext: str


class ArtifactPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    period: str
    created_at: datetime | None
    size_bytes: int


class ArtifactDetail(ArtifactPublic):
    ciphertext: str


class ArtifactList(BaseModel):
    items: list[ArtifactPublic]


class BackupPayload(BaseModel):
    """The DECRYPTED artifact payload — what the browser posts for restore.

    Every field defaults so a malformed body reaches the handler and fails
    with the spec's `restore_invalid_payload` instead of a type error.
    """

    model_config = ConfigDict(extra="forbid")

    v: int = 0
    kind: str = ""
    period: str = ""
    exported_at: str = ""
    snapshot: Any = None


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str
    payload: BackupPayload


class RestoreResponse(BaseModel):
    restored: dict[str, int]
    absent_tables: list[str]
    skipped_tables: list[str]
    fx_rates_inserted: int
