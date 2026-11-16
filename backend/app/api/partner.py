"""partner — the read-only projection (M90, docs/43 §6).

EVERY route is GET: a pinned invariant with a test
(`test_partner_router_is_get_only`), not a comment. The partner's only
mutations in the whole system are accepting their invite and leaving the
link (M93) — both on the sharing router, never here. M106 adds a sixth GET —
`/partner/transactions/{id}/events` — under the same rule: it reads the
owner's receipt log through the SAME join-scoped predicates, filters it to
owner/partner actors, and hands it to a projector that strips the payload.

**The tenancy rule (docs/43 §6 — an escalation if broken):** every partner
query expresses ownership as `<domain table>.user_id == link.user_id`,
reached from `SharingLink.partner_user_id == current_user.id` — a JOIN
path. No query here filters a domain table by `current_user.id` (that is
the partner's own tenant, wrong data) or by nothing at all (a cross-tenant
read). Scope is re-read per request, which is what makes revocation and
scope changes take effect immediately: the four DATA routes answer
`401 sharing_not_linked` uniformly for never-linked / revoked / expired /
cancelled / owner deleted, so the response never distinguishes "you were
revoked" from "you never had access". `GET /partner/state` is the one
exception (M92, docs/43 §6): a logged-in user asking about their OWN
sharing state gets `200 {linked: false}` when there is no link — a normal
first-page state, not an auth failure — and only a caller with no valid
session gets a 401 (from `get_current_user`).

D5: no key material ever crosses this surface; the UI resolves every E2EE
helper with `dek = null` (frontend). D4: the three privacy ContextVars are
re-bound here with the OWNER's `privacy_mode` — the data subject's posture
governs, and any egress seam a partner route can reach is sealed by the
owner's flag, not the caller's.

D4/amounts (M90 D4/A "Reading A", docs/43 §9): under that same owner flag
every MONETARY field in these payloads is JSON `null`. The mask is applied
by the projection serializers in `app/schemas/sharing.py`
(`project_account_page` / `project_transaction_page` / `project_budget`),
which is why it cannot be forgotten here: a route returns a page built by a
projector, never a bare `AccountPage` / `TransactionPage`. The owner's own
routes keep serving real numbers — privacy mode hides the figures from the
PARTNER, not from the account holder.
"""

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.accounts import _account_totals, _build_public, _committed_cash_subquery
from app.api.deps import Pagination, get_current_user, get_db, get_pagination, paginate
from app.api.transactions import _event_public, _live_scope, _transaction_page
from app.core.config import get_settings
from app.core.errors import api_error
from app.llm.egress import bind_privacy_lock as bind_llm_privacy_lock
from app.models.accounts import INVESTMENT_ACCOUNT_TYPES, Account
from app.models.budgets import Budget
from app.models.categories import Category
from app.models.sharing import SharingLink, SharingLinkStatus
from app.models.transaction_events import TransactionEvent
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.accounts import AccountPage
from app.schemas.categories import CategoryPage
from app.schemas.sharing import (
    PartnerAccountPage,
    PartnerBudget,
    PartnerOwnerBrief,
    PartnerState,
    PartnerStateResponse,
    PartnerStateUnlinked,
    PartnerTransactionEventPage,
    PartnerTransactionPage,
    project_account_page,
    project_budget,
    project_transaction_events,
    project_transaction_page,
)
from app.services import account_value
from app.services import fx as fx_service
from app.services import market_data as market_data_service
from app.services.budgets import carry_in
from app.services.reports import FxCache
from app.services.txn_filters import LEDGER_EXCLUDE_SPLIT

router = APIRouter(prefix="/partner", tags=["partner"])

# The five CORE GETs, by path. The M106 events route is deliberately absent:
# it needs a real `transaction_id`, so a fixed path list cannot address it
# (`test_partner_router_is_get_only` walks `router.routes` instead, and
# covers it).
PARTNER_ROUTES = ("/state", "/accounts", "/transactions", "/categories", "/budgets")


class PartnerContext:
    """`get_partner_link`'s yield: the link, its owner, the live scope."""

    __slots__ = ("link", "owner", "scope")

    def __init__(self, link: SharingLink, owner: User, scope: list[int]) -> None:
        self.link = link
        self.owner = owner
        self.scope = scope


