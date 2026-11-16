"""sharing_links + sharing_audit — household sharing (M90, docs/43).

Two roles, exactly one link per seat: `user_id` is the OWNER (the data
subject), `partner_user_id` is the PARTNER (read-only projection consumer).
This is the first feature where a second authenticated user reads a first
user's rows, so both tables carry `user_id` = owner and join
`_USER_OWNED_MODELS` (a deleted account and the demo reset take them with
it) — AGENTS §2 holds literally.

Deliberate column decisions (docs/43 §3):

* `email` is plaintext BY DESIGN — the owner's own record of whom they
  invited. The invitee's address reaches `sharing_audit` only as
  `args_hash` (sha256, byte-identical formula to `mcp_audit`).
* `token_hash` is never nulled: keeping the hash lets a replay be answered
  deterministically (`409 sharing_invite_used`) instead of looking like an
  unknown token. The raw token exists only in the `201` body and the email.
* `actor_user_id` / `invite_id` on the audit carry NO foreign key — the
  `mcp_audit.mcp_token_id` rule (docs/40 §3): deleting an account must not
  delete the receipts it produced. D7: `actor_user_id` is the one column
  `mcp_audit` has no analogue for, because there the actor IS the subject
  and here they are two different people.
* Active seats are DB-enforced partial unique indexes, not application
  checks that can race: at most one `active` row per owner and per partner
  (I2 — the accept race is decided by the database).
"""

import enum
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Enum, Index, desc, text
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class SharingLinkStatus(enum.StrEnum):
    """Consent lifecycle (docs/43 §4). revoked/cancelled/expired are terminal."""

    pending = "pending"
    active = "active"
    revoked = "revoked"
    expired = "expired"
    cancelled = "cancelled"


# docs/43 §4: terminal states never transition — a new relationship is a new
# row (I5). `active` ⇔ partner + accepted_at set (I3); `pending` ⇒ neither (I4).
_ACTIVE_PREDICATE = text("status = 'active'")


class SharingLink(TimestampMixin, table=True):
    __tablename__ = "sharing_links"
    __table_args__ = (
        Index("ix_sharing_links_user", "user_id"),
        Index("ix_sharing_links_partner_status", "partner_user_id", "status"),
        Index("ix_sharing_links_user_created", "user_id", desc("created_at")),
        # I2 — the database, not the application, resolves the accept race.
        Index(
            "uq_sharing_links_active_owner",
            "user_id",
            unique=True,
            postgresql_where=_ACTIVE_PREDICATE,
        ),
        Index(
            "uq_sharing_links_active_partner",
            "partner_user_id",
            unique=True,
            postgresql_where=_ACTIVE_PREDICATE,
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'expired', 'cancelled')",
            name="ck_sharing_links_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    # The OWNER — the data subject (docs/43 §1).
    user_id: int = Field(foreign_key="users.id")
    # NULL while `pending`; set at accept. CASCADE: a deleted partner drops
    # the link while the owner's audit rows survive (they hold no FK).
    partner_user_id: int | None = Field(default=None, foreign_key="users.id", ondelete="CASCADE")
    # Invite target, stored strip().lower() (docs/43 §3).
    email: str = Field(max_length=320)
    # sha256 hex of the raw invite token — never nulled (module docstring).
    token_hash: str = Field(max_length=64, unique=True)
    status: SharingLinkStatus = Field(
        default=SharingLinkStatus.pending,
        sa_column=Column(
            Enum(SharingLinkStatus, native_enum=False, length=16),
            nullable=False,
            server_default="pending",
        ),
    )
    # The owner's allowlist of `accounts.id`: order-preserving,
    # de-duplicated. JSON, not a junction table — the `sync_agreements
    # .account_refs` rule (docs/39): written only through the sharing router,
    # and tenancy + export scoping ride on `user_id`.
    account_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # created_at + 7 days. Informational once `active`.
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    accepted_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    revoked_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class SharingAudit(TimestampMixin, table=True):
    """One receipt per owner-router mutating request — the M80 pattern
    (docs/40 §3) applied to the consent log: what did the owner (or the
    partner, on accept) do, answered with a query instead of a vibe.

    Append-only, best effort: a receipt that fails to write is logged at
    WARNING and the response stands (docs/43 §3) — an unreachable receipt
    sink must never fail a user's invite.
    """

    __tablename__ = "sharing_audit"
    __table_args__ = (Index("ix_sharing_audit_user_created", "user_id", desc("created_at")),)

    id: int | None = Field(default=None, primary_key=True)
    # The OWNER — the data subject, so export and account deletion cover the
    # receipts (docs/43 §3).
    user_id: int = Field(foreign_key="users.id", index=True)
    # Who acted: the owner's id, or the partner's on `invite_accept`. NO FK —
    # deleting that account must never delete a receipt (docs/43 §3, D7).
    actor_user_id: int | None = None
    # The `sharing_links.id` the event concerns. NO FK — link rows die with
    # their users; receipts must not.
    invite_id: int | None = None
    # invite_create | invite_cancel | invite_accept | scope_update | revoke
    event: str = Field(max_length=64)
    # sha256 hex of the canonical request body JSON — the invite body carries
    # the invitee's email, so it is hashed, never stored (docs/43 §3, D8:
    # unsalted sha256, byte-identical formula to `mcp_audit.args_hash`).
    args_hash: str = Field(max_length=64)
    # The status actually returned, refusals included (the `mcp_audit`
    # derivation rule).
    response_code: int
    duration_ms: int
