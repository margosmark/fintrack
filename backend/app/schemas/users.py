"""users — response/update shapes (docs/08).

The user object NEVER exposes hashed_password or llm_api_key.
"""

import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.users import DashboardPreset, LlmProvider

# --- M108 P2 (docs/09 §Saved report views, docs/02 §users) -------------------
#
# The saved-report tab set: the SAME seven ids the report strip renders
# (`frontend/src/lib/reportTabs.ts::REPORT_TABS`). A saved view is addressed by
# the id the strip restores, so the two lists are one contract pinned in
# docs/09; it is restated here (not shared, the server has no TS) because the
# server must reject a preset it could never restore rather than store it and
# hand the client a preset that restores to nothing.
REPORT_TABS = frozenset(
    {"spending", "categories", "cashflow", "networth", "performance", "dividends", "projections"}
)

# Granularity is docs/12's shared contract (`day | week | month | year`).
REPORT_GRANULARITIES = frozenset({"day", "week", "month", "year"})

# A preset's `state` is a CLOSED set of names, not an arbitrary blob: every
# key must be one the Reports page actually restores, so a typo'd or invented
# field is a 422 at write time instead of a preset that silently restores
# nothing (AGENTS §3 — strict validation beats silent tolerance).
SAVED_VIEW_STATE_KEYS = frozenset(
    {"from", "to", "granularity", "view", "year", "sort_key", "sort_desc", "group_by"}
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VIEW_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SORT_KEY = re.compile(r"^[a-z_]{1,32}$")

SAVED_VIEW_MAX_COUNT = 50
SAVED_VIEW_MAX_STATE_KEYS = 8
SAVED_VIEW_MAX_STR = 64

# --- M174 (docs/09 §Tags that mark recurring) --------------------------------
#
# How many tags a user may nominate as "marks recurring". The cap exists for
# the same reason `SAVED_VIEW_MAX_COUNT` does: the confirmed-subscription query
# reads this list on EVERY cash-flow calendar call, and a four-figure list would
# be a scan cost nobody chose. It is a ceiling on the FEATURE, not on the
# user's tags — a tenant can own hundreds of tags and nominate twenty.
RECURRING_INTENT_MAX_TAGS = 20

# --- M123 (docs/09 §Create from search) --------------------------------------
#
# `kind` picks WHICH closed key-set validates `state`; `tab` stays the restore
# target (the id the client's list filters on). A transaction view's `tab` is
# always TRANSACTION_TAB, and the two are cross-validated in `SavedReportView`
# so a stored preset can never claim one kind and carry the other's shape.
SAVED_VIEW_KINDS = frozenset({"reports", "transactions"})
TRANSACTION_TAB = "transactions"

# The four URL-addressable search params of `/transactions` (docs/09), kept
# verbatim so restoring a transaction view is a navigation to
# `/transactions?q=…&category=…` — the view IS a bookmark.
TRANSACTION_STATE_KEYS = frozenset({"q", "category", "account", "tag"})

# `q` is free text typed into the search box (the server's own `/transactions`
# handler takes far longer than the id-shaped params), so it gets its own cap.
TRANSACTION_VIEW_MAX_Q = 240
_ID_RE = re.compile(r"^\d{1,10}$")
_TAG_LIST_RE = re.compile(r"^\d{1,10}(,\d{1,10})*$")


def _validate_saved_state(state: dict) -> dict:
    """Type-check each key of a preset's `state` against its own rule."""
    if len(state) > SAVED_VIEW_MAX_STATE_KEYS:
        raise ValueError(
            f"saved_report_views state may hold at most {SAVED_VIEW_MAX_STATE_KEYS} keys"
        )
    for key, value in state.items():
        if key not in SAVED_VIEW_STATE_KEYS:
            raise ValueError(f"unknown saved_report_views state key {key!r}")
        if key in ("from", "to"):
            if not isinstance(value, str) or not _ISO_DATE.match(value):
                raise ValueError(
                    f"saved_report_views state {key!r} must be an ISO date (YYYY-MM-DD)"
                )
        elif key == "granularity":
            if value not in REPORT_GRANULARITIES:
                raise ValueError(
                    "saved_report_views state 'granularity' must be day|week|month|year"
                )
        elif key == "view":
            if not isinstance(value, str) or not _VIEW_NAME.match(value):
                raise ValueError("saved_report_views state 'view' must be a short lowercase name")
        elif key == "year":
            # `True` is an `int` subclass — reject it explicitly.
            if isinstance(value, bool) or not isinstance(value, int) or not 1900 <= value <= 2200:
                raise ValueError("saved_report_views state 'year' must be an integer in 1900..2200")
        elif key == "sort_key":
            if not isinstance(value, str) or not _SORT_KEY.match(value):
                raise ValueError(
                    "saved_report_views state 'sort_key' must be a short snake_case name"
                )
        elif key == "group_by":
            # M124 added the tag dimension to the Group-by control, so a
            # saved view has to be able to restore it (docs/12 §Tag dimension).
            if value not in ("category", "tag", "owner"):
                raise ValueError("saved_report_views state 'group_by' must be category|tag|owner")
        elif key == "sort_desc" and not isinstance(value, bool):
            raise ValueError("saved_report_views state 'sort_desc' must be a boolean")
        if isinstance(value, str) and len(value) > SAVED_VIEW_MAX_STR:
            raise ValueError(
                f"saved_report_views state {key!r} is longer than {SAVED_VIEW_MAX_STR}"
            )
    return state


def _validate_transaction_state(state: dict) -> dict:
    """Type-check a `kind="transactions"` view's `state`.

    Same rule as `_validate_saved_state`: a CLOSED key-set with a per-key
    format, so a typo'd or invented field is a 422 at write time rather than a
    view that silently restores nothing (AGENTS §3). The keys are the four
    URL-addressable search params, so `state` and `?…` stay one encoding.
    """
    if len(state) > SAVED_VIEW_MAX_STATE_KEYS:
        raise ValueError(
            f"saved_report_views state may hold at most {SAVED_VIEW_MAX_STATE_KEYS} keys"
        )
    for key, value in state.items():
        if key not in TRANSACTION_STATE_KEYS:
            raise ValueError(f"unknown saved_report_views state key {key!r}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"saved_report_views state {key!r} must be a non-empty string")
        if key == "q":
            if len(value) > TRANSACTION_VIEW_MAX_Q:
                raise ValueError(
                    f"saved_report_views state 'q' is longer than {TRANSACTION_VIEW_MAX_Q}"
                )
        elif key == "category":
            if value != "uncategorized" and not _ID_RE.match(value):
                raise ValueError(
                    "saved_report_views state 'category' must be a category id or 'uncategorized'"
                )
        elif key == "account":
            if not _ID_RE.match(value):
                raise ValueError("saved_report_views state 'account' must be an account id")
        elif not _TAG_LIST_RE.match(value):
            raise ValueError("saved_report_views state 'tag' must be comma-separated tag ids")
    return state


class SavedReportView(BaseModel):
    """One saved view (docs/09 §Saved report views / §Create from search).

    `id` is generated by the CLIENT (a crypto-random token): the column is a
    plain JSON list with no primary key, so rename and delete address a preset
    by the id the list already carries and two tabs never collide.

    `kind` selects the `state` contract — `reports` (the original M108 preset,
    default so every stored M108 row still validates unchanged) or
    `transactions` (M123). `tab` remains the restore target: a report tab id,
    or `transactions`.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=80)
    tab: str
    # Defaults to `reports`, so a client that never heard of `kind` still
    # writes exactly the M108 shape.
    kind: str = "reports"
    state: dict = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in SAVED_VIEW_KINDS:
            raise ValueError(f"unknown saved report view kind {value!r}")
        return value

    @field_validator("title")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # A title of only spaces is not a name; strip once so " Groceries " and
        # "Groceries" are the same preset rather than two lookalikes.
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @model_validator(mode="after")
    def _kind_contract(self) -> "SavedReportView":
        """`kind` and `tab` must describe the same surface.

        Runs after every field is individually well-formed, so this is where
        the cross-check lives: a preset may not claim `kind="reports"` with
        `tab="transactions"` (or the reverse) and store a `state` the other
        kind's reader would silently ignore.
        """
        if self.kind == "reports":
            if self.tab not in REPORT_TABS:
                raise ValueError(f"unknown report tab {self.tab!r}")
            _validate_saved_state(self.state)
        elif self.tab != TRANSACTION_TAB:
            raise ValueError(
                f"a saved view with kind='transactions' must use tab {TRANSACTION_TAB!r}"
            )
        else:
            _validate_transaction_state(self.state)
        return self


def _validate_saved_view_list(views: list) -> list:
    if len(views) > SAVED_VIEW_MAX_COUNT:
        raise ValueError(f"at most {SAVED_VIEW_MAX_COUNT} saved report views")
    seen: set[str] = set()
    for view in views:
        # `field_validator` runs AFTER the item models are built, so the list
        # holds `SavedReportView` objects, not the raw dicts it was given.
        view_id = view["id"] if isinstance(view, dict) else view.id
        if view_id in seen:
            raise ValueError(f"duplicate saved report view id {view_id!r}")
        seen.add(view_id)
    return views


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str | None = None
    privacy_mode: bool
    currency_tracker_enabled: bool
    llm_provider: LlmProvider
    llm_base_url: str | None = None
    llm_model: str | None = None
    encryption_required: bool
    # M97 (docs/09 §Dashboard layout presets): always present on reads — the
    # first-visit default is `overview`, so a client never has to interpret
    # "absent" versus "overview".
    dashboard_preset: DashboardPreset
    # M108 P2 (docs/09 §Saved report views): always present on reads, `[]` for
    # "none saved" — the same absent-vs-empty rule as `dashboard_preset`.
    saved_report_views: list[SavedReportView] = Field(default_factory=list)
    # M174 (docs/09 §Tags that mark recurring): always present on reads, `[]`
    # for "the feature is off" — a client must never have to tell "absent" from
    # "no tags chosen". The ids are the caller's OWN `tags.id`s; ownership is
    # checked at the write seam, so a read can only echo what was stored.
    recurring_intent_tag_ids: list[int] = Field(default_factory=list)


class UserUpdate(BaseModel):
    privacy_mode: bool | None = None
    currency_tracker_enabled: bool | None = None
    llm_provider: LlmProvider | None = None
    # Not validated here on purpose: the policy check lives in
    # api/users.py::update_me so the envelope can carry the domain code
    # 422 invalid_llm_base_url (scheme/host SSRF policy + loopback-only under
    # privacy — app/llm/egress.py). A Pydantic validator would surface the
    # generic validation_error code instead. None/empty clears the stored URL.
    llm_base_url: str | None = None
    llm_model: str | None = None
    encryption_required: bool | None = None
    # Write-only (docs/02): accepted on PATCH, never returned by any read.
    llm_api_key: str | None = None
    # M97: one of the three fixed orders; an unknown value is a 422
    # validation_error (the enum does the validation, no custom code).
    dashboard_preset: DashboardPreset | None = None
    # M108 P2: the WHOLE list, not a per-preset CRUD — a saved view is a
    # preference like every other `users` column, so the client sends the list
    # it wants stored and rename/delete are client-side list edits. An unknown
    # value is a 422 validation_error (docs/08).
    saved_report_views: list[SavedReportView] | None = None
    # M174 (docs/09 §Tags that mark recurring): the WHOLE list, not a per-tag
    # CRUD — the same whole-list preference write `saved_report_views` uses, so
    # adding or removing a tag is a client-side list edit and one PATCH. `[]`
    # turns the feature off. Ids are validated as POSITIVE ints here and as the
    # CALLER'S OWN tags in `api/users.py::update_me` — the schema cannot see the
    # table, so ownership is a domain error (`422
    # invalid_recurring_intent_tag_ids`), not a generic validation_error.
    recurring_intent_tag_ids: list[int] | None = None

    @field_validator("recurring_intent_tag_ids", mode="before")
    @classmethod
    def _valid_tag_ids(cls, value: list | None) -> list | None:
        """Validate the RAW value, before Pydantic's int coercion.

        `mode="before"` on purpose: a JSON `["7"]` would otherwise be silently
        coerced to `7` and stored, and AGENTS §3 is explicit that strict
        validation beats silent tolerance — an id list is a wire contract, and a
        string id is a client bug this seam should name rather than absorb.

        Ownership is NOT checked here — the schema cannot see the `tags` table,
        so a foreign id is a domain error (`422
        invalid_recurring_intent_tag_ids`, `api/users.py::update_me`).
        """
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("recurring_intent_tag_ids must be a list of tag ids")
        if len(value) > RECURRING_INTENT_MAX_TAGS:
            raise ValueError(
                f"recurring_intent_tag_ids may hold at most {RECURRING_INTENT_MAX_TAGS} tags"
            )
        for tag_id in value:
            if isinstance(tag_id, bool) or not isinstance(tag_id, int):
                raise ValueError("recurring_intent_tag_ids must be integers, not strings")
            if tag_id <= 0:
                raise ValueError("recurring_intent_tag_ids must be positive tag ids")
        return value

    @field_validator("saved_report_views")
    @classmethod
    def _unique_ids(cls, value: list | None) -> list | None:
        return None if value is None else _validate_saved_view_list(value)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ProfileUpdate(BaseModel):
    # name/email optional; current_password required ONLY when email changes.
    name: str | None = None
    email: EmailStr | None = None
    current_password: str | None = None


class DeleteAccountRequest(BaseModel):
    current_password: str
