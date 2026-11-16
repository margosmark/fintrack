"""users — accounts + server-side preference columns (docs/02, docs/11 step 1)."""

import enum

from sqlalchemy import JSON, Column, Enum, Integer, SmallInteger, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class LlmProvider(enum.StrEnum):
    ollama = "ollama"
    openai_compatible = "openai_compatible"


class BackupFrequency(enum.StrEnum):
    """M56 scheduled-backup cadence (docs/11 § Schedule model).

    `off` is a real value, not a NULL: the schedule always has a complete,
    valid shape, so the client never has to interpret "absent" versus "never".
    """

    off = "off"
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class ExportFrequency(enum.StrEnum):
    """M75 scheduled-export cadence (docs/11 § "Scheduled export").

    Deliberately a twin of `BackupFrequency`: the two schedules are
    independent settings (a user can want one and not the other), but their
    shapes are pinned identical so the Settings pickers — and the docs that
    describe them — can be read side by side. `off` is a real value, never
    NULL, for the same reason as M56: a schedule always has a complete shape.
    """

    off = "off"
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class DashboardPreset(enum.StrEnum):
    """M97 dashboard layout preset (docs/09 §Dashboard layout presets).

    Three FIXED orders over the same cards — no editor, no drag-and-drop, no
    card hiding (the rejected scope is documented in docs/10 §M97). `overview`
    is the first-visit default because the G1 tour teaches on that layout; a
    preset only reorders, so every card stays reachable under all three.
    """

    overview = "overview"
    import_first = "import_first"
    budget_first = "budget_first"


class User(TimestampMixin, table=True):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    hashed_password: str
    is_admin: bool = False

    # Optional display name; null until the user sets one in Settings.
    name: str | None = Field(default=None, max_length=255)

    # Preferences (persisted so they roam across devices; consumed from M7).
    # llm_api_key is write-only: accepted on write payloads, never returned.
    privacy_mode: bool = False
    currency_tracker_enabled: bool = True
    llm_provider: LlmProvider = Field(
        default=LlmProvider.ollama,
        sa_column=Column(Enum(LlmProvider, native_enum=False, length=32), nullable=False),
    )
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    # E2EE Phase 2 (docs/33): reject new plaintext writes when true.
    encryption_required: bool = False

    # M56 scheduled client-side-encrypted backup (docs/11 § Schedule model).
    # The five columns are the whole schedule; ranges are enforced by the
    # request schema AND by the CHECK constraints from migration
    # 0029_backup_artifacts, so no client can store an impossible schedule.
    backup_frequency: BackupFrequency = Field(
        default=BackupFrequency.off,
        sa_column=Column(
            Enum(BackupFrequency, native_enum=False, length=16),
            nullable=False,
            server_default="off",
        ),
    )
    backup_hour: int = Field(
        default=3,
        sa_column=Column(SmallInteger, nullable=False, server_default="3"),
    )
    backup_weekday: int = Field(
        default=0,
        sa_column=Column(SmallInteger, nullable=False, server_default="0"),
    )
    backup_day_of_month: int = Field(
        default=1,
        sa_column=Column(SmallInteger, nullable=False, server_default="1"),
    )
    backup_keep_last_n: int = Field(
        default=5,
        sa_column=Column(Integer, nullable=False, server_default="5"),
    )

    # M75 scheduled export (docs/11 § "Scheduled export"). Same five-column
    # shape as the M56 schedule — independent settings, so a user can run one
    # and not the other — and the ranges are enforced by the request schema
    # AND by the CHECK constraints from migration 0032_export_schedule.
    export_frequency: ExportFrequency = Field(
        default=ExportFrequency.off,
        sa_column=Column(
            Enum(ExportFrequency, native_enum=False, length=16),
            nullable=False,
            server_default="off",
        ),
    )
    export_hour: int = Field(
        default=3,
        sa_column=Column(SmallInteger, nullable=False, server_default="3"),
    )
    export_weekday: int = Field(
        default=0,
        sa_column=Column(SmallInteger, nullable=False, server_default="0"),
    )
    export_day_of_month: int = Field(
        default=1,
        sa_column=Column(SmallInteger, nullable=False, server_default="1"),
    )
    export_keep_last_n: int = Field(
        default=5,
        sa_column=Column(Integer, nullable=False, server_default="5"),
    )

    # M97 dashboard layout preset (docs/09 §Dashboard layout presets): which of
    # three fixed orders the dashboard renders its existing cards in. Like every
    # other preference here it lives on the row so it roams across devices; the
    # CHECK (migration 0036_dashboard_preset) keeps any client to the three.
    dashboard_preset: DashboardPreset = Field(
        default=DashboardPreset.overview,
        sa_column=Column(
            Enum(DashboardPreset, native_enum=False, length=16),
            nullable=False,
            server_default="overview",
        ),
    )

    # M108 P2 (docs/09 §Saved report views, docs/02 row): the user's saved
    # Reports presets — a LIST of `{id, title, tab, state}` rows, so the M97
    # "preference lives on the users row and roams with the user" rule applies
    # with `[]` standing for "none saved" (never NULL, for the same
    # absent-vs-empty reason as `backup_frequency = off`). Per-USER, never
    # per-account: a preset stores a date range, a granularity and a chart
    # variant — no account scope. `app/schemas/users.py::SavedReportView`
    # validates every entry on write; the column only has to hold them.
    saved_report_views: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="'[]'::json"),
    )

    # M109 (docs/45 §2): saved what-if scenarios. Same M108-P2 reasoning as
    # `saved_report_views` above — per-user content that roams with the
    # account, `[]` meaning "none saved" is a complete shape, and the entry
    # shape (`{id, name, events[]}`) is an APPLICATION contract validated by
    # `app/schemas/forecast.py`, not a SQL constraint that would duplicate it.
    # The export picks it up for free: the ZIP's table set is derived from
    # `app.models`, so this rides inside `users.csv` (docs/11 §File list).
    forecast_scenarios: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="'[]'::json"),
    )

    # M174 (docs/09 §Tags that mark recurring): the user's OWN tags that mean
    # "this is a recurring commitment". Mark's ask — "for recurring stuff can we
    # make that based on tags since there can be multiple" — with the PM's
    # ruling honoured: NO magic tag names (`/sub|recurring|monthly/i` would be
    # a second, invisible normalizer). The user names the tags; this column is
    # a LIST of `tags.id` values they chose, `[]` meaning "the feature is off"
    # (never NULL, for the same absent-vs-empty reason as `backup_frequency =
    # off` and `saved_report_views` above).
    #
    # Ownership is enforced at the write seam (`api/users.py::update_me`), not
    # by an FK: the column is JSON, so the DB cannot carry one — the same
    # contract `saved_report_views` lives under.
    recurring_intent_tag_ids: list[int] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="'[]'::json"),
    )
