"""Export schemas — scheduled export (M75, docs/11 § "Scheduled export").

Request models are deliberately LOOSE where the spec pins a *format*: the
domain ranges live in the handler so the error envelope carries the spec's
code (`export_invalid_schedule`) instead of a generic `validation_error` —
the same rule M56 applied to `schemas/backup.py`, mirrored here on purpose so
the two schedules read identically. Pydantic still owns types, and
`extra="forbid"` is on every model: a typo'd field is a loud 422.
"""

from pydantic import BaseModel, ConfigDict

from app.models.users import ExportFrequency


class ScheduleRead(BaseModel):
    """The five schedule columns as stored (docs/11 § Schedule model)."""

    frequency: ExportFrequency
    hour: int
    weekday: int
    day_of_month: int
    keep_last_n: int


class ScheduleUpdate(BaseModel):
    """Full replace of the schedule — a partial patch cannot express intent."""

    model_config = ConfigDict(extra="forbid")

    # Plain `str`, not the enum: an unknown value must fail as 422
    # `export_invalid_schedule` (the spec's code), not as a Pydantic
    # validation_error (docs/11 error table).
    frequency: str
    hour: int
    weekday: int
    day_of_month: int
    keep_last_n: int
