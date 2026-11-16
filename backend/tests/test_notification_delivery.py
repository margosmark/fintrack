"""M59 — notification delivery channels, outbox, transports (docs/37).

Everything runs against real sockets (a local webhook sink and a local SMTP
sink), because the acceptance criteria are about bytes on the wire: signature,
timestamp, exact payload keys, and what must NEVER appear in a message.
"""

from __future__ import annotations

import json
import socketserver
import threading
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.config import get_settings
from app.core.security import create_access_token, hash_password
from app.models.notifications import (
    NotificationDelivery,
    NotificationDeliveryAttempt,
)
from app.models.spending_alerts import SpendingAlert
from app.models.users import User
from app.services.demo_seed import DEMO_EMAIL
from app.services.notify_delivery import run_once, verify_signature
from app.services.txn_filters import today_et
from tests.test_deep_common import auth, create_account, get_category, register, seed_transaction


def _today() -> date:
    """The clock, read at CALL time — never captured at import (t_435c2d14)."""
    return today_et()


_EMAIL_SUBJECT = "fin_tracker: you have a new notification"
_PAYLOAD_KEYS = {
    "v",
    "event",
    "delivery_id",
    "alert_type",
    "severity",
    "occurred_at",
    "item_key",
    "link",
}
_STAT_KEYS = {
    "scanned_users",
    "enqueued",
    "retracted",
    "attempted",
    "sent",
    "retrying",
    "dead",
    "cancelled",
}


# --- sinks -------------------------------------------------------------------


