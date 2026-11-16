"""sharing — owner consent routes + the invitee's mutations (M90, docs/43).

Lifecycle (docs/43 §4): pending → active (single-use accept) / cancelled
(owner) / expired (TTL) ; active → revoked (owner's `revoke`, or the
partner's own `leave` — D6). Terminal states never transition — a new
relationship is a new row.

Three cross-cutting rules this module owns:

* **Receipts** — one `sharing_audit` row per MUTATING request, success and
  handler-level refusal alike (docs/43 §3), best effort: a receipt that
  cannot be written is logged at WARNING and the response stands. Committed
  WITH the state change on success, in its own short transaction on a
  refusal. The demo guard (dependency) and the rate limiter (decorator)
  fire before the handler, so their 429/403 are answered without a receipt —
  there is no handler to attribute them to.
* **No enumeration oracle** (docs/42 rule): inviting ANY address that clears
  checks (3)-(8) answers `201` exactly like one with an account. Create
  never reveals whether the address is registered.
* **Email is best effort and privacy-governed**: `email_sent: false`
  whenever the owner's privacy mode is on or SMTP is unconfigured — the
  `201` body is always a working invite (docs/43 §9.3).
* **State, never recency** (M98 F-1/F-2): every act on "the link"
  resolves a row by STATUS — the owner's `scope`/`revoke` and `GET
  /sharing` take the `status='active'` row (the partial unique index makes
  it unique), the partner's `leave` takes `partner_user_id = me AND
  status='active'`, and a pending invite is addressed only by its OWN id
  (`DELETE /sharing/invites/{id}`). Newest-by-id is a LISTING order, never
  a selector: a pending row created before the accept keeps a higher `id`
  forever, and newest-first let it hide the live partner, absorb the
  scope write, and block consent withdrawal permanently.
"""

import hashlib
import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlmodel import func, select

from app.api.deps import Pagination, get_current_user, get_db, get_pagination, paginate
from app.core.config import get_settings
from app.core.errors import APIError, api_error
from app.core.ratelimit import SHARING_INVITE_LIMIT, SHARING_MUTATE_LIMIT, human_paced_limit
from app.models.accounts import Account
from app.models.sharing import SharingAudit, SharingLink, SharingLinkStatus
from app.models.users import User
from app.schemas.sharing import (
    AcceptBody,
    InviteCreateBody,
    InviteCreated,
    InvitePartnerBrief,
    InvitePublic,
    ScopeUpdateBody,
    SharingAuditPublic,
    SharingPartnerBrief,
    SharingState,
)
from app.services.demo_seed import DEMO_EMAIL
from app.services.notify_delivery import send_invite_email
from app.services.session_release import release_before_provider_wait

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sharing", tags=["sharing"])

INVITE_TTL = timedelta(days=7)
# 5 outstanding `pending` invites per owner — parity with `mcp_token_cap`'s 400
# (docs/43 §11): a single owner cannot mint an unbounded pile of live tokens.
PENDING_INVITE_CAP = 5
# Where a copied invite link points (docs/43 §5).
ACCEPT_ROUTE = "/sharing/accept"


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def hash_invite_token(token: str) -> str:
    """sha256 hex of the raw invite token (docs/43 §3)."""
    return hashlib.sha256(token.encode()).hexdigest()


