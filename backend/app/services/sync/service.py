"""Bank-sync orchestration (docs/39): consent -> pull -> stage -> status.

Rules this module exists to keep:

1. **It is the only caller of a `Provider`.** The privacy-mode egress guard
   (`_guard_sync_egress`) runs before every provider call, so bank sync is
   destination 6 in docs/11's egress inventory and is sealed by the same
   `privacy_mode` ContextVar that already seals BoC Valet and Yahoo.
2. **It never writes a transaction.** Rows go through
   `services.import_staging.stage_rows` — the exact code path `POST
   /imports/upload` uses — so bank sync can never become a parallel write path
   (AGENTS §2 / docs/39 § Ingestion parity).
3. **Revocation is authoritative locally.** The agreement stops pulling even if
   the provider's own revoke call fails; the failure is recorded in
   `last_error` rather than blocking the user from disconnecting.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession
from sqlmodel import select

from app.core.errors import api_error
from app.importers.base import RawTransaction
from app.models.accounts import Account
from app.models.institutions import Institution
from app.models.sync_agreements import (
    ProviderStatus,
    SyncAgreement,
    SyncAgreementStatus,
)
from app.models.users import User
from app.services.fx import privacy_locked
from app.services.import_staging import StagedBatch, StagedRow, stage_rows
from app.services.sync.provider import (
    Provider,
    ProviderError,
    ProviderRevoked,
    get_provider,
)
from app.services.sync.types import (
    ProviderAccount,
    ProviderInstitution,
    ProviderUser,
)


def _write_session(db: Session) -> SQLModelSession:
    """Open a writer on the CALLER'S bind — never the maintenance engine.

    The request session's bind is the request engine (`get_request_engine()`:
    the unprivileged `fintrack_app` role whenever `APP_DATABASE_URL` is set),
    so a request-path commit here runs under the same tenant stamp and the
    same row-level security as every other statement of that request
    (docs/55 §Roles). Falling back to `get_engine()` would have put `pull`,
    `create_agreement` and `revoke` through the schema owner, which the
    policies never constrain.

    A `Connection` bind is the test harness's outer transaction: the writer
    joins it as a savepoint so the harness can still roll the whole thing
    back.
    """
    bind = db.get_bind()
    if isinstance(bind, Connection):
        return SQLModelSession(
            bind=bind,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
    # Engine bind (the production shape): the SAME engine the request session
    # was built on — a fresh pooled connection, already stamped by that
    # engine's `begin` listener with the current request's tenant.
    return SQLModelSession(bind=bind, expire_on_commit=False)


def _guard_sync_egress() -> None:
    """The single bank-sync egress seam — cache/status reads never reach it.

    Reuses the `services/fx.py` request-scoped lock rather than a second
    ContextVar: `deps.get_current_user` binds `privacy_mode` for every request,
    so one flag, one vocabulary (`privacy_mode_blocks_external_api`), one place
    to audit (docs/11 § egress inventory).
    """
    if privacy_locked():
        raise api_error(
            403,
            "Privacy mode blocks bank sync; manual entry and file imports keep working",
            "privacy_mode_blocks_external_api",
        )


def provider_or_404(slug: str) -> Provider:
    provider = get_provider(slug)
    if provider is None:
        raise api_error(404, f"Unknown sync provider: {slug}", "sync_provider_unknown")
    return provider


def _account_projections(accounts: Sequence[Account]) -> list[ProviderAccount]:
    return [
        ProviderAccount(local_id=a.id, currency=a.currency, account_type=a.type.value)
        for a in accounts
    ]


def _institution_name(db: Session, account: Account) -> str:
    """Same resolution `POST /imports/upload` uses for `import_batches.institution`."""
    if account.institution_id is None:
        return ""
    institution = db.get(Institution, account.institution_id)
    return institution.name if institution else ""


def _owned_accounts(db: Session, user: User, account_ids: Sequence[int]) -> list[Account]:
    """Resolve + validate requested account ids (404 on the first unknown)."""
    accounts: list[Account] = []
    for account_id in account_ids:
        account = db.get(Account, account_id)
        if account is None or account.user_id != user.id:
            raise api_error(404, f"Account {account_id} not found", "not_found")
        accounts.append(account)
    return accounts


def _linked_accounts(db: Session, agreement: SyncAgreement) -> list[Account]:
    """Accounts still present for this agreement (dangling refs are dropped)."""
    ids = sorted(set(agreement.account_refs.values()))
    if not ids:
        return []
    rows = db.exec(
        select(Account).where(Account.user_id == agreement.user_id).where(Account.id.in_(ids))
    ).all()
    return list(rows)


def get_agreement(db: Session, user: User, agreement_id: int) -> SyncAgreement:
    """Owned-or-404 with the sync-specific code (docs/39 error table)."""
    agreement = db.get(SyncAgreement, agreement_id)
    if agreement is None or agreement.user_id != user.id:
        raise api_error(404, "Sync agreement not found", "sync_agreement_missing")
    return agreement


def list_agreements(db: Session, user: User) -> list[SyncAgreement]:
    return list(
        db.exec(
            select(SyncAgreement)
            .where(SyncAgreement.user_id == user.id)
            .order_by(SyncAgreement.id.desc())
        ).all()
    )


def list_institutions(db: Session, slug: str) -> list[ProviderInstitution]:
    provider = provider_or_404(slug)
    # M175 (docs/07 §Provider-wait ledger): the provider call is a wait, and
    # the request session must not be held across it — `get_current_user` has
    # already run its one query on THIS session (deps.get_db), so the
    # connection would sit `idle in transaction` for the whole dispatch and
    # pin a pool slot. Nothing is written on this session, so closing it
    # releases the connection without committing anything (docs/01 §Sessions).
    # Same shape as the create/pull/revoke dispatch below.
    db.close()
    _guard_sync_egress()
    try:
        return list(provider.list_institutions())
    except ProviderError as exc:
        raise api_error(exc.status_code, exc.detail, exc.code) from exc


def create_agreement(
    db: Session, user: User, slug: str, account_ids: Sequence[int]
) -> SyncAgreement:
    """Register consent for `account_ids` with `slug` (docs/39 § Consent)."""
    provider = provider_or_404(slug)
    if not account_ids:
        raise api_error(422, "account_ids must contain at least one account", "validation_error")
    # Preserve the caller's order (deduped, first win): `account_ids[i]` decides
    # which provider ref the adapter mints for it (docs/39 § Consent).
    unique_ids = list(dict.fromkeys(account_ids))
    if user.id is None:  # get_current_user always loads a persisted row
        raise api_error(500, "session user has no id", "internal_error")
    user_id = user.id
    accounts = _owned_accounts(db, user, unique_ids)
    account_projections = _account_projections(accounts)

    existing = db.exec(
        select(SyncAgreement)
        .where(SyncAgreement.user_id == user.id)
        .where(SyncAgreement.provider == slug)
        .where(SyncAgreement.status != SyncAgreementStatus.revoked)
    ).first()
    if existing is not None:
        raise api_error(
            409,
            f"A {slug} agreement is already active for this account set",
            "sync_agreement_exists",
        )

    db.close()
    _guard_sync_egress()
    try:
        grant = provider.create_agreement(ProviderUser(id=user_id), account_projections)
    except ProviderError as exc:
        raise api_error(exc.status_code, exc.detail, exc.code) from exc

    # Strict validation beats silent tolerance: an adapter that fails to map a
    # requested account would otherwise drop it from every future pull with no
    # signal at all.
    requested = {account.local_id for account in account_projections}
    mapped = set(grant.account_refs.values())
    if mapped != requested:
        missing = sorted(requested - mapped)
        raise api_error(
            422,
            f"Provider did not map requested account(s): {missing}",
            "sync_account_mapping_incomplete",
        )

    with _write_session(db) as write_db:
        current_user = write_db.get(User, user_id)
        if current_user is None:
            raise api_error(404, "User not found", "not_found") from None
        _owned_accounts(write_db, current_user, unique_ids)
        existing = write_db.exec(
            select(SyncAgreement)
            .where(SyncAgreement.user_id == user_id)
            .where(SyncAgreement.provider == slug)
            .where(SyncAgreement.status != SyncAgreementStatus.revoked)
        ).first()
        if existing is not None:
            raise api_error(
                409,
                f"A {slug} agreement is already active for this account set",
                "sync_agreement_exists",
            )
        agreement = SyncAgreement(
            user_id=user_id,
            provider=slug,
            consent_id=grant.consent_id,
            status=SyncAgreementStatus.active,
            account_refs=dict(grant.account_refs),
            scopes=list(grant.scopes),
        )
        write_db.add(agreement)
        write_db.commit()
        write_db.refresh(agreement)
        return agreement


def _record_failure(db: Session, agreement: SyncAgreement, exc: ProviderError) -> None:
    """Persist what the provider just said so the status page can show it."""
    now = datetime.now(UTC)
    if isinstance(exc, ProviderRevoked):
        agreement.status = SyncAgreementStatus.revoked
        agreement.revoked_at = now
        agreement.provider_status = None
        account_status = None
    elif exc.code == "sync_auth_required":
        agreement.status = SyncAgreementStatus.auth_required
        agreement.provider_status = ProviderStatus.auth_required
        account_status = ProviderStatus.auth_required
    else:
        # Retryable: the consent is still good, the call failed.
        agreement.provider_status = ProviderStatus.degraded
        account_status = ProviderStatus.degraded
    agreement.last_error = exc.detail[:255]
    for account in _linked_accounts(db, agreement):
        account.provider_status = account_status
    db.commit()


def pull(db: Session, user: User, agreement_id: int) -> PullOutcome:
    """Pull deltas for one agreement and stage them (docs/39 § Pull)."""
    agreement = get_agreement(db, user, agreement_id)
    if agreement.status == SyncAgreementStatus.revoked:
        raise api_error(409, "This agreement was revoked", "sync_agreement_revoked")
    if agreement.status == SyncAgreementStatus.auth_required:
        raise api_error(409, "Re-authorization is required before pulling", "sync_auth_required")

    if user.id is None:  # get_current_user always loads a persisted row
        raise api_error(500, "session user has no id", "internal_error")
    user_id = user.id
    provider = provider_or_404(agreement.provider)
    since = agreement.last_sync_at
    agreement_provider = agreement.provider
    account_refs = dict(agreement.account_refs)
    db.close()
    _guard_sync_egress()
    try:
        batch = provider.pull_delta(ProviderUser(id=user_id), since)
    except ProviderError as exc:
        with _write_session(db) as write_db:
            current_user = write_db.get(User, user_id)
            if current_user is None:
                raise api_error(404, "User not found", "not_found") from None
            current = get_agreement(write_db, current_user, agreement_id)
            if current.status != SyncAgreementStatus.revoked:
                _record_failure(write_db, current, exc)
        raise api_error(exc.status_code, exc.detail, exc.code) from exc

    grouped: dict[int, list[RawTransaction]] = defaultdict(list)
    balances: list[SyncBalance] = []
    dropped = 0
    staged: list[StagedBatch] = []
    with _write_session(db) as write_db:
        current_user = write_db.get(User, user_id)
        if current_user is None:
            raise api_error(404, "User not found", "not_found") from None
        agreement = get_agreement(write_db, current_user, agreement_id)
        if agreement.status == SyncAgreementStatus.revoked:
            raise api_error(409, "This agreement was revoked", "sync_agreement_revoked")
        accounts = {account.id: account for account in _linked_accounts(write_db, agreement)}
        ref_to_account = {
            ref: accounts[local_id]
            for ref, local_id in account_refs.items()
            if local_id in accounts
        }
        for txn in batch.transactions:
            account = ref_to_account.get(txn.account_ref)
            if account is None:
                dropped += 1
                continue
            grouped[account.id].append(
                RawTransaction(
                    date=txn.date,
                    amount_native=txn.amount_native,
                    raw_description=txn.description,
                    currency=txn.currency,
                    posting_date=txn.posting_date,
                    external_ref=txn.external_id,
                )
            )
        for bal in batch.balances:
            account = ref_to_account.get(bal.account_ref)
            if account is not None:
                balances.append(
                    SyncBalance(
                        account_id=account.id,
                        amount=bal.amount,
                        currency=bal.currency,
                        as_of=bal.as_of,
                    )
                )
        for account_id in sorted(grouped):
            account = accounts[account_id]
            staged.append(
                stage_rows(
                    write_db,
                    user=current_user,
                    account=account,
                    institution_name=_institution_name(write_db, account),
                    file_name=f"bank-sync:{agreement_provider}",
                    rows=grouped[account_id],
                    source_label="Bank sync",
                    system_actor=True,
                )
            )
        now = datetime.now(UTC)
        agreement.last_sync_at = now
        agreement.provider_status = batch.provider_status
        agreement.last_error = None
        for account in accounts.values():
            account.last_sync_at = now
            account.provider_status = batch.provider_status
        write_db.commit()
        write_db.refresh(agreement)
        return PullOutcome(
            agreement=agreement, staged=staged, balances=balances, dropped_rows=dropped
        )


def revoke(db: Session, user: User, agreement_id: int) -> SyncAgreement:
    """Disconnect locally AND at the provider; local revocation always wins."""
    agreement = get_agreement(db, user, agreement_id)
    if agreement.status == SyncAgreementStatus.revoked:
        return agreement  # idempotent: a second revoke is not an error

    provider = provider_or_404(agreement.provider)
    if user.id is None:  # get_current_user always loads a persisted row
        raise api_error(500, "session user has no id", "internal_error")
    user_id = user.id
    db.close()
    _guard_sync_egress()
    provider_error: ProviderError | None = None
    try:
        provider.revoke(ProviderUser(id=user_id))
    except ProviderError as exc:
        # The user asked to disconnect; a provider outage must not be able to
        # refuse that. Recorded, not blocking (docs/39 § Revocation).
        provider_error = exc

    with _write_session(db) as write_db:
        current_user = write_db.get(User, user_id)
        if current_user is None:
            raise api_error(404, "User not found", "not_found") from None
        agreement = get_agreement(write_db, current_user, agreement_id)
        if agreement.status == SyncAgreementStatus.revoked:
            return agreement
        now = datetime.now(UTC)
        agreement.status = SyncAgreementStatus.revoked
        agreement.revoked_at = now
        agreement.provider_status = None
        agreement.last_error = provider_error.detail[:255] if provider_error else None
        for account in _linked_accounts(write_db, agreement):
            account.provider_status = None
        write_db.commit()
        write_db.refresh(agreement)
        return agreement


@dataclass(frozen=True)
class SyncBalance:
    """A pulled balance resolved to a local account id — reported, never stored."""

    account_id: int
    amount: Decimal
    currency: str
    as_of: date


@dataclass
class PullOutcome:
    """Everything the router needs to answer one pull."""

    agreement: SyncAgreement
    staged: list[StagedBatch]
    balances: list[SyncBalance]
    dropped_rows: int

    @property
    def batch_ids(self) -> list[int]:
        return [item.batch.id for item in self.staged]

    @property
    def rows(self) -> list[StagedRow]:
        return [row for item in self.staged for row in item.rows]

    @property
    def imported(self) -> int:
        return sum(item.batch.imported for item in self.staged)

    @property
    def duplicates_skipped(self) -> int:
        return sum(item.batch.duplicates_skipped for item in self.staged)

    @property
    def unresolved(self) -> int:
        return sum(item.batch.unresolved for item in self.staged)


__all__ = [
    "PullOutcome",
    "SyncBalance",
    "create_agreement",
    "get_agreement",
    "list_agreements",
    "list_institutions",
    "provider_or_404",
    "pull",
    "revoke",
]
