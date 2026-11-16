"""sharing — request/response shapes for household sharing (M90, docs/43).

Every request body is `extra="forbid"` (docs/43 §5) so an unknown field is
a 422 `validation_error`, never silently dropped. Response shapes are pinned
by the spec's JSON blocks: `SharingState` (owner + accept), `InviteCreated`
(the ONE body that ever carries `token` / `accept_url`), `InvitePublic`
(never carries them), `SharingAuditPublic`, and the partner's
`PartnerState`.

This module is also the PROJECTION serializer the privacy-mode mask lives in
(docs/43 §9): `PartnerAccountPage` / `PartnerTransactionPage` /
`project_budget` carry the widened, nullable money fields that let the
owner's `privacy_mode` cross the partner JOIN seam as JSON `null`, while the
owner-facing `AccountPublic` / `TransactionPublic` / `BudgetPublic` keep
serving real numbers to the owner's own browser.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

import email_validator
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.transaction_events import TransactionEventKind
from app.schemas.accounts import AccountPage, AccountPublic
from app.schemas.transactions import (
    SplitParentPublic,
    TransactionEventPublic,
    TransactionPage,
    TransactionPublic,
)


class SharingPartnerBrief(BaseModel):
    """`SharingState.partner` — null while pending / none (docs/43 §5)."""

    model_config = ConfigDict(extra="forbid")

    user_id: int
    name: str | None = None
    email: str


class InvitePartnerBrief(BaseModel):
    """`InvitePublic.partner` — no email here; the invite row carries it."""

    model_config = ConfigDict(extra="forbid")

    user_id: int
    name: str | None = None


class SharingState(BaseModel):
    """200 from GET /sharing, PATCH /sharing/scope, POST /sharing/revoke and
    POST /sharing/invites/accept. `status: "none"` ⇒ nulls / `[]`."""

    model_config = ConfigDict(extra="forbid")

    status: str
    link_id: int | None = None
    email: str | None = None
    partner: SharingPartnerBrief | None = None
    account_ids: list[int] = Field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None


def _valid_invite_email(value: str) -> str:
    """Email-address validation for the invite body.

    `EmailStr` (email-validator 2.x) unconditionally rejects `.local` — no
    `test_environment` bypass exists — which would answer
    `422 validation_error` BEFORE the handler and make docs/43 §5 check (3)
    (`email == DEMO_EMAIL` → `403 demo_account_restricted`, pinned again in
    §Verification 2) unreachable: the demo tenant lives at
    `demo@fintracker.local`. So ordinary addresses go through
    email-validator exactly as `EmailStr` would (syntax, no deliverability —
    that was never checked), and `.local` domains — local-network invites —
    fall back to a conservative shape check. Everything else re-raises.
    """
    candidate = value.strip()
    try:
        email_validator.validate_email(candidate, check_deliverability=False)
    except email_validator.EmailSyntaxError:
        local, at, domain = candidate.rpartition("@")
        if (
            at
            and re.fullmatch(r"[A-Za-z0-9._%+-]{1,64}", local)
            and re.fullmatch(r"[A-Za-z0-9.-]+\.local", domain.lower())
        ):
            return value
        raise
    return value


class InviteCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, AfterValidator(_valid_invite_email)]
    # Deny-by-default: an empty allowlist is legal at every stage and means
    # "the partner sees nothing" (docs/43 §2).
    account_ids: list[int] = Field(default_factory=list)


class ScopeUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ids: list[int]


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=200)


class InviteCreated(BaseModel):
    """201 from POST /sharing/invites — `token` + `accept_url` shown ONCE."""

    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    status: str
    account_ids: list[int] = Field(default_factory=list)
    expires_at: datetime
    created_at: datetime | None = None
    token: str
    accept_url: str
    email_sent: bool


class InvitePublic(BaseModel):
    """GET /sharing/invites items — NO `token`, NO `accept_url`, ever."""

    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    status: str
    account_ids: list[int] = Field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    partner: InvitePartnerBrief | None = None


class SharingAuditPublic(BaseModel):
    """GET /sharing/audit items (docs/43 §3 — the `mcp_audit` shape)."""

    model_config = ConfigDict(extra="forbid")

    id: int
    event: str
    actor_user_id: int | None = None
    invite_id: int | None = None
    args_hash: str
    response_code: int
    duration_ms: int
    created_at: datetime | None = None


class PartnerOwnerBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    name: str | None = None
    email: str


class PartnerState(BaseModel):
    """The LINKED arm of `GET /partner/state` (docs/43 §6) — `linked: true`
    plus the projection the banner renders from."""

    model_config = ConfigDict(extra="forbid")

    linked: Literal[True] = True
    owner: PartnerOwnerBrief
    account_ids: list[int] = Field(default_factory=list)
    privacy_mode: bool
    link_id: int
    accepted_at: datetime | None = None


class PartnerStateUnlinked(BaseModel):
    """The UNLINKED arm of `GET /partner/state` (M92, docs/43 §6).

    An authenticated caller with no `active` link is a NORMAL first-page
    state — "no sharing link yet" is not an authentication failure — so the
    answer is `200 {linked: false}` rather than `401`. Genuinely
    unauthenticated callers still get `401 invalid_token` from
    `get_current_user`, before this model is ever reached.
    """

    model_config = ConfigDict(extra="forbid")

    linked: Literal[False] = False


# `GET /partner/state`'s `response_model`, discriminated on `linked`.
PartnerStateResponse = PartnerState | PartnerStateUnlinked


class PartnerBudget(BaseModel):
    """GET /partner/budgets item — the `GET /budgets` shape with the
    server-computed `spent` / `remaining` / `pct_used` DROPPED (D3): those
    figures are computed over the owner's whole ledger and would either read
    hidden accounts or assert a partial number the server made up.

    M110 (docs/43 §9): the rollover's `carried_in` rides ALONGSIDE the
    definition as its own field — `carry` is the owner's setting (structure,
    never masked) and `carried_in` is money, so it follows the target
    `amount` into JSON `null` while the owner's privacy mode is on
    (`project_budget`). The row itself — id, category, period, name — is
    kept, so the payload's shape (D3 "definitions only") stands."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    user_id: int
    category_id: int
    amount: Decimal | None  # serialized as a string (AGENTS §2); null when masked
    period: str
    carry: str  # off | forward — the owner's setting, not a figure
    carried_in: Decimal | None  # money: null when masked or when nothing carried
    category_name: str