def _resolve_partner_link(current_user: User, db: Session) -> PartnerContext | None:
    """Resolve the caller's single `active` link — one indexed point read on
    `ix_sharing_links_partner_status`, no cache (docs/43 §6).

    Returns `None` for every way the link can be absent (never-linked,
    revoked, expired, cancelled, owner deleted) — one uniform absence, so no
    caller can tell "you were revoked" from "you never had access".
    """
    link = db.exec(
        select(SharingLink)
        .where(
            SharingLink.partner_user_id == current_user.id,
            SharingLink.status == SharingLinkStatus.active,
        )
        .limit(1)
    ).first()
    if link is None:
        # One uniform answer for every way the link can be absent.
        return None
    owner = db.get(User, link.user_id)
    if owner is None:
        # The owner's row is gone (CASCADE would normally have dropped the
        # link) — same uniform absence, never a distinguishable one.
        return None
    # I6: ids validated at write time may dangle later (account deleted) —
    # skipped at READ time, never staged into (docs/43 §3).
    requested = list(link.account_ids or [])
    if requested:
        live = set(
            db.exec(
                select(Account.id).where(Account.user_id == owner.id, Account.id.in_(requested))
            ).all()
        )
        scope = [account_id for account_id in requested if account_id in live]
    else:
        scope = []  # deny-by-default: the partner sees nothing
    # D4 / docs/43 §9.2: rebind the three egress seams with the OWNER's
    # posture for the rest of this request — the caller's own flag was bound
    # by get_current_user and must not govern the data subject's rows.
    fx_service.bind_privacy_lock(owner.privacy_mode)
    market_data_service.bind_privacy_lock(owner.privacy_mode)
    bind_llm_privacy_lock(owner.privacy_mode)
    return PartnerContext(link, owner, scope)


