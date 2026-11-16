"""Notification delivery: transports, outbox, dispatch, worker (docs/37, M59).

Everything here is best-effort and auditable: a send either lands a `sent`
delivery row plus one attempt-log row, or it backs off and retries, or it
dead-letters. The in-app inbox is never touched by delivery — a transport
failure changes nothing in `spending_alerts`, `bill_reminders`, the union, or
read state.

Privacy (docs/37 decisions 1-2): the payload is five fields, snapshotted at
enqueue; `title`, `body` and `amount_cad` never leave the machine; a
`privacy_mode` user gets in-app only unless the channel row carries an
explicit `privacy_opt_in_at`.

Secrets: SMTP credentials and the webhook HMAC master key are read from env
settings at send time and never written to the database.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession  # has .exec — the worker must use this one
from sqlmodel import select

from app.core.config import Settings, get_settings
from app.core.errors import APIError, api_error

# Shared address policy with the LLM egress seam: same allow/deny rule, this
# card's own error codes (docs/37 "Webhook target policy").
from app.llm.egress import _ALLOWED_SCHEMES, _denied, _resolve, safe_target
from app.models.notifications import (
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationSetting,
)
from app.models.users import User
from app.schemas.notifications import (
    CHANNEL_NAMES,
    GENERATOR_ALERT_TYPES,
    LEGACY_ALERT_TYPES,
)
from app.services.session_release import release_before_provider_wait

logger = logging.getLogger(__name__)

# Single source for retry semantics (docs/37) — constants, not env, so the
# spec, the code and the tests cannot drift apart.
_MAX_ATTEMPTS = 5
_BACKOFF_SECONDS = (30, 60, 120, 240)  # after attempts 1..4; attempt 5 -> dead
_DISPATCH_BATCH = 50
_HTTP_TIMEOUT_SECONDS = 10.0
_EMAIL_SUBJECT = "fin_tracker: you have a new notification"  # constant by design
_PAYLOAD_EVENT = "notification"
_MAX_DETAIL_CHARS = 500

STAT_KEYS = (
    "scanned_users",
    "enqueued",
    "retracted",
    "attempted",
    "sent",
    "retrying",
    "dead",
    "cancelled",
)


# --- payload (docs/37 "What is sent") ---------------------------------------


def build_payload(
    *,
    delivery_id: int,
    alert_type: str,
    severity: str,
    occurred_at: datetime,
    item_key: str,
    link: str,
) -> dict:
    """Exactly the five data fields docs/37 pins — never title/body/amount."""
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return {
        "v": 1,
        "event": _PAYLOAD_EVENT,
        "delivery_id": delivery_id,
        "alert_type": alert_type,
        "severity": severity,
        "occurred_at": occurred_at.astimezone(UTC).isoformat(),
        "item_key": item_key,
        "link": link,
    }


def email_body(payload: dict, settings: Settings) -> str:
    base = (settings.notify_app_base_url or "").rstrip("/")
    return (
        "You have a new notification in fin_tracker.\n\n"
        f"Type: {payload['alert_type']}\n"
        f"Severity: {payload['severity']}\n"
        f"Occurred: {payload['occurred_at']}\n\n"
        f"Open fin_tracker: {base}{payload['link']}\n"
    )


def signing_key(user_id: int, settings: Settings) -> bytes:
    """Per-user HMAC key, derived from env and never stored (docs/37)."""
    master = (settings.notify_webhook_secret or settings.jwt_secret).encode()
    return hmac.new(master, str(user_id).encode(), hashlib.sha256).digest()


def webhook_headers(user_id: int, delivery_id: int, body_text: str, settings: Settings) -> dict:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    message = f"{timestamp}.{body_text}".encode()
    signature = hmac.new(signing_key(user_id, settings), message, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-FinTrack-Event": _PAYLOAD_EVENT,
        "X-FinTrack-Delivery": str(delivery_id),
        "X-FinTrack-Timestamp": timestamp,
        "X-FinTrack-Signature": f"sha256={signature}",
    }


def verify_signature(
    user_id: int, timestamp: str, body: bytes, signature: str, *, settings=None
) -> bool:
    """Reference verification (used by tests and by anyone integrating)."""
    settings = settings or get_settings()
    expected = hmac.new(
        signing_key(user_id, settings), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


# --- configuration + eligibility (docs/37) -----------------------------------


def email_configured(settings: Settings, target: str | None) -> bool:
    return bool(settings.notify_smtp_host and settings.notify_smtp_from and target)


def send_invite_email(target: str, accept_url: str, settings: Settings) -> bool:
    """Best-effort M90 invite mail (docs/43 §9.3; docs/11 egress destination 4).

    Never raises — SMTP trouble downgrades to `email_sent: false`, and the
    201 body's one-time `accept_url` stays the primary delivery channel. The
    privacy-mode gate lives in the CALLER (`sharing.create_invite`), so the
    mailer spy can assert this function is never even entered for a private
    owner. Lives beside the notification SMTP so invites reuse THE smtplib
    call site instead of adding network code to a router (AGENTS §2 seam
    discipline).
    """
    if not email_configured(settings, target):
        return False
    msg = EmailMessage()
    msg["Subject"] = "You're invited to a shared household on fin_tracker"
    msg["From"] = settings.notify_smtp_from
    msg["To"] = target
    msg.set_content(
        "You have been invited to view a shared household on fin_tracker.\n\n"
        f"Open the invitation: {accept_url}\n\n"
        "This link works once and expires in 7 days.\n"
    )
    try:
        with smtplib.SMTP(settings.notify_smtp_host, settings.notify_smtp_port, timeout=10) as smtp:
            if settings.notify_smtp_starttls:
                smtp.starttls()
            if settings.notify_smtp_user:
                smtp.login(settings.notify_smtp_user, settings.notify_smtp_password)
            smtp.send_message(msg)
    except Exception:  # mail is never on the critical path
        logger.warning("invite mail to %s failed", target, exc_info=True)
        return False
    return True


def channel_configured(channel: str, target: str | None, settings: Settings) -> bool:
    """Would enabling this channel be accepted right now (`configured` field)?"""
    if channel == "email":
        return email_configured(settings, target)
    return bool(target)


def privacy_ok(channel: NotificationChannel, user: User) -> bool:
    """Decision 2: privacy mode needs the explicit, per-channel opt-in."""
    return (not user.privacy_mode) or channel.privacy_opt_in_at is not None


def type_flags(db: Session, user_id: int) -> dict[str, tuple[bool, bool]]:
    """alert_type -> (enabled, delivery_enabled). Missing row => (True, False):
    the inbox gate defaults ON (docs/35), delivery defaults OFF (opt-in)."""
    rows = db.exec(
        select(
            NotificationSetting.alert_type,
            NotificationSetting.enabled,
            NotificationSetting.delivery_enabled,
        ).where(NotificationSetting.user_id == user_id)
    ).all()
    out: dict[str, tuple[bool, bool]] = {
        atype: (True, False) for atype in (*LEGACY_ALERT_TYPES, *GENERATOR_ALERT_TYPES)
    }
    out.update(
        {alert_type: (bool(enabled), bool(delivery)) for alert_type, enabled, delivery in rows}
    )
    return out


def validate_webhook_target(
    url: str, *, settings: Settings | None = None, at_send: bool = False
) -> None:
    """Write-time AND send-time target policy (docs/37). Raises APIError.

    `at_send` mirrors `app/llm/egress.py`: an UNVERIFIABLE host is storable at
    write time (a webhook URL saved while its DNS was briefly down should not be
    rejected), but it FAILS CLOSED at the send point, where there is no later
    chance to check. The previous form of this function was

        addrs = _resolve(hostname)
        if addrs and any(_denied(addr) for addr in addrs):

    which fails OPEN in exactly the dangerous case: `addrs` falsy means "did
    not resolve", and the send proceeded to `httpx.post`, which performs its
    OWN second DNS lookup that this policy never sees. A hostname that
    answered publicly at validation time and privately at send time therefore
    reached an internal address.
    """
    settings = settings or get_settings()
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = parts.hostname
    except ValueError:
        raise api_error(
            422, "Webhook target must be an http(s) URL", "invalid_webhook_url"
        ) from None
    if scheme not in _ALLOWED_SCHEMES or not host:
        raise api_error(422, "Webhook target must be an http(s) URL", "invalid_webhook_url")
    if settings.notify_webhook_allow_private:
        return
    hostname = host.lower().rstrip(".")
    addrs = _resolve(hostname)  # None = unresolvable now
    if not addrs:
        if at_send:
            # Its OWN code, not `webhook_target_forbidden`: the two are not the
            # same failure and must not be retried the same way. A denied
            # address is a permanent policy refusal; an unresolvable hostname is
            # a transient DNS condition, and before this check it fell through
            # to `httpx.post` and surfaced as `webhook_unreachable` /
            # `retryable`. That behaviour is preserved deliberately — DNS blips
            # must not permanently abandon a notification.
            raise api_error(
                403,
                "Webhook target could not be verified at send time; refusing to "
                "deliver to an unresolvable host",
                "webhook_target_unverifiable",
            )
        return
    if any(_denied(addr) for addr in addrs):
        raise api_error(
            403,
            "Webhook target resolves to a private/reserved address; "
            "set NOTIFY_WEBHOOK_ALLOW_PRIVATE=true to allow it",
            "webhook_target_forbidden",
        )


# --- transports (one interface, docs/37 "Approach") --------------------------


class SendResult:
    """Outcome of one send attempt. `outcome` ∈ sent | retryable | fatal."""

    __slots__ = ("detail", "error_code", "http_status", "outcome")

    def __init__(
        self,
        outcome: str,
        http_status: int | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.http_status = http_status
        self.error_code = error_code
        self.detail = detail


def _short(text: str) -> str:
    return text[:_MAX_DETAIL_CHARS]


def send_email(payload: dict, target: str | None, settings: Settings) -> SendResult:
    if not email_configured(settings, target):
        return SendResult(
            "fatal", None, "smtp_not_configured", "SMTP is not configured on this install"
        )
    msg = EmailMessage()
    msg["Subject"] = _EMAIL_SUBJECT  # constant for every type (docs/37)
    msg["From"] = settings.notify_smtp_from
    msg["To"] = target or ""
    msg.set_content(email_body(payload, settings))
    try:
        with smtplib.SMTP(
            settings.notify_smtp_host, settings.notify_smtp_port, timeout=_HTTP_TIMEOUT_SECONDS
        ) as smtp:
            if settings.notify_smtp_starttls:
                smtp.starttls()
            if settings.notify_smtp_user:
                smtp.login(settings.notify_smtp_user, settings.notify_smtp_password)
            smtp.send_message(msg)
    except smtplib.SMTPRecipientsRefused as exc:
        return SendResult("fatal", None, "smtp_recipients_refused", _short(str(exc)))
    except smtplib.SMTPSenderRefused as exc:
        return SendResult("fatal", exc.smtp_code, "smtp_error", _short(str(exc)))
    except smtplib.SMTPResponseException as exc:
        outcome = "fatal" if exc.smtp_code >= 500 else "retryable"
        return SendResult(outcome, exc.smtp_code, "smtp_error", _short(str(exc)))
    except smtplib.SMTPNotSupportedError as exc:
        return SendResult("fatal", None, "smtp_error", _short(str(exc)))
    except smtplib.SMTPException as exc:
        return SendResult("retryable", None, "smtp_unavailable", _short(str(exc)))
    except (OSError, TimeoutError) as exc:
        return SendResult(
            "retryable", None, "smtp_unavailable", _short(f"{type(exc).__name__}: {exc}")
        )
    return SendResult("sent")


def send_webhook(
    payload: dict, target: str | None, user_id: int, delivery_id: int, settings: Settings
) -> SendResult:
    if not target:
        return SendResult("fatal", None, "channel_not_configured", "webhook target is empty")
    try:
        validate_webhook_target(target, settings=settings, at_send=True)
    except APIError as exc:
        # An unverifiable host is TRANSIENT (DNS), not a policy refusal — keep
        # the retryable outcome the pre-check behaviour produced, so a brief DNS
        # outage does not permanently abandon the delivery. A denied address
        # stays fatal.
        outcome = "retryable" if exc.code == "webhook_target_unverifiable" else "fatal"
        return SendResult(outcome, None, exc.code, _short(exc.detail))
    body_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    headers = webhook_headers(user_id, delivery_id, body_text, settings)
    try:
        response = httpx.post(
            target, content=body_text.encode(), headers=headers, timeout=_HTTP_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        # `safe_target` strips userinfo/query before anything reaches a log or
        # the attempt row (docs/37 — the payload itself is never logged).
        return SendResult(
            "retryable",
            None,
            "webhook_unreachable",
            _short(f"{type(exc).__name__} at {safe_target(target)}: {exc}"),
        )
    status = response.status_code
    if 200 <= status < 300:
        return SendResult("sent", status)
    if status in (408, 429) or status >= 500:
        return SendResult("retryable", status, "webhook_http_error", _short(response.text))
    return SendResult("fatal", status, "webhook_http_error", _short(response.text))


def send(
    payload: dict,
    channel: str,
    target: str | None,
    user_id: int,
    delivery_id: int,
    settings: Settings,
) -> SendResult:
    """The one dispatch seam: same arguments, same outcome contract."""
    if channel == "email":
        return send_email(payload, target, settings)
    if channel == "webhook":
        return send_webhook(payload, target, user_id, delivery_id, settings)
    return SendResult("fatal", None, "channel_not_configured", f"unknown channel {channel!r}")


# --- outbox ------------------------------------------------------------------


def ensure_channels(db: Session, user: User) -> list[NotificationChannel]:
    """Idempotent seed of the two rows for this user, both disabled (docs/37)."""
    existing = {
        row.channel: row
        for row in db.exec(
            select(NotificationChannel).where(NotificationChannel.user_id == user.id)
        ).all()
    }
    for name in CHANNEL_NAMES:
        if name in existing:
            continue
        db.add(
            NotificationChannel(
                user_id=user.id,
                channel=name,
                enabled=False,
                target=(user.email if name == "email" else None),
            )
        )
    db.commit()
    return list(
        db.exec(
            select(NotificationChannel)
            .where(NotificationChannel.user_id == user.id)
            .order_by(NotificationChannel.id)
        ).all()
    )


def cancel_pending(
    db: Session, user_id: int, *, channel: str | None = None, alert_type: str | None = None
) -> int:
    """Explicit user action: drop queued rows (terminal, docs/37). No attempt
    row is written — nothing was sent."""
    q = select(NotificationDelivery).where(
        NotificationDelivery.user_id == user_id,
        NotificationDelivery.status == "pending",
    )
    if channel is not None:
        q = q.where(NotificationDelivery.channel == channel)
    if alert_type is not None:
        q = q.where(NotificationDelivery.alert_type == alert_type)
    rows = list(db.exec(q).all())
    now = datetime.now(UTC)
    for row in rows:
        row.status = "cancelled"
        row.last_error_code = "delivery_not_eligible"
        row.last_error = "cancelled by a settings change"
        row.next_attempt_at = now
        db.add(row)
    if rows:
        db.commit()
    return len(rows)


def _retract_vanished(db: Session, user_id: int, channel: str, keys: set[str]) -> int:
    """Retract active rows whose inbox item left the union; a still-pending row
    is cancelled rather than sent stale (docs/37 "Deduplication and re-arm")."""
    rows = list(
        db.exec(
            select(NotificationDelivery).where(
                NotificationDelivery.user_id == user_id,
                NotificationDelivery.channel == channel,
                NotificationDelivery.retracted_at.is_(None),
            )
        ).all()
    )
    now = datetime.now(UTC)
    changed = 0
    for row in rows:
        if row.item_key in keys:
            continue
        row.retracted_at = now
        if row.status == "pending":
            row.status = "cancelled"
            row.last_error_code = "delivery_not_eligible"
            row.last_error = "inbox item left the union before dispatch"
        db.add(row)
        changed += 1
    if changed:
        db.commit()
    return changed


def _enqueue(db: Session, user_id: int, channel: NotificationChannel, items: list) -> int:
    existing = set(
        db.exec(
            select(NotificationDelivery.item_key).where(
                NotificationDelivery.user_id == user_id,
                NotificationDelivery.channel == channel.channel,
                NotificationDelivery.retracted_at.is_(None),
            )
        ).all()
    )
    now = datetime.now(UTC)
    added = 0
    for item in items:
        if item.id in existing:
            continue
        db.add(
            NotificationDelivery(
                user_id=user_id,
                channel=channel.channel,
                alert_type=item.kind,
                item_key=item.id,
                severity=item.severity,
                occurred_at=item.occurred_at,
                link=item.link,
                status="pending",
                attempts=0,
                next_attempt_at=now,
            )
        )
        existing.add(item.id)
        added += 1
    if added:
        db.commit()
    return added


def _ineligible_reason(
    db: Session,
    row: NotificationDelivery,
    user: User | None,
    channel: NotificationChannel | None,
    settings: Settings,
) -> str | None:
    """Second eligibility check, at the send point (docs/37 predicate)."""
    if user is None:
        return "user no longer exists"
    if channel is None or not channel.enabled:
        return "channel is disabled"
    if not privacy_ok(channel, user):
        return "privacy mode without an explicit opt-in"
    enabled, delivery = type_flags(db, row.user_id).get(row.alert_type, (True, False))
    if not enabled:
        return "inbox toggle is off for this type"
    if not delivery:
        return "delivery toggle is off for this type"
    if not channel_configured(channel.channel, channel.target, settings):
        return "transport is not configured"
    return None


def _dispatch(db: Session, *, user_id: int | None = None) -> dict:
    now = datetime.now(UTC)
    q = (
        select(NotificationDelivery)
        .where(
            NotificationDelivery.status == "pending",
            NotificationDelivery.retracted_at.is_(None),
            NotificationDelivery.next_attempt_at <= now,
        )
        .order_by(NotificationDelivery.next_attempt_at)
        .limit(_DISPATCH_BATCH)
    )
    if user_id is not None:
        q = q.where(NotificationDelivery.user_id == user_id)
    rows = list(db.exec(q).all())
    out = {"attempted": 0, "sent": 0, "retrying": 0, "dead": 0, "cancelled": 0}
    if not rows:
        return out
    settings = get_settings()
    channels: dict[tuple[int, str], NotificationChannel | None] = {}
    users: dict[int, User | None] = {}
    for row in rows:
        if (row.user_id, row.channel) not in channels:
            channels[(row.user_id, row.channel)] = db.exec(
                select(NotificationChannel).where(
                    NotificationChannel.user_id == row.user_id,
                    NotificationChannel.channel == row.channel,
                )
            ).first()
        if row.user_id not in users:
            users[row.user_id] = db.get(User, row.user_id)
        reason = _ineligible_reason(
            db, row, users[row.user_id], channels[(row.user_id, row.channel)], settings
        )
        if reason is not None:
            row.status = "cancelled"
            row.last_error_code = "delivery_not_eligible"
            row.last_error = reason
            db.add(row)
            out["cancelled"] += 1
            continue

        out["attempted"] += 1
        payload = build_payload(
            delivery_id=row.id or 0,
            alert_type=row.alert_type,
            severity=row.severity,
            occurred_at=row.occurred_at,
            item_key=row.item_key,
            link=row.link,
        )
        channel_row = channels[(row.user_id, row.channel)]
        # M175 (docs/07 §Provider-wait ledger): a send is a network wait — SMTP
        # or the webhook, both bounded at 10 s — and this loop runs a whole
        # batch of them (`_DISPATCH_BATCH` rows, worker AND `POST
        # /notifications/deliveries/run`). Everything the loop has decided so
        # far (the retracted/cancelled rows marked above, the previous rows'
        # attempt bookkeeping) is committed here, so the connection is `idle`
        # rather than `idle in transaction` while the provider answers, and the
        # last `db.commit()` below still closes the batch.
        release_before_provider_wait(db)
        result = send(
            payload,
            row.channel,
            channel_row.target if channel_row else None,
            row.user_id,
            row.id or 0,
            settings,
        )
        attempt_no = (row.attempts or 0) + 1
        row.attempts = attempt_no
        detail = result.detail if result.outcome != "sent" else None
        if detail:
            detail = detail.replace("\n", " ")[:_MAX_DETAIL_CHARS]
        db.add(
            NotificationDeliveryAttempt(
                user_id=row.user_id,
                delivery_id=row.id,
                attempt=attempt_no,
                outcome=result.outcome,
                error_code=result.error_code,
                http_status=result.http_status,
                detail=detail,
            )
        )
        row.last_error = detail
        row.last_error_code = result.error_code if result.outcome != "sent" else None
        if result.outcome == "sent":
            row.status = "sent"
            row.sent_at = now
            row.last_error = None
            row.last_error_code = None
            out["sent"] += 1
        elif result.outcome == "fatal":
            row.status = "dead"
            row.dead_at = now
            out["dead"] += 1
        elif attempt_no >= _MAX_ATTEMPTS:
            row.status = "dead"
            row.dead_at = now
            out["dead"] += 1
        else:
            row.next_attempt_at = now + timedelta(seconds=_BACKOFF_SECONDS[attempt_no - 1])
            out["retrying"] += 1
        db.add(row)
    db.commit()
    return out


def run_once(db: Session, *, user_id: int | None = None) -> dict:
    """Scan + dispatch — the single entry point (worker, POST .../run, tests).

    Local import: `app.api.notifications` owns the inbox union and imports
    this module for its channel side effects.
    """
    from app.api.notifications import _collect

    stats = dict.fromkeys(STAT_KEYS, 0)
    q = select(NotificationChannel).where(NotificationChannel.enabled.is_(True))
    if user_id is not None:
        q = q.where(NotificationChannel.user_id == user_id)
    by_user: dict[int, list[NotificationChannel]] = {}
    for channel in db.exec(q).all():
        by_user.setdefault(channel.user_id, []).append(channel)

    for uid, channels in by_user.items():
        user = db.get(User, uid)
        if user is None:
            continue
        active = [c for c in channels if privacy_ok(c, user)]
        if not active:
            continue  # privacy mode without opt-in: in-app only, no union work
        stats["scanned_users"] += 1
        items = _collect(db, uid)  # same gated union the inbox serves (docs/35)
        keys = {item.id for item in items}
        flags = type_flags(db, uid)
        eligible = [i for i in items if flags.get(i.kind, (True, False)) == (True, True)]
        for channel in active:
            stats["retracted"] += _retract_vanished(db, uid, channel.channel, keys)
            stats["enqueued"] += _enqueue(db, uid, channel, eligible)

    dispatched = _dispatch(db, user_id=user_id)
    stats.update(dispatched)
    return stats


# --- worker (docs/37 "The worker") -------------------------------------------


def _open_session() -> Session:
    """The worker's own DB session.

    MUST be SQLModel's `Session`: every query in this module uses `.exec()`,
    which plain `sqlalchemy.orm.Session` does not have. The test suite never
    constructs this object (requests inject a SQLModel session via `get_db`),
    so the wrong class only surfaces as a failed worker tick in a live server —
    which is exactly how it was caught (docs/37 verification, running stack).
    """
    from app.api.deps import get_engine

    return SQLModelSession(get_engine())


def _run_once_thread() -> None:
    with _open_session() as db:
        run_once(db)


async def worker_loop() -> None:
    """Ticks until cancelled. Errors are logged, never fatal (docs/37)."""
    while True:
        settings = get_settings()
        try:
            # to_thread: an SMTP/httpx call must not block the event loop.
            await asyncio.to_thread(_run_once_thread)
        except Exception:  # the loop must outlive any tick
            logger.exception("notification delivery tick failed")
        await asyncio.sleep(max(int(settings.notify_worker_interval_seconds), 1))


def start_worker() -> asyncio.Task | None:
    """Started from `lifespan` only when NOTIFY_WORKER_ENABLED (default true);
    tests force it off so every step is deterministic."""
    if not get_settings().notify_worker_enabled:
        return None
    return asyncio.create_task(worker_loop())


async def stop_worker(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "SendResult",
    "build_payload",
    "cancel_pending",
    "channel_configured",
    "email_configured",
    "ensure_channels",
    "privacy_ok",
    "run_once",
    "send",
    "send_email",
    "send_webhook",
    "signing_key",
    "start_worker",
    "stop_worker",
    "type_flags",
    "validate_webhook_target",
    "verify_signature",
    "webhook_headers",
    "worker_loop",
]