# --- privacy-mode amount masking (M90 D4/A "Reading A", docs/43 §9) --------
#
# Reading B was M90's binding contract: privacy mode is an EGRESS lockdown and
# masked nothing — stated in words in the old honesty test so a later approval
# would surface as a diff. Reading A is the shipped one, and it covers the
# PARTNER seam only: the owner's own view never masks (docs/43 §9). That is
# why the mask lives HERE, in the projection serializer, and not in
# `AccountPublic` / `TransactionPublic` / `BudgetPublic` — the shapes every
# owner route shares. No partner route can build a payload without passing
# through one of these three projectors.
#
# What is masked: every monetary field (`cash_balance`, `investment_value`,
# `total_value`, `starting_balance`, `amount_native`, `cad_amount`, a budget's
# target `amount` and its M110 `carried_in`). What is not: identity and
# structure — account
# names, ids, currencies, dates, payees, category names, budget period — and
# the E2EE ciphers, which are already opaque ciphertext.

_ACCOUNT_MONEY = ("cash_balance", "investment_value", "total_value", "starting_balance")
_TRANSACTION_MONEY = ("amount_native", "cad_amount")


class PartnerAccountPublic(AccountPublic):
    """`GET /partner/accounts` row: the four money fields widen to `str | None`
    so privacy mode can cross the seam as a JSON `null`."""

    cash_balance: str | None
    investment_value: str | None
    total_value: str | None
    starting_balance: str | None


class PartnerAccountPage(BaseModel):
    items: list[PartnerAccountPublic]
    total: int
    limit: int
    offset: int


class PartnerTransactionPublic(TransactionPublic):
    """`GET /partner/transactions` row: money widens to `Decimal | None`."""

    amount_native: Decimal | None
    cad_amount: Decimal | None


class PartnerSplitParentPublic(SplitParentPublic):
    """M107 (docs/44 §9): a split parent for the partner — the same two money
    fields widen, so privacy mode masks the GROUP HEADER's amount exactly
    like it masks every other money field. A header that printed a real total
    while the children read `null` would leak precisely what the mask exists
    to hide."""

    amount_native: Decimal | None
    cad_amount: Decimal | None