class _WebhookSink:
    """Tiny local HTTP server that records every request it receives."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict] = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                sink.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": raw}
                )
                payload = json.dumps({"ok": sink.status < 400}).encode()
                self.send_response(sink.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:  # silence the access log
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    @property
    def count(self) -> int:
        return len(self.requests)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _SMTPHandler(socketserver.StreamRequestHandler):
    """Minimal SMTP: enough for smtplib to hand over one message."""

    def handle(self) -> None:
        self.wfile.write(b"220 sink ESMTP\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            upper = cmd.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                self.wfile.write(b"250-sink\r\n250 HELP\r\n")
            elif upper.startswith("MAIL FROM") or upper.startswith("RCPT TO"):
                self.wfile.write(b"250 2.1.0 OK\r\n")
            elif upper.startswith("DATA"):
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                data = b""
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b".\n", b""):
                        break
                    data += chunk
                self.server.messages.append(data)  # type: ignore[attr-defined]
                self.wfile.write(b"250 2.0.0 OK: queued\r\n")
            elif upper.startswith("QUIT"):
                self.wfile.write(b"221 2.0.0 Bye\r\n")
                return
            else:
                self.wfile.write(b"250 2.0.0 OK\r\n")


class _SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _smtp_sink() -> tuple[_SMTPServer, list, int]:
    server = _SMTPServer(("127.0.0.1", 0), _SMTPHandler)
    messages: list = []
    server.messages = messages  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, messages, server.server_address[1]


# --- helpers -----------------------------------------------------------------


def _new_user(client: TestClient) -> tuple[dict[str, str], int]:
    body = register(client)
    return auth(body["access_token"]), body["user"]["id"]


def _channels(client: TestClient, headers: dict) -> dict[str, dict]:
    r = client.get("/api/v1/notifications/channels", headers=headers)
    assert r.status_code == 200, r.text
    return {row["channel"]: row for row in r.json()}


def _enable_webhook(client: TestClient, headers: dict, url: str) -> dict:
    row = _channels(client, headers)["webhook"]
    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}",
        headers=headers,
        json={"enabled": True, "target": url},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _setting_id(client: TestClient, headers: dict, alert_type: str) -> int:
    rows = client.get("/api/v1/notifications", headers=headers).json()
    return next(s["id"] for s in rows if s["alert_type"] == alert_type)


def _patch_setting(client: TestClient, headers: dict, alert_type: str, **fields) -> dict:
    r = client.patch(
        f"/api/v1/notifications/{_setting_id(client, headers, alert_type)}",
        headers=headers,
        json=fields,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _seed_bill(
    client: TestClient,
    headers: dict,
    name: str = "Netflix",
    amount: str = "15.99",
    days_ahead: int = 2,
) -> int:
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": name,
            "amount": amount,
            "frequency": "monthly",
            "next_due_date": (_today() + timedelta(days=days_ahead)).isoformat(),
            "remind_days_before": 7,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_budget(client: TestClient, session: Session, headers: dict, user_id: int) -> None:
    """groceries budget(200) + a -337 CAD txn today -> a `critical` alert row."""
    account_id = create_account(client, headers)
    category_id = get_category(session, user_id, "groceries")
    r = client.post(
        "/api/v1/budgets",
        headers=headers,
        json={"category_id": category_id, "amount": 200.0, "period": "monthly"},
    )
    assert r.status_code == 201, r.text
    seed_transaction(
        session,
        user_id,
        account_id,
        "Superstore",
        amount="-337.00",
        day=_today(),
        category_id=category_id,
    )


def _deliveries(session: Session, user_id: int) -> list[NotificationDelivery]:
    session.expire_all()
    return list(
        session.exec(
            select(NotificationDelivery)
            .where(NotificationDelivery.user_id == user_id)
            .order_by(NotificationDelivery.id)
        ).all()
    )


def _attempts(session: Session, delivery_id: int) -> list[NotificationDeliveryAttempt]:
    session.expire_all()
    return list(
        session.exec(
            select(NotificationDeliveryAttempt)
            .where(NotificationDeliveryAttempt.delivery_id == delivery_id)
            .order_by(NotificationDeliveryAttempt.attempt)
        ).all()
    )


def _age_delivery(session: Session, delivery_id: int) -> None:
    """Force `next_attempt_at` into the past so the next tick retries now."""
    session.expire_all()
    row = session.get(NotificationDelivery, delivery_id)
    assert row is not None
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(row)
    session.commit()


def _make_user(session: Session, email: str) -> User:
    user = User(email=email, hashed_password=hash_password("correct-horse-battery"))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# --- settings / channels -----------------------------------------------------


def test_settings_seed_delivery_enabled_as_copy_of_enabled(client: TestClient) -> None:
    """docs/37 decision 3: generator three deliverable, legacy five inert."""
    headers, _ = _new_user(client)
    rows = client.get("/api/v1/notifications", headers=headers).json()
    assert len(rows) == 8
    by_type = {s["alert_type"]: s for s in rows}
    for t in ("budget_threshold", "bill_due", "tax_loss"):
        assert by_type[t]["enabled"] is True and by_type[t]["delivery_enabled"] is True
    for t in (
        "large_transaction",
        "low_balance",
        "dividend_received",
        "price_alert",
        "monthly_summary",
    ):
        assert by_type[t]["enabled"] is False and by_type[t]["delivery_enabled"] is False

    patched = _patch_setting(client, headers, "bill_due", delivery_enabled=False)
    assert patched["delivery_enabled"] is False and patched["enabled"] is True
    again = client.get("/api/v1/notifications", headers=headers).json()
    assert next(s for s in again if s["alert_type"] == "bill_due")["delivery_enabled"] is False


def test_channels_seed_disabled_idempotent_and_configured_flag(
    client: TestClient, monkeypatch
) -> None:
    headers, _ = _new_user(client)
    first = _channels(client, headers)
    assert set(first) == {"email", "webhook"}
    assert all(row["enabled"] is False for row in first.values())
    assert all(row["privacy_opt_in"] is False for row in first.values())
    assert first["email"]["target"] is not None
    assert first["webhook"]["target"] is None
    assert first["webhook"]["configured"] is False
    assert first["email"]["configured"] is False  # no SMTP configured in tests

    second = _channels(client, headers)
    assert {k: v["id"] for k, v in second.items()} == {k: v["id"] for k, v in first.items()}

    monkeypatch.setattr(get_settings(), "notify_smtp_host", "smtp.example.com")
    monkeypatch.setattr(get_settings(), "notify_smtp_from", "notify@example.com")
    assert _channels(client, headers)["email"]["configured"] is True


def test_enable_email_without_smtp_is_channel_not_configured(
    client: TestClient, monkeypatch
) -> None:
    headers, _ = _new_user(client)
    email_row = _channels(client, headers)["email"]
    r = client.patch(
        f"/api/v1/notifications/channels/{email_row['id']}",
        headers=headers,
        json={"enabled": True},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "channel_not_configured"

    monkeypatch.setattr(get_settings(), "notify_smtp_host", "smtp.example.com")
    monkeypatch.setattr(get_settings(), "notify_smtp_from", "notify@example.com")
    r = client.patch(
        f"/api/v1/notifications/channels/{email_row['id']}",
        headers=headers,
        json={"enabled": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True


def test_webhook_target_policy(client: TestClient) -> None:
    headers, _ = _new_user(client)
    row = _channels(client, headers)["webhook"]

    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}", headers=headers, json={"target": "ftp://x"}
    )
    assert r.status_code == 422 and r.json()["code"] == "invalid_webhook_url"

    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}",
        headers=headers,
        json={"target": "http://192.168.1.10/hook"},
    )
    assert r.status_code == 403 and r.json()["code"] == "webhook_target_forbidden"

    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}",
        headers=headers,
        json={"target": "http://127.0.0.1:9/hook"},
    )
    assert r.status_code == 200 and r.json()["target"] == "http://127.0.0.1:9/hook"


def test_channel_validation_failure_leaves_row_untouched(client: TestClient) -> None:
    """A 4xx must not half-apply the patch (project -> validate -> write)."""
    headers, _ = _new_user(client)
    email_row = _channels(client, headers)["email"]
    r = client.patch(
        f"/api/v1/notifications/channels/{email_row['id']}",
        headers=headers,
        json={"target": "not-an-email", "enabled": True},
    )
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    after = _channels(client, headers)["email"]
    assert after["enabled"] is False and after["target"] == email_row["target"]


def test_foreign_channel_id_is_404(client: TestClient) -> None:
    headers_a, _ = _new_user(client)
    headers_b, _ = _new_user(client)
    foreign = _channels(client, headers_a)["webhook"]["id"]
    r = client.patch(
        f"/api/v1/notifications/channels/{foreign}",
        headers=headers_b,
        json={"enabled": True, "target": "http://127.0.0.1:9/hook"},
    )
    assert r.status_code == 404 and r.json()["code"] == "not_found"


# --- privacy mode (docs/37 decision 2) ---------------------------------------


def test_privacy_mode_requires_explicit_opt_in(client: TestClient) -> None:
    headers, _ = _new_user(client)
    row = _channels(client, headers)["webhook"]
    r = client.patch("/api/v1/users/me", headers=headers, json={"privacy_mode": True})
    assert r.status_code == 200, r.text
    assert r.json()["privacy_mode"] is True

    # Pointing the channel somewhere is allowed under privacy mode (their target,
    # their call) — turning it on is what needs the opt-in.
    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}",
        headers=headers,
        json={"target": "http://127.0.0.1:9/hook"},
    )
    assert r.status_code == 200, r.text

    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}",
        headers=headers,
        json={"enabled": True},
    )
    assert r.status_code == 409 and r.json()["code"] == "privacy_opt_in_required"
    assert _channels(client, headers)["webhook"]["enabled"] is False

    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}",
        headers=headers,
        json={"enabled": True, "privacy_opt_in": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True and r.json()["privacy_opt_in"] is True

    # Sticky: enabling again without repeating the opt-in stays allowed.
    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}", headers=headers, json={"enabled": True}
    )
    assert r.status_code == 200

    # Revoking the opt-in takes delivery away again (row flag stays off).
    r = client.patch(
        f"/api/v1/notifications/channels/{row['id']}",
        headers=headers,
        json={"privacy_opt_in": False},
    )
    assert r.status_code == 200 and r.json()["privacy_opt_in"] is False


def test_privacy_default_is_in_app_only(client: TestClient, session: Session) -> None:
    """Acceptance: privacy-mode default = in-app only — no row, no send."""
    sink = _WebhookSink()
    try:
        headers, user_id = _new_user(client)
        r = client.patch("/api/v1/users/me", headers=headers, json={"privacy_mode": True})
        assert r.status_code == 200, r.text
        _seed_bill(client, headers)
        row = _channels(client, headers)["webhook"]
        client.patch(
            f"/api/v1/notifications/channels/{row['id']}",
            headers=headers,
            json={"target": sink.url},
        )

        inbox = client.get("/api/v1/notifications/inbox", headers=headers).json()
        assert any(i["kind"] == "bill_due" for i in inbox["items"]), "in-app must still work"

        stats = run_once(session, user_id=user_id)
        assert stats["enqueued"] == 0 and stats["attempted"] == 0
        assert _deliveries(session, user_id) == []
        assert sink.count == 0
    finally:
        sink.close()


def test_privacy_flip_after_enqueue_cancels_dispatch(client: TestClient, session: Session) -> None:
    """The send-point re-check: privacy turned on later blocks the queue."""
    sink = _WebhookSink()
    try:
        headers, user_id = _new_user(client)
        _enable_webhook(client, headers, sink.url)
        _seed_bill(client, headers)
        stats = run_once(session, user_id=user_id)
        assert stats["sent"] == 1 and sink.count == 1

        # Model "queued while privacy was off": put the row back to pending.
        row = _deliveries(session, user_id)[0]
        row.status = "pending"
        row.sent_at = None
        session.add(row)
        session.commit()

        r = client.patch("/api/v1/users/me", headers=headers, json={"privacy_mode": True})
        assert r.status_code == 200, r.text

        stats = run_once(session, user_id=user_id)
        assert stats["cancelled"] == 1 and stats["attempted"] == 0
        row = _deliveries(session, user_id)[0]
        assert row.status == "cancelled"
        assert row.last_error_code == "delivery_not_eligible"
        assert len(_attempts(session, row.id)) == 1  # the original send only
        assert sink.count == 1  # nothing left the machine
    finally:
        sink.close()


# --- transport + payload -----------------------------------------------------


def test_webhook_payload_is_signed_minimal_and_exact(client: TestClient, session: Session) -> None:
    sink = _WebhookSink()
    try:
        headers, user_id = _new_user(client)
        _enable_webhook(client, headers, sink.url)
        bill_id = _seed_bill(client, headers, name="Netflix", amount="15.99")
        stats = run_once(session, user_id=user_id)
        assert stats["sent"] == 1 and sink.count == 1

        req = sink.requests[0]
        assert req["path"] == "/hook"
        h = req["headers"]
        assert h["Content-Type"] == "application/json"
        assert h["X-FinTrack-Event"] == "notification"
        assert h["X-FinTrack-Timestamp"].isdigit()
        body = req["body"]
        assert verify_signature(user_id, h["X-FinTrack-Timestamp"], body, h["X-FinTrack-Signature"])
        assert not verify_signature(
            user_id + 999, h["X-FinTrack-Timestamp"], body, h["X-FinTrack-Signature"]
        ), "a different user's key must not validate this signature"

        payload = json.loads(body)
        assert set(payload) == _PAYLOAD_KEYS
        assert payload["v"] == 1 and payload["event"] == "notification"
        # M116 (docs/35/37): a due bill drills into ITS OWN detail page.
        assert payload["alert_type"] == "bill_due" and payload["link"] == f"/bills/{bill_id}"
        assert payload["severity"] in {"critical", "warning", "info"}
        assert payload["item_key"].startswith("bill_due:")
        assert h["X-FinTrack-Delivery"] == str(payload["delivery_id"])
        assert payload["occurred_at"].endswith("+00:00")

        # Content hints never leave (docs/37 "What is sent").
        text = body.decode()
        assert "Netflix" not in text and "15.99" not in text
    finally:
        sink.close()


def test_email_transport_sends_minimal_message(
    client: TestClient, session: Session, monkeypatch
) -> None:
    server, messages, port = _smtp_sink()
    try:
        headers, user_id = _new_user(client)
        monkeypatch.setattr(get_settings(), "notify_smtp_host", "127.0.0.1")
        monkeypatch.setattr(get_settings(), "notify_smtp_port", port)
        monkeypatch.setattr(get_settings(), "notify_smtp_from", "notify@example.com")
        monkeypatch.setattr(get_settings(), "notify_smtp_starttls", False)
        monkeypatch.setattr(get_settings(), "notify_app_base_url", "https://fintrac.example")
        email_row = _channels(client, headers)["email"]
        r = client.patch(
            f"/api/v1/notifications/channels/{email_row['id']}",
            headers=headers,
            json={"enabled": True},
        )
        assert r.status_code == 200, r.text
        bill_id = _seed_bill(client, headers, name="Netflix", amount="15.99")

        stats = run_once(session, user_id=user_id)
        assert stats["sent"] == 1, stats
        assert len(messages) == 1, messages
        raw = messages[0].decode("utf-8", "replace")
        assert f"Subject: {_EMAIL_SUBJECT}" in raw, raw
        assert "Type: bill_due" in raw
        assert "Severity: " in raw and "Occurred: " in raw
        assert f"Open fin_tracker: https://fintrac.example/bills/{bill_id}" in raw
        assert "Netflix" not in raw and "15.99" not in raw
        assert "text/html" not in raw  # plain text only
    finally:
        server.shutdown()
        server.server_close()


# --- toggles gate both surfaces ----------------------------------------------


def test_toggle_off_stops_inbox_and_delivery(client: TestClient, session: Session) -> None:
    """Acceptance: toggling a type off stops BOTH inbox and delivery."""
    sink = _WebhookSink()
    try:
        headers, user_id = _new_user(client)
        _enable_webhook(client, headers, sink.url)
        _seed_bill(client, headers)
        assert run_once(session, user_id=user_id)["sent"] == 1
        assert sink.count == 1
        inbox_before = client.get("/api/v1/notifications/inbox", headers=headers).json()
        assert any(i["kind"] == "bill_due" for i in inbox_before["items"])

        _patch_setting(client, headers, "bill_due", enabled=False)
        inbox_after = client.get("/api/v1/notifications/inbox", headers=headers).json()
        assert all(i["kind"] != "bill_due" for i in inbox_after["items"])

        stats = run_once(session, user_id=user_id)
        assert stats["enqueued"] == 0 and stats["sent"] == 0
        assert len(_deliveries(session, user_id)) == 1  # no new outbox rows
        assert sink.count == 1  # nothing new left the machine
    finally:
        sink.close()


def test_toggling_off_cancels_pending_rows(client: TestClient, session: Session) -> None:
    """Queued rows are dropped immediately by the toggle (terminal)."""
    headers, user_id = _new_user(client)
    _enable_webhook(client, headers, "http://127.0.0.1:9/hook")
    now = datetime.now(UTC)
    pending = NotificationDelivery(
        user_id=user_id,
        channel="webhook",
        alert_type="bill_due",
        item_key="bill_due:1",
        severity="warning",
        occurred_at=now,
        link="/bills",
        status="pending",
        attempts=0,
        next_attempt_at=now + timedelta(hours=1),
    )
    session.add(pending)
    session.commit()

    _patch_setting(client, headers, "bill_due", delivery_enabled=False)
    row = _deliveries(session, user_id)[0]
    assert row.status == "cancelled"
    assert row.last_error_code == "delivery_not_eligible"
    assert _attempts(session, row.id) == [], "cancelling is not a send attempt"

    # Turning it back on does NOT resurrect the dropped row (documented).
    _patch_setting(client, headers, "bill_due", delivery_enabled=True)
    assert _deliveries(session, user_id)[0].status == "cancelled"


def test_disabling_channel_cancels_its_queue(client: TestClient, session: Session) -> None:
    headers, user_id = _new_user(client)
    channel = _enable_webhook(client, headers, "http://127.0.0.1:9/hook")
    now = datetime.now(UTC)
    session.add(
        NotificationDelivery(
            user_id=user_id,
            channel="webhook",
            alert_type="bill_due",
            item_key="bill_due:2",
            severity="warning",
            occurred_at=now,
            link="/bills",
            status="pending",
            attempts=0,
            next_attempt_at=now + timedelta(hours=1),
        )
    )
    session.commit()
    r = client.patch(
        f"/api/v1/notifications/channels/{channel['id']}",
        headers=headers,
        json={"enabled": False},
    )
    assert r.status_code == 200, r.text
    assert _deliveries(session, user_id)[0].status == "cancelled"


# --- retry / backoff / dead-letter -------------------------------------------


def test_retry_backoff_dead_letter_and_retry_endpoint(client: TestClient, session: Session) -> None:
    sink = _WebhookSink(status=500)
    try:
        headers, user_id = _new_user(client)
        _enable_webhook(client, headers, sink.url)
        _seed_bill(client, headers)

        stats = run_once(session, user_id=user_id)
        assert stats == {
            "scanned_users": 1,
            "enqueued": 1,
            "retracted": 0,
            "attempted": 1,
            "sent": 0,
            "retrying": 1,
            "dead": 0,
            "cancelled": 0,
        }, stats
        row = _deliveries(session, user_id)[0]
        assert row.status == "pending" and row.attempts == 1
        delay = (row.next_attempt_at - datetime.now(UTC)).total_seconds()
        assert 20 <= delay <= 45, f"expected the 30s backoff step, got {delay}s"
        log = _attempts(session, row.id)
        assert len(log) == 1
        assert log[0].outcome == "retryable" and log[0].http_status == 500
        assert log[0].error_code == "webhook_http_error"

        for _ in range(4):
            _age_delivery(session, row.id)
            run_once(session, user_id=user_id)
        row = _deliveries(session, user_id)[0]
        assert row.status == "dead" and row.attempts == 5 and row.dead_at is not None
        assert len(_attempts(session, row.id)) == 5

        dead = client.get(
            "/api/v1/notifications/deliveries",
            headers=headers,
            params={"status": "dead"},
        ).json()
        assert dead["total"] == 1
        item = dead["items"][0]
        assert item["status"] == "dead" and len(item["attempt_log"]) == 5
        assert all(
            a["outcome"] == "retryable" and a["http_status"] == 500 for a in item["attempt_log"]
        )

        r = client.post(f"/api/v1/notifications/deliveries/{row.id}/retry", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending" and body["attempts"] == 0
        assert body["dead_at"] is None and len(body["attempt_log"]) == 5, "history preserved"

        r = client.post(f"/api/v1/notifications/deliveries/{row.id}/retry", headers=headers)
        assert r.status_code == 409 and r.json()["code"] == "delivery_not_retryable"
    finally:
        sink.close()


def test_fatal_4xx_dead_letters_on_first_attempt(client: TestClient, session: Session) -> None:
    sink = _WebhookSink(status=404)
    try:
        headers, user_id = _new_user(client)
        _enable_webhook(client, headers, sink.url)
        _seed_bill(client, headers)
        stats = run_once(session, user_id=user_id)
        assert stats["dead"] == 1 and stats["retrying"] == 0, stats
        row = _deliveries(session, user_id)[0]
        assert row.status == "dead" and row.attempts == 1
        log = _attempts(session, row.id)
        assert log[0].outcome == "fatal" and log[0].http_status == 404
        # One attempt only — no retry storm against a rejecting endpoint.
        _age_delivery(session, row.id)
        assert run_once(session, user_id=user_id)["attempted"] == 0
    finally:
        sink.close()


def test_failed_delivery_leaves_inbox_intact(client: TestClient, session: Session) -> None:
    """Acceptance: failures never touch the inbox or the generation audit rows."""
    sink = _WebhookSink(status=500)
    try:
        headers, user_id = _new_user(client)
        _enable_webhook(client, headers, sink.url)
        _seed_budget(client, session, headers, user_id)
        _seed_bill(client, headers)

        inbox_before = client.get("/api/v1/notifications/inbox", headers=headers).json()
        assert inbox_before["total"] >= 2
        alerts_before = len(
            session.exec(select(SpendingAlert).where(SpendingAlert.user_id == user_id)).all()
        )
        assert alerts_before >= 1

        row = None
        for _ in range(5):
            run_once(session, user_id=user_id)
            rows = _deliveries(session, user_id)
            pending = [r for r in rows if r.status == "pending"]
            if not pending:
                break
            row = pending[0]
            _age_delivery(session, row.id)

        inbox_after = client.get("/api/v1/notifications/inbox", headers=headers).json()
        assert inbox_after == inbox_before, "delivery failure must not disturb the inbox"
        alerts_after = len(
            session.exec(select(SpendingAlert).where(SpendingAlert.user_id == user_id)).all()
        )
        assert alerts_after == alerts_before, "dispatch must not write generation rows"
        assert sink.count >= 1
        assert all(r.status in {"dead", "pending"} for r in _deliveries(session, user_id))
    finally:
        sink.close()


# --- tenancy / audit / API surface -------------------------------------------


def test_run_endpoint_returns_stats(client: TestClient, session: Session) -> None:
    sink = _WebhookSink()
    try:
        headers, _ = _new_user(client)
        _enable_webhook(client, headers, sink.url)
        _seed_bill(client, headers)
        r = client.post("/api/v1/notifications/deliveries/run", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == _STAT_KEYS
        assert body["scanned_users"] == 1 and body["enqueued"] == 1 and body["sent"] == 1
        assert sink.count == 1
    finally:
        sink.close()


def test_tenancy_delivery_ids_and_list_scoping(client: TestClient, session: Session) -> None:
    sink = _WebhookSink()
    try:
        headers_a, user_a = _new_user(client)
        headers_b, user_b = _new_user(client)
        _enable_webhook(client, headers_a, sink.url)
        _seed_bill(client, headers_a)
        assert run_once(session, user_id=user_a)["sent"] == 1
        delivery_id = _deliveries(session, user_a)[0].id

        page = client.get("/api/v1/notifications/deliveries", headers=headers_b).json()
        assert page["total"] == 0 and page["items"] == []
        r = client.post(f"/api/v1/notifications/deliveries/{delivery_id}/retry", headers=headers_b)
        assert r.status_code == 404 and r.json()["code"] == "not_found"

        before = len(_attempts(session, delivery_id))
        assert run_once(session, user_id=user_b)["attempted"] == 0
        assert len(_attempts(session, delivery_id)) == before
        assert _deliveries(session, user_a)[0].status == "sent"
    finally:
        sink.close()


def test_deliveries_list_envelope_filters_and_validation(client: TestClient) -> None:
    headers, _ = _new_user(client)
    r = client.get("/api/v1/notifications/deliveries", headers=headers)
    assert r.status_code == 200
    assert set(r.json()) == {"items", "total", "limit", "offset"}
    assert r.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}

    for params in ({"status": "bogus"}, {"limit": 0}, {"limit": 201}, {"offset": -1}):
        r = client.get("/api/v1/notifications/deliveries", params=params, headers=headers)
        assert r.status_code == 422, params
        assert r.json()["code"] == "validation_error", params

    r = client.get("/api/v1/notifications/deliveries", headers=headers)
    assert r.status_code == 200


def test_demo_reads_open_writes_blocked(client: TestClient, session: Session) -> None:
    user = _make_user(session, DEMO_EMAIL)
    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}

    assert client.get("/api/v1/notifications/channels", headers=headers).status_code == 200
    assert client.get("/api/v1/notifications/deliveries", headers=headers).status_code == 200

    channels = client.get("/api/v1/notifications/channels", headers=headers).json()
    for method, path, kwargs in (
        (
            "patch",
            f"/api/v1/notifications/channels/{channels[0]['id']}",
            {"json": {"enabled": True}},
        ),
        ("post", "/api/v1/notifications/deliveries/run", {}),
        ("post", "/api/v1/notifications/deliveries/1/retry", {}),
    ):
        r = getattr(client, method)(path, headers=headers, **kwargs)
        assert r.status_code == 403, path
        assert r.json()["code"] == "demo_account_restricted", path


def test_export_scopes_new_tables_to_caller(client: TestClient, session: Session) -> None:
    """docs/11 step 2 keeps holding for the M59 tables (registry-driven)."""
    sink = _WebhookSink()
    try:
        headers, user_id = _new_user(client)
        _enable_webhook(client, headers, sink.url)
        _seed_bill(client, headers)
        assert run_once(session, user_id=user_id)["sent"] == 1

        r = client.get("/api/v1/export/full", headers=headers)
        assert r.status_code == 200, r.text
        snap = r.json()
        for table in (
            "notification_channels",
            "notification_deliveries",
            "notification_delivery_attempts",
        ):
            assert table in snap, f"{table} missing from export"
            assert snap[table], f"{table} exported empty despite rows"
            assert {row["user_id"] for row in snap[table]} == {user_id}
        # No credential material is exported (docs/37: env-only secrets).
        blob = json.dumps(snap)
        assert "notify_smtp_password" not in blob
        assert get_settings().notify_smtp_password == "" or "smtp_password" not in blob
    finally:
        sink.close()


def test_new_routes_listed_in_openapi(client: TestClient) -> None:
    """docs/08 contract: every delivery route exists on the running app."""
    client.get("/api/v1/notifications")
    # M87 (docs/42): /openapi.json is authenticated-only over HTTP; the schema
    # itself is what is under test here.
    from app.main import app

    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/notifications/channels",
        "/api/v1/notifications/channels/{channel_id}",
        "/api/v1/notifications/deliveries",
        "/api/v1/notifications/deliveries/run",
        "/api/v1/notifications/deliveries/{delivery_id}/retry",
    ):
        assert path in paths, path


def test_worker_opens_a_session_that_can_run_queries() -> None:
    """Regression: the asyncio worker opened `sqlalchemy.orm.Session`, which has
    no `.exec()` — every tick failed with `AttributeError`, invisible to the
    test suite because requests inject a SQLModel session through `get_db`.
    Only a live tick (running-stack smoke) hit it, so pin the class here."""
    from app.services.notify_delivery import _open_session

    db = _open_session()
    try:
        assert hasattr(db, "exec"), f"worker session must be SQLModel's, got {type(db).__name__}"
        assert db.exec(select(func.count()).select_from(NotificationDelivery)).one() is not None
    finally:
        db.close()