def get_partner_link(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PartnerContext:
    """The DATA routes' dependency: no `active` link → **401
    `sharing_not_linked`** (docs/43 §7) — one uniform answer for never-linked
    / revoked / expired / cancelled / owner deleted, so the response never
    distinguishes "you were revoked" from "you never had access". Scope is
    re-read per request, which is what makes revocation and scope changes take
    effect immediately.

    `GET /partner/state` deliberately does NOT use this (M92, docs/43 §6): for
    a successful login asking about its own sharing state, "no link yet" is a
    normal answer, not an auth failure, so that route answers
    `200 {linked: false}` instead. The other four keep the 401.
    """
    context = _resolve_partner_link(current_user, db)
    if context is None:
        raise api_error(401, "No shared household is linked to this account", "sharing_not_linked")
    return context


def get_partner_state_link(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PartnerContext | None:
    """`GET /partner/state`'s dependency: the SAME read as `get_partner_link`,
    with the absence left as `None` so the handler can answer `200 {linked:
    false}` (M92, docs/43 §6). A caller with no valid session never reaches
    this — `get_current_user` answers `401 invalid_token` first."""
    return _resolve_partner_link(current_user, db)


PartnerDep = Annotated[PartnerContext, Depends(get_partner_link)]
PartnerStateDep = Annotated[PartnerContext | None, Depends(get_partner_state_link)]


@router.get("/state", response_model=PartnerStateResponse)
def partner_state(context: PartnerStateDep) -> PartnerStateResponse:
    """Owner identity + live scope — the banner the UI renders from.

    `200 {linked: false}` when the authenticated caller has no `active` link
    (the invite CTA on Settings is the UI's next step); `200 {linked: true,
    …}` when they do. Unauthenticated callers are refused by
    `get_current_user` with `401 invalid_token`, so `401 sharing_not_linked`
    is no longer part of this route's contract (docs/43 §6/§7).
    """
    if context is None:
        return PartnerStateUnlinked()
    owner = context.owner
    return PartnerState(
        owner=PartnerOwnerBrief(user_id=owner.id, name=owner.name, email=owner.email),
        account_ids=list(context.scope),
        privacy_mode=owner.privacy_mode,
        link_id=context.link.id,
        accepted_at=context.link.accepted_at,
    )


@router.get("/accounts", response_model=PartnerAccountPage)
def partner_accounts(
    context: PartnerDep,
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> PartnerAccountPage:
    """`Account.user_id == owner AND Account.id IN scope` — rows are the
    OWNER's accounts, served through the partner's projection serializer
    (`PartnerAccountPublic`, docs/43 §9.4): identical to what the owner's
    browser receives except that the four money fields are JSON `null`
    while `context.owner.privacy_mode` is on. Empty scope ⇒ `items: []`."""
    owner_id = context.owner.id
    scope = context.scope
    if not scope:
        return PartnerAccountPage(
            items=[], total=0, limit=pagination.limit, offset=pagination.offset
        )
    stmt = (
        select(Account, _committed_cash_subquery(account_ids=scope))
        .where(Account.user_id == owner_id, Account.id.in_(scope))
        .order_by(Account.id)
    )
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    today = datetime.now(UTC).date()
    # Valuation is scope-filtered too (docs/43 §2): a hidden account's
    # trades/pools never even enter the computation for a partner read.
    cad, reporting = _account_totals(db, owner_id, today, account_ids=scope)
    # M145: the partner projection is the OWNER's numbers through a
    # serializer, so an investment account's cash must come from the SAME
    # chain the owner's card and chart read — scope-batched, never one query
    # per account.
    investment = [account for account, _ in rows if account.type in INVESTMENT_ACCOUNT_TYPES]
    cash_map = account_value.accounts_cash(db, owner_id, investment, today) if investment else {}
    items = [
        _build_public(
            db,
            account,
            balance,
            cad,
            reporting,
            today,
            investment_cash=cash_map.get(account.id),
            investment_account=account.type in INVESTMENT_ACCOUNT_TYPES,
        )
        for account, balance in rows
    ]
    return project_account_page(
        AccountPage(items=items, total=total, limit=pagination.limit, offset=pagination.offset),
        masked=context.owner.privacy_mode,
    )


@router.get("/transactions", response_model=PartnerTransactionPage)
def partner_transactions(
    context: PartnerDep,
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
    account_id: Annotated[int | None, Query()] = None,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> PartnerTransactionPage:
    """The owner's list query with `current_user.id` replaced by `owner_id`
    and `account_id IN scope` conjoined (docs/43 §6) — ONE query builder
    (`_transaction_page`), so the staging rule, the `date` rule and the
    pagination shape cannot drift between the two surfaces.

    `account_id` outside the scope ⇒ 404 `sharing_account_missing` (not a
    silent ignore: the client asked for a row it may not see). `from`/`to`
    gate `transactions.date` only, never `posting_date` (docs/02).

    The page is projected through `PartnerTransactionPage` on the way out:
    `amount_native` / `cad_amount` are JSON `null` while the owner's privacy
    mode is on (docs/43 §9), dates, payees and categories unchanged.
    """
    owner_id = context.owner.id
    scope = context.scope
    if account_id is not None and account_id not in scope:
        raise api_error(404, "Account not found", "sharing_account_missing")
    if not scope:
        return PartnerTransactionPage(
            items=[], total=0, limit=pagination.limit, offset=pagination.offset
        )
    scope_predicate = (
        (Transaction.user_id == owner_id)
        & _live_scope(owner_id, account_ids=scope)
        & Transaction.account_id.in_(scope)
        # M107 (docs/44 §6.1 + §9): the partner's page is built by the SAME
        # `_transaction_page`, so it is LEDGER exactly like the owner's —
        # split parents out, children in, `split_parents` for the headers.
        # One population rule, two surfaces: they cannot drift.
        & LEDGER_EXCLUDE_SPLIT
    )
    if account_id is not None:
        scope_predicate &= Transaction.account_id == account_id
    if from_ is not None:
        scope_predicate &= Transaction.date >= from_
    if to is not None:
        scope_predicate &= Transaction.date <= to
    stmt = (
        select(Transaction)
        .where(scope_predicate)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    return project_transaction_page(
        _transaction_page(
            db, stmt, owner_id=owner_id, pagination=pagination, pair_account_ids=scope
        ),
        masked=context.owner.privacy_mode,
    )


@router.get(
    "/transactions/{transaction_id}/events",
    response_model=PartnerTransactionEventPage,
)
def partner_transaction_events(
    transaction_id: int,
    context: PartnerDep,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PartnerTransactionEventPage:
    """M106 / docs/43 §6: one transaction's receipt log, projected.

    Two filters, in this order:

    1. the ROW must be visible — the owner's row, `account_id IN scope`, and
       the committed-only predicate (`_live_scope`), i.e. exactly the rows
       `GET /partner/transactions` already serves. History cannot reach past
       the ledger a partner may read.
    2. the RECEIPT's `actor` must be the owner or the partner themselves.
       A bank-sync pull stages with `actor IS NULL`, and that is dropped here:
       a partner sees what the two of them did, never what a machine did
       (docs/43 §6 — "partner did X" + "owner did X", nothing else).

    What comes back then goes through `project_transaction_events`, which
    strips the payload so no written value — money included — crosses the
    seam docs/43 §9 seals. `404 not_found` for a row outside the scope, the
    same answer as an id that never existed.
    """
    owner_id = context.owner.id
    scope = context.scope
    if not scope:
        return PartnerTransactionEventPage(items=[], total=0)
    transaction = db.exec(
        select(Transaction).where(
            Transaction.user_id == owner_id,
            Transaction.account_id.in_(scope),
            _live_scope(owner_id, account_ids=scope),
            Transaction.id == transaction_id,
        )
    ).first()
    if transaction is None:
        raise api_error(404, "Transaction not found", "not_found")
    allowed = sorted({owner_id, current_user.id})
    events = db.exec(
        select(TransactionEvent)
        .where(
            TransactionEvent.tx_id == transaction.id,
            TransactionEvent.user_id == owner_id,
            TransactionEvent.actor.in_(allowed),
        )
        .order_by(TransactionEvent.created_at.desc(), TransactionEvent.id.desc())
    ).all()
    names = {row.id: row.name for row in db.exec(select(User).where(User.id.in_(allowed))).all()}
    return project_transaction_events(
        [_event_public(event, current_user.id, names) for event in events]
    )


@router.get("/categories", response_model=CategoryPage)
def partner_categories(
    context: PartnerDep,
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> CategoryPage:
    """The owner's categories, unfiltered — byte-identical to
    `GET /categories` (docs/43 §6). A category name is owner-set metadata,
    not a fact derived from any account, so it cannot leak a hidden one."""
    stmt = select(Category).where(Category.user_id == context.owner.id).order_by(Category.id)
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    return CategoryPage(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/budgets", response_model=list[PartnerBudget])
def partner_budgets(
    context: PartnerDep,
    db: Annotated[Session, Depends(get_db)],
) -> list[PartnerBudget]:
    """Definitions ONLY (D3): the same bare-list shape as `GET /budgets`
    with `spent` / `remaining` / `pct_used` dropped — those are computed
    over the owner's whole ledger and would either read hidden accounts
    (leak) or assert a partial number the server made up. The partner's UI
    derives progress client-side (`s3Engine.budgetsProgress`) over rows it
    actually holds, labelled "shared accounts only" (docs/43 §2).

    M110 (docs/43 §9): the rollover's `carried_in` is published here as its
    OWN field, per the same one-producer rule the owner's route follows
    (`services.budgets.carry_in`), so this surface can never print a carry the
    Budgets page does not. It is money computed over the owner's whole
    ledger — exactly why D3 drops `spent`/`remaining`/`pct_used`, and exactly
    why it joins `amount` in the privacy-mode mask (docs/43 §9). The row keeps
    its identity (`id`, `category_id`, `category_name`, `period`, `carry`), so
    D3's shape is untouched; only the numbers are hidden."""
    rows = db.exec(
        select(Budget, Category.name)
        .join(Category, Budget.category_id == Category.id)
        .where(Budget.user_id == context.owner.id)
        .order_by(Category.name)
    ).all()
    reporting = get_settings().reporting_currency
    fx = FxCache(db, reporting)
    return [
        project_budget(
            PartnerBudget(
                id=budget.id,
                user_id=budget.user_id,
                category_id=budget.category_id,
                amount=budget.amount,
                period=budget.period,
                carry=budget.carry,
                carried_in=carry_in(db, budget, reporting, fx),
                category_name=category_name,
            ),
            masked=context.owner.privacy_mode,
        )
        for budget, category_name in rows
    ]
