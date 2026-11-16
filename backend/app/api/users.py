"""users — GET/PATCH /users/me (docs/08)."""

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api.auth import _issue_pair, _set_refresh_cookie
from app.api.deps import get_current_user, get_db
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import api_error
from app.core.ratelimit import CREDENTIAL_MUTATE_LIMIT, human_paced_limit, limiter
from app.core.security import hash_password, verify_password
from app.llm.base import make_provider, resolve_settings
from app.llm.egress import safe_target, select_api_key, validate_base_url
from app.models.acb_pools import AcbPool
from app.models.accounts import Account
from app.models.assistant import AssistantAudit, AssistantProposal
from app.models.auth_factors import OidcLink, RecoveryCode, TotpFactor, WebAuthnCredential
from app.models.bill_reminders import BillReminder
from app.models.budgets import Budget, BudgetCarryover
from app.models.categories import Category
from app.models.debts import Debt
from app.models.dividend_history import DividendHistory, DividendHistoryRefresh
from app.models.etf_holdings import EtfHolding
from app.models.fundamentals import Fundamentals
from app.models.goals import Goal
from app.models.import_batches import ImportBatch
from app.models.institutions import Institution
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion
from app.models.mcp_audit import McpAudit
from app.models.mcp_tokens import McpToken
from app.models.notifications import (
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationInboxRead,
    NotificationSetting,
)
from app.models.prices import Price
from app.models.reconciliation import ReconciliationItem
from app.models.recurring_suppressions import RecurringSuppression
from app.models.refresh_tokens import RefreshToken
from app.models.rule_tags import RuleTag
from app.models.rules import Rule
from app.models.securities import Security
from app.models.sharing import SharingAudit, SharingLink
from app.models.spending_alerts import SpendingAlert
from app.models.sync_agreements import SyncAgreement
from app.models.tags import Tag
from app.models.tax_projections import TaxProjection
from app.models.trades import Trade
from app.models.transaction_events import TransactionEvent
from app.models.transaction_receipts import TransactionReceipt
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import LlmProvider, User
from app.models.watchlist import WatchlistItem
from app.schemas.auth import TokenResponse
from app.schemas.users import (
    ChangePasswordRequest,
    DeleteAccountRequest,
    ProfileUpdate,
    UserPublic,
    UserUpdate,
)
from app.services.demo_seed import DEMO_EMAIL
from app.services.session_release import release_before_provider_wait

router = APIRouter(prefix="/users", tags=["users"])

logger = logging.getLogger(__name__)


class LlmTestRequest(BaseModel):
    """Candidate config for a connection probe. All optional; omitted fields
    fall back to the user's stored settings. Never persisted."""

    llm_provider: LlmProvider | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None


_LLM_TEST_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