def _args_hash(body: dict) -> str:
    """Byte-identical formula to `mcp_server.audit.args_hash` (D8): unsalted
    sha256 over the canonical JSON — sorted keys, tight separators, `default=str`.
    The invite body carries the invitee's email, so it is hashed, never stored.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _dedupe(ids: list[int]) -> list[int]:
    """Order-preserving de-duplication — the `account_ids` column invariant
    (docs/43 §3): order-preserving, de-duplicated."""
    seen: set[int] = set()
    out: list[int] = []
    for value in ids:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _record_receipt(
    db: Session,
    *,
    owner_id: int,
    actor_id: int,
    invite_id: int | None,
    event: str,
    body: dict,
    response_code: int,
    started: float,
) -> None:
    """Best-effort receipt write (docs/43 §3): never raises.

    On the success path callers `db.add(...)` their own receipt row BEFORE
    the state-change commit so both land in ONE transaction; this helper is
    the refusal path (the request's own short transaction) and the
    IntegrityError fallback.
    """
    try:
        db.add(
            SharingAudit(
                user_id=owner_id,
                actor_user_id=actor_id,
                invite_id=invite_id,
                event=event,
                args_hash=_args_hash(body),
                response_code=response_code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
        db.commit()
    except Exception:  # an unreachable receipt sink must not fail a response
        db.rollback()
        logger.warning("sharing receipt for %s failed", event, exc_info=True)


def _commit_state_and_receipt(db: Session, event: str, make_receipt, reapply) -> None:
    """Success path (docs/43 §3): the receipt commits WITH the state change
    in ONE transaction. If the receipt poisons that transaction (insert
    failure), it is rolled back and the state change alone is re-applied and
    committed — an unreachable receipt sink must never fail a user's action.
    `reapply` re-establishes what `db.rollback()` just reverted.
    """
    try:
        db.add(make_receipt())
        db.commit()
        return
    except IntegrityError:
        # A STATE conflict (the accept race, I2) — never a receipt problem:
        # `sharing_audit` rows carry no unique constraint, so an integrity
        # failure here can only come from the caller's own state change.
        raise
    except Exception:  # receipt failure downgrades, never propagates
        db.rollback()
        logger.warning(
            "sharing receipt for %s failed; committing state alone", event, exc_info=True
        )
    reapply()
    db.commit()


def _owned_account_ids(db: Session, owner_id: int, ids: list[int]) -> None:
    """404 `sharing_account_missing` for any id not owned by `owner_id`.

    Uniform by design (docs/43 §7): an unknown id and another tenant's id
    answer identically, so the response never confirms which ids exist.
    """
    wanted = set(ids)
    if not wanted:
        return
    owned = set(
        db.exec(select(Account.id).where(Account.user_id == owner_id, Account.id.in_(wanted))).all()
    )
    if not wanted <= owned:
        raise api_error(404, "One or more accounts not found", "sharing_account_missing")


def _active_link(db: Session, owner_id: int) -> SharingLink | None:
    """The owner's LIVE link — resolved by STATE, never by recency (M98 F-1).

    `uq_sharing_links_active_owner` guarantees at most one row answers
    (docs/43 §3, I2), so this is the only row an owner-act on "the link"
    may target. Recency is not a selector: a `pending` invite created
    BEFORE the accept keeps a higher `id` forever, and targeting newest
    first let it hijack `revoke` (409 forever), `PATCH /scope` (written to
    a row no partner reads) and `GET /sharing` (the live partner hidden).
    """
    return db.exec(
        select(SharingLink)
        .where(
            SharingLink.user_id == owner_id,
            SharingLink.status == SharingLinkStatus.active,
        )
        .limit(1)
    ).first()


def _ended_link(db: Session, owner_id: int) -> SharingLink | None:
    """The partnership the owner most recently ENDED — the row `revoke`'s
    idempotent repeat answers for (M79, docs/39 §8: `200` on every repeat).

    Newest `revoked_at` first, so the answer never names an OLDER ended row
    while a later one exists (M98 F-2's receipt rule: a receipt names the
    row the act concerns). A `pending` / `cancelled` / `expired` row is
    deliberately NOT one of these: with no live link and nothing ever
    ended, revoke answers `404 sharing_not_linked` instead of the `409` a
    cancel could not clear (M98 F-1).
    """
    return db.exec(
        select(SharingLink)
        .where(
            SharingLink.user_id == owner_id,
            SharingLink.status == SharingLinkStatus.revoked,
        )
        .order_by(SharingLink.revoked_at.desc().nullslast(), SharingLink.id.desc())
        .limit(1)
    ).first()


def _active_partner_link(db: Session, partner_id: int) -> SharingLink | None:
    """The partner's LIVE link — the state-based twin of `_active_link`
    (M98 F-2): `partner_user_id = me AND status = 'active'`, the exact
    predicate every partner read already re-resolves (docs/43 §6), so
    `leave` ends the row that actually grants access. Newest-first here
    used to pick a stale terminal row and answer `200` as a no-op while
    the link survived."""
    return db.exec(
        select(SharingLink)
        .where(
            SharingLink.partner_user_id == partner_id,
            SharingLink.status == SharingLinkStatus.active,
        )
        .limit(1)
    ).first()


def _ended_partner_link(db: Session, partner_id: int) -> SharingLink | None:
    """The partnership the partner most recently ENDED — `leave`'s
    idempotent repeat (M79), same ordering rule as `_ended_link`."""
    return db.exec(
        select(SharingLink)
        .where(
            SharingLink.partner_user_id == partner_id,
            SharingLink.status == SharingLinkStatus.revoked,
        )
        .order_by(SharingLink.revoked_at.desc().nullslast(), SharingLink.id.desc())
        .limit(1)
    ).first()


def _latest_row(db: Session, owner_id: int) -> SharingLink | None:
    """`GET /sharing`'s FALLBACK when no active link exists — newest by id,
    whatever its status (docs/43 §5): it renders the pending invite the
    owner can cancel and the ended state the UI shows as "Nobody has
    access". Deliberately NOT a targeting helper: owner mutations resolve
    state (`_active_link`), never recency (M98 F-1)."""
    return db.exec(
        select(SharingLink)
        .where(SharingLink.user_id == owner_id)
        .order_by(SharingLink.id.desc())
        .limit(1)
    ).first()


def _owner_has_active(db: Session, owner_id: int) -> bool:
    """Does `owner_id` already hold an active link? Create check (5) and
    the accept pre-check — the OWNER seat's answer, so a caller who cannot
    be given a second partner is told so in owner terms.

    Deliberately its own function (the `_partner_occupied` rule):
    `test_owner_side_accept_race_loser_gets_the_owner_attributed_code`
    bypasses it to reproduce the interleaving where BOTH accepts pass the
    pre-check, leaving the partial unique index as the only referee — so
    the `IntegrityError` attribution must read `_active_link`, never this.
    """
    return (
        db.exec(
            select(SharingLink.id)
            .where(
                SharingLink.user_id == owner_id,
                SharingLink.status == SharingLinkStatus.active,
            )
            .limit(1)
        ).first()
        is not None
    )


def _partner_occupied(db: Session, user_id: int, exclude_link_id: int) -> bool:
    """Is `user_id` already SOME OTHER owner's active partner (check 9)?

    Deliberately its own function: `test_accept_race_is_decided_by_the_
    database` bypasses it to reproduce the interleaving where BOTH accepts
    pass the pre-check, leaving the partial unique index as the only referee.
    """
    row = db.exec(
        select(SharingLink.id)
        .where(
            SharingLink.partner_user_id == user_id,
            SharingLink.status == SharingLinkStatus.active,
            SharingLink.id != exclude_link_id,
        )
        .limit(1)
    ).first()
    return row is not None


def _state_of(db: Session, link: SharingLink | None) -> SharingState:
    if link is None:
        return SharingState(status="none")
    partner: SharingPartnerBrief | None = None
    if link.partner_user_id is not None:
        row = db.get(User, link.partner_user_id)
        partner = SharingPartnerBrief(
            user_id=link.partner_user_id,
            name=row.name if row is not None else None,
            email=row.email if row is not None else link.email,
        )
    return SharingState(
        status=link.status.value,
        link_id=link.id,
        email=link.email,
        partner=partner,
        account_ids=list(link.account_ids or []),
        expires_at=link.expires_at,
        created_at=link.created_at,
        accepted_at=link.accepted_at,
        revoked_at=link.revoked_at,
    )


# --- owner reads ------------------------------------------------------------


@router.get("", response_model=SharingState)
def get_sharing_state(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SharingState:
    """The owner's ACTIVE link when one exists, otherwise their latest row
    by `id`, whatever its status; no rows → status `none`. Never 404
    (docs/43 §5).

    STATE first, recency only as the no-live-link fallback (M98 F-1): an
    invite created BEFORE the accept keeps a higher `id` forever, so
    newest-first answered `pending` and hid the live partner — the very
    person whose access this card exists to withdraw — from the owner's
    canonical state view.
    """
    link = _active_link(db, current_user.id)
    if link is None:
        link = _latest_row(db, current_user.id)
    return _state_of(db, link)


@router.get("/invites")
def list_invites(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> dict:
    """Newest first. NEVER returns `token` / `accept_url` — the raw token
    exists in exactly two places: the create `201` and the invite email."""
    stmt = (
        select(SharingLink)
        .where(SharingLink.user_id == current_user.id)
        .order_by(SharingLink.id.desc())
    )
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    items: list[InvitePublic] = []
    for link in rows:
        partner: InvitePartnerBrief | None = None
        if link.partner_user_id is not None:
            row = db.get(User, link.partner_user_id)
            partner = InvitePartnerBrief(
                user_id=link.partner_user_id,
                name=row.name if row is not None else None,
            )
        items.append(
            InvitePublic(
                id=link.id,
                email=link.email,
                status=link.status.value,
                account_ids=list(link.account_ids or []),
                expires_at=link.expires_at,
                created_at=link.created_at,
                accepted_at=link.accepted_at,
                revoked_at=link.revoked_at,
                partner=partner,
            )
        )
    return {"items": items, "total": total, "limit": pagination.limit, "offset": pagination.offset}


@router.get("/audit")
def list_audit(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> dict:
    """Owner's own receipts, newest first (docs/43 §3) — `user_id ==
    current_user.id` on the query: there is no cross-account read of this
    table anywhere (AGENTS §2)."""
    stmt = (
        select(SharingAudit)
        .where(SharingAudit.user_id == current_user.id)
        .order_by(SharingAudit.created_at.desc(), SharingAudit.id.desc())
    )
    rows, total = paginate(db, stmt, limit=pagination.limit, offset=pagination.offset)
    items = [
        SharingAuditPublic(
            id=row.id,
            event=row.event,
            actor_user_id=row.actor_user_id,
            invite_id=row.invite_id,
            args_hash=row.args_hash,
            response_code=row.response_code,
            duration_ms=row.duration_ms,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return {"items": items, "total": total, "limit": pagination.limit, "offset": pagination.offset}


# --- owner mutations --------------------------------------------------------


@router.post("/invites", response_model=InviteCreated, status_code=201)
@human_paced_limit(SHARING_INVITE_LIMIT)
def create_invite(
    request: Request,
    body: InviteCreateBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> InviteCreated:
    """Create a pending invite (docs/43 §5).

    Checks in order: (1) demo write guard — the dependency, before this
    handler; (2) rate 5/min — the decorator; (3) inviting the demo tenant;
    (4) self-invite; (5) an active link exists; (6) a pending invite for
    that address; (7) ≥5 outstanding pending; (8) an `account_ids` entry not
    owned. Any address passing (3)-(8) answers `201` — never reveals
    whether it has an account (docs/42 enumeration-oracle rule).
    """
    started = time.monotonic()
    owner_id = current_user.id
    invite_id: int | None = None
    try:
        email = _normalize_email(str(body.email))
        if email == DEMO_EMAIL:
            raise api_error(403, "The demo account cannot be invited", "demo_account_restricted")
        if email == _normalize_email(current_user.email):
            raise api_error(403, "You cannot invite your own address", "sharing_self_invite")
        if _owner_has_active(db, owner_id):
            raise api_error(409, "An active sharing link already exists", "sharing_partner_exists")
        duplicate = db.exec(
            select(SharingLink.id)
            .where(
                SharingLink.user_id == owner_id,
                SharingLink.status == SharingLinkStatus.pending,
                SharingLink.email == email,
            )
            .limit(1)
        ).first()
        if duplicate is not None:
            raise api_error(
                409, "A pending invite for this address already exists", "sharing_invite_exists"
            )
        pending = db.exec(
            select(func.count())
            .select_from(SharingLink)
            .where(
                SharingLink.user_id == owner_id,
                SharingLink.status == SharingLinkStatus.pending,
            )
        ).one()
        if pending >= PENDING_INVITE_CAP:
            raise api_error(
                400,
                f"At most {PENDING_INVITE_CAP} pending invites per account",
                "sharing_invite_cap",
            )
        account_ids = _dedupe(body.account_ids)
        _owned_account_ids(db, owner_id, account_ids)

        token = secrets.token_urlsafe(32)
        link = SharingLink(
            user_id=owner_id,
            email=email,
            token_hash=hash_invite_token(token),
            account_ids=account_ids,
            expires_at=datetime.now(UTC) + INVITE_TTL,
        )
        db.add(link)
        db.flush()  # link.id for the receipt, same transaction
        _commit_state_and_receipt(
            db,
            "invite_create",
            make_receipt=lambda: SharingAudit(
                user_id=owner_id,
                actor_user_id=owner_id,
                invite_id=link.id,
                event="invite_create",
                args_hash=_args_hash(body.model_dump()),
                response_code=201,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            # rollback expunged the pending row — re-add the same object
            # (its field values survive; a fresh id is assigned on commit).
            reapply=lambda: db.add(link),
        )
        db.refresh(link)
    except APIError as exc:
        _record_receipt(
            db,
            owner_id=owner_id,
            actor_id=owner_id,
            invite_id=invite_id,
            event="invite_create",
            body=body.model_dump(),
            response_code=exc.status_code,
            started=started,
        )
        raise

    settings = get_settings()
    accept_url = f"{settings.app_origin.rstrip('/')}{ACCEPT_ROUTE}?token={token}"
    # D4 / docs/43 §9.3: the OWNER's privacy mode gates the mail outright;
    # SMTP unconfigured does the same. Neither is ever an error.
    email_sent = False
    if not current_user.privacy_mode:
        # M175 (docs/07 §Provider-wait ledger): `db.refresh(link)` above
        # autobegan and SMTP is a 10 s wait. The invite is already committed, so
        # this releases a read — the send's own outcome is reported, never
        # rolled back, which is the point of sending AFTER the commit.
        release_before_provider_wait(db)
        email_sent = send_invite_email(email, accept_url, settings)
    return InviteCreated(
        id=link.id,
        email=link.email,
        status=link.status.value,
        account_ids=list(link.account_ids or []),
        expires_at=link.expires_at,
        created_at=link.created_at,
        token=token,
        accept_url=accept_url,
        email_sent=email_sent,
    )


@router.delete("/invites/{invite_id}", status_code=204)
@human_paced_limit(SHARING_MUTATE_LIMIT)
def cancel_invite(
    request: Request,
    invite_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Owner changed their mind before acceptance → `cancelled` (terminal)."""
    started = time.monotonic()
    owner_id = current_user.id
    try:
        link = db.get(SharingLink, invite_id)
        if link is None or link.user_id != owner_id:
            raise api_error(404, "Invite not found", "sharing_missing")
        if link.status != SharingLinkStatus.pending:
            raise api_error(409, "Only a pending invite can be cancelled", "sharing_not_active")
        link.status = SharingLinkStatus.cancelled
        db.add(link)
        _commit_state_and_receipt(
            db,
            "invite_cancel",
            make_receipt=lambda: SharingAudit(
                user_id=owner_id,
                actor_user_id=owner_id,
                invite_id=link.id,
                event="invite_cancel",
                args_hash=_args_hash({}),
                response_code=204,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            reapply=lambda: (
                setattr(link, "status", SharingLinkStatus.cancelled),
                db.add(link),
            ),
        )
    except APIError as exc:
        _record_receipt(
            db,
            owner_id=owner_id,
            actor_id=owner_id,
            invite_id=invite_id,
            event="invite_cancel",
            body={},
            response_code=exc.status_code,
            started=started,
        )
        raise


@router.patch("/scope", response_model=SharingState)
@human_paced_limit(SHARING_MUTATE_LIMIT)
def update_scope(
    request: Request,
    body: ScopeUpdateBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SharingState:
    """Re-pick the allowlist on the LIVE link — adding an account is always
    a deliberate PATCH, never an "all" toggle (docs/43 §2).

    State, not recency (M98 F-1): `status='active'` is the only row a scope
    edit may write, because it is the row every partner read re-resolves —
    writing anywhere else was a silent no-op that reported `200` while the
    partner kept the old scope. A pending invite is addressed by its OWN id
    (`DELETE /sharing/invites/{id}`), never by an owner-level "the row", and
    its allowlist is set at creation (`POST /sharing/invites.account_ids`);
    with no live link there is nothing to scope, so this answers the
    byte-identical `404 sharing_not_linked` family (docs/43 §7) rather than
    writing to a row no partner ever reads.
    """
    started = time.monotonic()
    owner_id = current_user.id
    link_id: int | None = None
    try:
        link = _active_link(db, owner_id)
        if link is None:
            raise api_error(
                404, "No shared household is linked to this account", "sharing_not_linked"
            )
        link_id = link.id
        account_ids = _dedupe(body.account_ids)
        _owned_account_ids(db, owner_id, account_ids)
        link.account_ids = account_ids
        db.add(link)
        _commit_state_and_receipt(
            db,
            "scope_update",
            make_receipt=lambda: SharingAudit(
                user_id=owner_id,
                actor_user_id=owner_id,
                invite_id=link.id,
                event="scope_update",
                args_hash=_args_hash(body.model_dump()),
                response_code=200,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            reapply=lambda: (
                setattr(link, "account_ids", account_ids),
                db.add(link),
            ),
        )
        db.refresh(link)
    except APIError as exc:
        _record_receipt(
            db,
            owner_id=owner_id,
            actor_id=owner_id,
            event="scope_update",
            body=body.model_dump(),
            response_code=exc.status_code,
            started=started,
            invite_id=link_id,
        )
        raise
    return _state_of(db, link)


@router.post("/revoke", response_model=SharingState)
@human_paced_limit(SHARING_MUTATE_LIMIT)
def revoke(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SharingState:
    """Consent withdrawal — M79 semantics (docs/39 §8): a local,
    authoritative, idempotent status write. `200` the first time and `200`
    on every repeat; the very next partner DATA request is
    `401 sharing_not_linked` because every partner read re-resolves the
    `WHERE partner_user_id = ? AND status = 'active'` row — while
    `GET /partner/state` flips to `200 {linked: false}` (M92, docs/43 §6).

    Targets by STATE (M98 F-1): the `active` row, always — a newer pending
    invite used to win newest-first and turn this into a permanent `409`
    while the partner kept live access. The repeat answers on the
    partnership that ended most recently (M79); no live link and nothing
    ever ended → `404 sharing_not_linked`, never a `409` a cancel cannot
    clear, never a `200` that leaves an active link in place.
    """
    started = time.monotonic()
    owner_id = current_user.id
    link_id: int | None = None
    try:
        link = _active_link(db, owner_id)
        if link is None:
            link = _ended_link(db, owner_id)
            if link is None:
                raise api_error(
                    404, "No shared household is linked to this account", "sharing_not_linked"
                )
        link_id = link.id
        if link.status == SharingLinkStatus.active:
            link.status = SharingLinkStatus.revoked
            link.revoked_at = datetime.now(UTC)
            db.add(link)
        revoked_status, revoked_at = link.status, link.revoked_at

        def _reapply_revoke() -> None:
            link.status = revoked_status
            link.revoked_at = revoked_at
            db.add(link)

        _commit_state_and_receipt(
            db,
            "revoke",
            make_receipt=lambda: SharingAudit(
                user_id=owner_id,
                actor_user_id=owner_id,
                invite_id=link.id,
                event="revoke",
                args_hash=_args_hash({}),
                response_code=200,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            reapply=_reapply_revoke,
        )
        db.refresh(link)
    except APIError as exc:
        _record_receipt(
            db,
            owner_id=owner_id,
            actor_id=owner_id,
            event="revoke",
            body={},
            response_code=exc.status_code,
            started=started,
            invite_id=link_id,
        )
        raise
    return _state_of(db, link)


# --- the invitee's mutations (docs/43 §5) -----------------------------------


@router.post("/invites/accept", response_model=SharingState)
@human_paced_limit(SHARING_MUTATE_LIMIT)
def accept_invite(
    request: Request,
    body: AcceptBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SharingState:
    """Consume a single-use invite token. Lives on the SHARING router so the
    partner read surface stays literally GET-only (docs/43 §5).

    Checks in order: (1) demo guard — dependency; (2) rate 10/min —
    decorator; (3) unknown token → `401 sharing_invite_invalid`; (4) token
    not addressed to the signed-in account → the SAME code, so a token
    holder learns nothing about which address was invited; (5) invitee ==
    owner → `403 sharing_self_invite`; (6) expired → materialize `expired`,
    `401 sharing_invite_expired`; (7) cancelled → `409`; (8) already
    accepted → `409 sharing_invite_used`; (9) the OWNER's seat already holds
    an active link (create check (5) mirrored) → `409
    sharing_partner_exists`; (10) the invitee is already SOME OTHER owner's
    active partner → `409 sharing_partner_occupied`.

    When both accepts of a race pass (9) and (10), the PARTIAL UNIQUE INDEX
    decides and the loser's answer is attributed to the seat that refused
    it (M98 F-6): owner seat → `409 sharing_invite_used` (an owner-side
    conflict: the owner's single seat was consumed by the winning accept —
    never the partner-side `sharing_partner_occupied`, which would tell the
    invitee the opposite party was the problem); partner seat → `409
    sharing_partner_occupied`. Receipt actor is unchanged on both arms: the
    invitee acted (D7).

    A receipt is written only when `token_hash` resolves to a row — an
    unknown token resolves to no tenant and `user_id` must be the owner, so
    there is nowhere to attribute it (documented, not hidden).
    """
    started = time.monotonic()
    link = db.exec(
        select(SharingLink).where(SharingLink.token_hash == hash_invite_token(body.token))
    ).first()
    if link is None:
        # Same envelope as (4) — one code for both (docs/43 §7).
        raise api_error(401, "Invitation token is not valid", "sharing_invite_invalid")
    owner_id = link.user_id
    try:
        if _normalize_email(current_user.email) != link.email:
            raise api_error(401, "Invitation token is not valid", "sharing_invite_invalid")
        if owner_id == current_user.id:
            raise api_error(403, "You cannot accept your own invite", "sharing_self_invite")
        now = datetime.now(UTC)
        if link.status == SharingLinkStatus.expired or (
            link.status == SharingLinkStatus.pending and link.expires_at < now
        ):
            if link.status == SharingLinkStatus.pending:
                # Materialize the TTL transition (docs/43 §4); the refusal
                # receipt below commits it with itself.
                link.status = SharingLinkStatus.expired
                db.add(link)
            raise api_error(401, "This invitation has expired", "sharing_invite_expired")
        if link.status == SharingLinkStatus.cancelled:
            raise api_error(409, "This invitation was cancelled", "sharing_invite_cancelled")
        if link.status in (SharingLinkStatus.active, SharingLinkStatus.revoked):
            # Single-use: the token was consumed by the accept that flipped
            # the row — a revoked link was accepted first, then withdrawn.
            raise api_error(409, "This invitation has already been used", "sharing_invite_used")
        # Create check (5) mirrored: the owner's other pending invites must
        # not be acceptable once that owner holds an active link (the partial
        # unique index would refuse it anyway — answer with the pinned code).
        if _owner_has_active(db, owner_id):
            raise api_error(409, "This household already has a partner", "sharing_partner_exists")
        if _partner_occupied(db, current_user.id, exclude_link_id=link.id):
            raise api_error(409, "You already share another household", "sharing_partner_occupied")

        link.partner_user_id = current_user.id
        link.accepted_at = now
        link.status = SharingLinkStatus.active
        db.add(link)

        def _reapply_accept() -> None:
            link.partner_user_id = current_user.id
            link.accepted_at = now
            link.status = SharingLinkStatus.active
            db.add(link)

        _commit_state_and_receipt(
            db,
            "invite_accept",
            make_receipt=lambda: SharingAudit(
                user_id=owner_id,
                actor_user_id=current_user.id,  # D7 — the partner acted
                invite_id=link.id,
                event="invite_accept",
                args_hash=_args_hash(body.model_dump()),
                response_code=200,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            reapply=_reapply_accept,
        )
        db.refresh(link)
    except APIError as exc:
        _record_receipt(
            db,
            owner_id=owner_id,
            actor_id=current_user.id,
            invite_id=link.id,
            event="invite_accept",
            body=body.model_dump(),
            response_code=exc.status_code,
            started=started,
        )
        raise
    except IntegrityError:
        # I2: the partial unique index resolved the race — the loser gets a
        # pinned and correctly ATTRIBUTED answer, never a 500 (M98 F-6).
        # Which seat refused it is read back from the winner's committed row
        # (the conflict can only surface once that row is visible), never
        # guessed: the OWNER's seat is an owner-side conflict →
        # `sharing_invite_used`, because the owner's single seat was already
        # consumed by the winning accept; the PARTNER's seat is the
        # partner-side conflict → `sharing_partner_occupied`.
        db.rollback()
        if _active_link(db, owner_id) is not None:
            detail, code = "This invitation has already been used", "sharing_invite_used"
        else:
            # The only other seat on the table is the invitee's
            # (`uq_sharing_links_active_partner`) — the partner-side
            # conflict, whose partner-side code this has always been.
            detail, code = "You already share another household", "sharing_partner_occupied"
        _record_receipt(
            db,
            owner_id=owner_id,
            actor_id=current_user.id,
            invite_id=link.id,
            event="invite_accept",
            body=body.model_dump(),
            response_code=409,
            started=started,
        )
        raise api_error(409, detail, code) from None
    return _state_of(db, link)


@router.post("/leave", response_model=SharingState)
@human_paced_limit(SHARING_MUTATE_LIMIT)
def leave(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SharingState:
    """Consent withdrawal from the ACCEPT side — M90 D6, ruled a one-route
    follow-up and shipped here (docs/43 §1/§5).

    The owner has `revoke`; this is the partner's mirror of it, and it
    mutates ONLY the row whose `partner_user_id` is the caller: a user who is
    an owner on one link and a partner on another loses exactly the
    partnership they are ending, and no other tenant's row is reachable from
    this query (AGENTS §2 — every read of the table carries a `user_id` OR a
    `partner_user_id` predicate, I1).

    Semantics are revoke's, M79 (docs/39 §8): a local, authoritative,
    idempotent status write — `200` the first time and `200` on every repeat
    — setting `status='revoked'` + `revoked_at`. It is deliberately the SAME
    terminal state `revoke` writes, so the ended link renders today's
    "Nobody has access" / "access was removed" path on both sides with no
    second UI. What distinguishes the two acts is the receipt: `event="leave"`
    with `actor_user_id = partner`, next to `revoke` with the owner.

    A caller whose account holds no partnership answers `404
    sharing_not_linked` — status 404 (not 401) because `sharing_not_linked`
    here is "no such link FOR YOU", and a 401 from a mutating path would run
    the client's refresh → /login dance on a perfectly valid session
    (docs/43 §7). The BODY is byte-identical to the partner read's envelope,
    so the answer never distinguishes "you were revoked" from "you never had
    access". That refusal writes NO receipt: `sharing_audit.user_id` must be
    the owner and no row names one (the accept route's documented rule, same
    reasoning — documented, not hidden).

    Targets by STATE (M98 F-2): `partner_user_id = me AND status='active'` —
    the very predicate every partner read re-resolves — so the row that
    ends is the row that GRANTED access, and every `leave` receipt names it.
    Newest-first here answered `200` for an older terminal row as a no-op
    while the live link survived and `/partner/state` stayed `linked: true`
    (fake consent withdrawal). The M79 repeat answers on the partnership
    that ended most recently.
    """
    started = time.monotonic()
    partner_id = current_user.id
    link = _active_partner_link(db, partner_id)
    if link is None:
        link = _ended_partner_link(db, partner_id)
    if link is None:
        raise api_error(404, "No shared household is linked to this account", "sharing_not_linked")
    owner_id = link.user_id
    link_id = link.id
    try:
        if link.status == SharingLinkStatus.active:
            link.status = SharingLinkStatus.revoked
            link.revoked_at = datetime.now(UTC)
            db.add(link)
        # `_active_partner_link` returned active and `_ended_partner_link`
        # returned revoked, so no third status is reachable here — a future
        # one simply does not resolve and answers the 404 above (never a 500).
        revoked_status, revoked_at = link.status, link.revoked_at

        def _reapply_leave() -> None:
            link.status = revoked_status
            link.revoked_at = revoked_at
            db.add(link)

        _commit_state_and_receipt(
            db,
            "leave",
            make_receipt=lambda: SharingAudit(
                user_id=owner_id,
                actor_user_id=partner_id,  # D7 — the partner acted
                invite_id=link.id,
                event="leave",
                args_hash=_args_hash({}),
                response_code=200,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            reapply=_reapply_leave,
        )
        db.refresh(link)
    except APIError as exc:
        _record_receipt(
            db,
            owner_id=owner_id,
            actor_id=partner_id,
            invite_id=link_id,
            event="leave",
            body={},
            response_code=exc.status_code,
            started=started,
        )
        raise
    return _state_of(db, link)


# Re-exported for the router-GET-only test: every path this module serves
# (docs/43 §6 pins the PARTNER router to GET; this one owns the mutations).
MUTATING_EVENTS = (
    "invite_create",
    "invite_cancel",
    "invite_accept",
    "scope_update",
    "revoke",
    "leave",
)