class PartnerTransactionPage(BaseModel):
    items: list[PartnerTransactionPublic]
    total: int
    limit: int
    offset: int
    split_parents: list[PartnerSplitParentPublic] = []


def _project_account(item: AccountPublic, *, masked: bool) -> PartnerAccountPublic:
    data: dict = item.model_dump()
    if masked:
        for key in _ACCOUNT_MONEY:
            data[key] = None
    return PartnerAccountPublic.model_validate(data)


def _project_transaction(item: TransactionPublic, *, masked: bool) -> PartnerTransactionPublic:
    data: dict = item.model_dump()
    if masked:
        for key in _TRANSACTION_MONEY:
            data[key] = None
    return PartnerTransactionPublic.model_validate(data)


def project_account_page(page: AccountPage, *, masked: bool) -> PartnerAccountPage:
    """The account page the partner receives: money is JSON `null` when the
    owner's privacy mode is on, real otherwise (the owner's own view is not
    built here and is never masked)."""
    return PartnerAccountPage(
        items=[_project_account(item, masked=masked) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


def _project_split_parent(parent: SplitParentPublic, *, masked: bool) -> PartnerSplitParentPublic:
    """M107 (docs/44 §9): the group header crosses the seam with the SAME
    mask its children do. `child_count` is structure, not money, so it always
    survives — a partner must still see `Split · 3 parts` under privacy mode,
    just never the total behind it."""
    data: dict = parent.model_dump()
    if masked:
        for key in _TRANSACTION_MONEY:
            data[key] = None
    return PartnerSplitParentPublic.model_validate(data)


def project_transaction_page(page: TransactionPage, *, masked: bool) -> PartnerTransactionPage:
    """The transaction page the partner receives — same rule as accounts."""
    return PartnerTransactionPage(
        items=[_project_transaction(item, masked=masked) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        split_parents=[
            _project_split_parent(parent, masked=masked) for parent in page.split_parents
        ],
    )


def project_budget(budget: PartnerBudget, *, masked: bool) -> PartnerBudget:
    """A budget definition for the partner: the row keeps its shape (D3), the
    target `amount` and the M110 `carried_in` go `null` under the owner's
    privacy mode — both are money, and a carried leftover reveals spending
    exactly as much as a limit does."""
    if not masked:
        return budget
    return budget.model_copy(update={"amount": None, "carried_in": None})


# --- M106: the partner's view of one transaction's receipt log (docs/43 §6) ---


class PartnerTransactionEventPublic(BaseModel):
    """`GET /partner/transactions/{id}/events` row — `TransactionEventPublic`
    with the PAYLOAD dropped.

    Minimal on purpose (docs/43 §6 "keep the projection honest but minimal"):
    kind, actor, when, hash and the rendered line travel; the payload does
    not. A payload is the exact set of values a PATCH wrote, `amount_native`
    among them — and §9 nulls money for a partner, so a serializer that
    shipped the payload would walk straight around the mask it exists to
    enforce. Field NAMES still travel as `changed_fields`, because a name is
    structure, not a figure.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    tx_id: int
    event_kind: TransactionEventKind
    actor: int | None
    actor_label: str
    at: datetime | None
    payload_hash: str
    summary: str
    changed_fields: list[str] = Field(default_factory=list)


class PartnerTransactionEventPage(BaseModel):
    items: list[PartnerTransactionEventPublic]
    total: int


def project_transaction_events(
    events: Sequence[TransactionEventPublic],
) -> PartnerTransactionEventPage:
    """Same receipts, payload stripped — the ONLY shape a partner ever gets.

    The actor filter (`actor IN (owner, partner)`) is applied in the query
    that feeds this, so a cloud-service receipt never reaches the projector;
    this function's job is the second half of the boundary: even an
    owner/partner receipt loses its values on the way out.
    """
    items = [
        PartnerTransactionEventPublic(
            id=event.id,
            tx_id=event.tx_id,
            event_kind=event.event_kind,
            actor=event.actor,
            actor_label=event.actor_label,
            at=event.at,
            payload_hash=event.payload_hash,
            summary=event.summary,
            changed_fields=[str(name) for name in (event.payload.get("fields") or [])],
        )
        for event in events
    ]
    return PartnerTransactionEventPage(items=items, total=len(items))