@router.post("/me/llm-test")
@limiter.limit("10/minute")
def test_llm_connection(
    request: Request,
    payload: LlmTestRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Probe the configured (or candidate) LLM endpoint with one tiny completion.

    Pure diagnostic — writes nothing. Backs the Settings "Test connection" button.

    Failure contract (H1): everything upstream surfaces as the GENERIC 502
    `llm_upstream_error` envelope — no HTTP status, URL or transport message is
    echoed. Egress-policy violations raised by the send seam keep their own
    codes (422 invalid_llm_base_url / 403 privacy_mode_blocks_external_api).
    """
    kind, base_url, _operator_key, model = resolve_settings(current_user)
    if payload.llm_provider is not None:
        kind = payload.llm_provider
    if payload.llm_base_url is not None:
        base_url = payload.llm_base_url
    if payload.llm_model is not None:
        model = payload.llm_model
    # H1: re-select the key against the FINAL destination — a candidate base
    # URL must never inherit the operator's env key resolved for another URL.
    candidate_key = payload.llm_api_key
    if candidate_key is None and current_user.llm_api_key:
        candidate_key = decrypt_secret(current_user.llm_api_key)
    api_key = select_api_key(base_url, candidate_key)

    provider = make_provider(kind, base_url, api_key, model)
    if provider is None:
        raise api_error(
            400,
            "LLM provider could not be constructed from these settings",
            "llm_test_invalid_config",
        )
    # M175 (docs/07 §Provider-wait ledger): the auth read above autobegan a
    # transaction on THIS request's session (deps.get_db), and `chat()` waits
    # up to 60 s — a "Test connection" burst would pin one pool slot per probe
    # (the M148 shape). Nothing is written and the response is built from the
    # locals already resolved, so this release has nothing pending.
    release_before_provider_wait(db)
    try:
        provider.chat(
            [{"role": "user", "content": "Respond with ok set to true."}],
            _LLM_TEST_SCHEMA,
        )
    except (httpx.HTTPError, ValueError) as exc:
        # Real sends never get here: post_chat_completions already converted
        # upstream failures into the generic envelope. This covers scripted /
        # monkeypatched providers. Details go to the server log only.
        if isinstance(exc, httpx.HTTPStatusError):
            logger.warning(
                "LLM upstream returned HTTP %s for %s",
                exc.response.status_code,
                safe_target(str(exc.request.url)),
            )
        else:
            logger.warning("LLM probe failed (%s)", type(exc).__name__)
        raise api_error(502, "LLM upstream request failed", "llm_upstream_error") from exc
    return {"ok": True, "model": model, "provider": str(kind)}


@router.get("/me", response_model=UserPublic)
def read_me(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    # response_model drops hashed_password / llm_api_key by construction (docs/08).
    return current_user


@router.patch("/me", response_model=UserPublic)
def update_me(
    payload: UserUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    # M8: the shared demo tenant is reset by POST /auth/demo — settings
    # mutations (incl. llm_base_url / privacy_mode) must not stick to it,
    # mirroring the guard on every other self-service mutation (docs/20).
    _assert_not_demo(current_user)
    data = payload.model_dump(exclude_unset=True)
    # docs/11 step 5: privacy mode requires a local LLM. Validate the EFFECTIVE
    # combination after the patch, covering both same-request conflicts and a
    # cloud provider already stored on the row.
    effective_privacy = data.get("privacy_mode", current_user.privacy_mode)
    effective_provider = data.get("llm_provider", current_user.llm_provider)
    if effective_privacy and effective_provider is LlmProvider.openai_compatible:
        raise api_error(
            409,
            "privacy_mode=true requires a local LLM provider (ollama); "
            "switch llm_provider first or drop privacy_mode",
            "privacy_mode_requires_local_llm",
        )
    # H1/H3: validate a user-supplied endpoint at WRITE time (422
    # invalid_llm_base_url — scheme/host policy, loopback-only under privacy).
    # The send seam in app/llm/egress.py re-checks the same policy on every
    # request, so a row stored before a policy change still cannot egress.
    # Falsy values (None/empty) mean "clear" and need no validation.
    if data.get("llm_base_url"):
        validate_base_url(data["llm_base_url"], privacy=bool(effective_privacy))
    # M174 (docs/09 §Tags that mark recurring): the column stores `tags.id`s in
    # a JSON document, so the database cannot carry the FK — ownership is checked
    # HERE or not at all. A nominated tag the caller does not own would silently
    # never match (the detector reads tags through a `user_id`-scoped join), and
    # a silent no-op is exactly what AGENTS §3 refuses. One query, only when the
    # field is actually being written.
    nominated = data.get("recurring_intent_tag_ids")
    if nominated:
        owned = {
            int(tag_id)
            for tag_id in db.exec(select(Tag.id).where(Tag.user_id == current_user.id)).all()
        }
        unknown = sorted(set(int(tag_id) for tag_id in nominated) - owned)
        if unknown:
            raise api_error(
                422,
                "recurring_intent_tag_ids must be tags you own; unknown ids: "
                + ", ".join(str(tag_id) for tag_id in unknown),
                "invalid_recurring_intent_tag_ids",
            )
    # llm_api_key is write-only and stored encrypted at rest (#8); every other
    # field copies through as-is.
    if data.get("llm_api_key") is not None:
        data["llm_api_key"] = encrypt_secret(data["llm_api_key"])
    for field, value in data.items():
        setattr(current_user, field, value)
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return current_user


# Order: child rows before the parent rows they reference (docs/20 cascade).
# New user-owned tables MUST be appended here or their rows will orphan on
# delete. Junction rows (rule_tags, transaction_tags) carry no user_id and are
# cleared by subquery in _delete_user_data before their Rule/Tag/Transaction
# parents.
_USER_OWNED_MODELS = [
    # docs/57: `assistant_audit.proposal_id` -> `assistant_proposals`, so the
    # audit rows are deleted before the proposals they audit.
    AssistantAudit,
    AssistantProposal,
    LlmSuggestion,
    LearnedPayee,
    Trade,
    AcbPool,
    Tag,
    ReconciliationItem,  # references Account + Transaction — before both
    TransactionReceipt,  # M108 P4 — references Transaction, so before it
    Transaction,  # references ImportBatch — before it
    SyncAgreement,  # M79 bank-sync consent (docs/39) — no FK, user_id only
    ImportBatch,
    EtfHolding,
    DividendHistory,
    DividendHistoryRefresh,
    Fundamentals,
    Price,
    WatchlistItem,
    Rule,
    SpendingAlert,  # references Budget — before it
    BudgetCarryover,  # M110 — references Budget, before it
    Budget,  # references Category — before it
    BillReminder,  # references Category — before it
    TaxProjection,
    NotificationSetting,
    NotificationInboxRead,  # M51 inbox read state (docs/35)
    NotificationDeliveryAttempt,  # M59 attempt log -> delivery (docs/37)
    NotificationDelivery,  # M59 outbox (docs/37)
    NotificationChannel,  # M59 transports (docs/37)
    Category,
    Goal,  # references Account — before it
    Debt,  # references Account — before it
    McpToken,
    Account,
    Security,
    Institution,
    RefreshToken,
    # M74 (docs/38): second-factor + SSO rows — a deleted account must leave
    # no factor, no recovery code, no passkey and no provider link behind.
    TotpFactor,
    RecoveryCode,
    WebAuthnCredential,
    OidcLink,
    # M80 (docs/40 §3): the MCP receipt log is tenant data — a deleted
    # account and a demo reset must leave no receipts behind. It references
    # only `users`, so appending it at the end keeps the child-before-parent
    # order above intact.
    McpAudit,
    # M90 (docs/43 §10): the consent rows and their receipts are owner data —
    # a deleted account and the demo reset take them with it. Both reference
    # only `users` (partner/invite FKs are on `sharing_links.partner_user_id`
    # — CASCADE — and `sharing_audit` deliberately holds no FK at all), so
    # the end of the list stays FK-safe.
    SharingLink,
    SharingAudit,
    # M96 (docs/09 §Recurring prompt): a declined candidate is tenant data —
    # deleting the account (and the demo reset) must take the "never ask me"
    # rows with it, or a future tenant reusing that identity inherits them.
    # References only `users`, so the end of the list stays FK-safe.
    RecurringSuppression,
    # M106 (docs/02 §transaction_events): the per-transaction receipt log is
    # tenant data — a deleted account and the demo reset take the receipts
    # with it. `tx_id`/`actor` deliberately hold NO FK (the mcp_audit rule),
    # so this references only `users` and the end of the list stays FK-safe.
    TransactionEvent,
]


def delete_user_rows(db: Session, user: User, *, keep: tuple[type, ...] = ()) -> None:
    """Delete every row `user` owns, in FK-safe child→parent order (docs/20).

    The `users` row itself is never touched here — `_delete_user_data` adds it,
    and the demo reset (M50) deliberately keeps the tenant identity so live
    sessions still resolve. `keep` names models whose rows survive the wipe;
    the demo reset passes the credential tables, because wiping demo *data*
    must not sign other visitors out or revoke operator-issued MCP access.
    Credentials are not content, which is also why `seed_fingerprint` skips the
    same two models.
    """
    uid = user.id
    # docs/20 cascade steps 6-7: junction tables have no user_id — they are
    # deleted via subqueries on the user's rules / transactions before those
    # parents (and before the tags themselves) in the same transaction.
    db.exec(delete(RuleTag).where(RuleTag.rule_id.in_(select(Rule.id).where(Rule.user_id == uid))))
    db.exec(
        delete(TransactionTag).where(
            TransactionTag.transaction_id.in_(
                select(Transaction.id).where(Transaction.user_id == uid)
            )
        )
    )
    for model in _USER_OWNED_MODELS:
        if model in keep:
            continue
        db.exec(delete(model).where(model.user_id == uid))


def _delete_user_data(db: Session, user: User) -> None:
    delete_user_rows(db, user)
    db.delete(user)


def _assert_not_demo(user: User) -> None:
    # The shared demo tenant (demo@fintracker.local) is restored to a fresh
    # seed by POST /auth/demo/reset (M50) — POST /auth/demo only seeds it and
    # never resets existing data — so self-service account changes must not
    # mutate or destroy it.
    if user.email == DEMO_EMAIL:
        raise api_error(
            403,
            "Account management is disabled for the demo account",
            "demo_account_restricted",
        )


@router.post("/me/change-password", response_model=TokenResponse)
@human_paced_limit(CREDENTIAL_MUTATE_LIMIT)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    _assert_not_demo(current_user)
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise api_error(401, "Current password is incorrect", "invalid_credentials")
    # bcrypt silently truncates beyond 72 bytes; reject up front (mirrors register).
    if len(payload.new_password.encode()) > 72:
        raise api_error(422, "Password exceeds 72 bytes", "password_too_long")
    current_user.hashed_password = hash_password(payload.new_password)
    # Revoke every other session so a password change logs out all other devices.
    db.exec(delete(RefreshToken).where(RefreshToken.user_id == current_user.id))
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    # Re-issue a fresh pair so the calling tab stays logged in.
    pair = _issue_pair(db, current_user)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair


@router.patch("/me/profile", response_model=UserPublic)
@human_paced_limit(CREDENTIAL_MUTATE_LIMIT)
def update_profile(
    payload: ProfileUpdate,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    _assert_not_demo(current_user)
    if payload.email is not None and payload.email != current_user.email:
        if payload.current_password is None:
            raise api_error(
                401,
                "Current password required to change email",
                "current_password_required",
            )
        if not verify_password(payload.current_password, current_user.hashed_password):
            raise api_error(401, "Current password is incorrect", "invalid_credentials")
        if db.exec(select(User).where(User.email == payload.email)).first() is not None:
            raise api_error(409, "Email already registered", "duplicate_email")
        current_user.email = payload.email
    if payload.name is not None:
        current_user.name = payload.name
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.delete("/me", status_code=204)
@human_paced_limit(CREDENTIAL_MUTATE_LIMIT)
def delete_account(
    payload: DeleteAccountRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    _assert_not_demo(current_user)
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise api_error(401, "Current password is incorrect", "invalid_credentials")
    _delete_user_data(db, current_user)
    db.commit()
