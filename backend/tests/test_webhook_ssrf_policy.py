"""Webhook SSRF policy: an UNVERIFIABLE host must fail CLOSED at send time.

`app/services/notify_delivery.py::validate_webhook_target` used to read

    addrs = _resolve(hostname)
    if addrs and any(_denied(addr) for addr in addrs):
        raise ...

`addrs` falsy means "DNS did not answer", and the `and` made that case pass.
At write time that is defensible — a webhook saved while its DNS was briefly
down should be storable. At SEND time it is not: the send then reached
`httpx.post`, which performs its OWN second lookup that the policy never sees,
so a hostname that answered publicly during validation and privately during
delivery reached an internal address.

`app/llm/egress.py` already had both halves (`at_send` fails closed);
`notify_delivery` reused its primitives but not its rule. These tests pin the
missing half.
"""

import pytest

from app.core.errors import APIError
from app.services import notify_delivery


def _settings_stub(**overrides):
    """A Settings-shaped stand-in so these tests do not depend on .env."""

    class _Stub:
        pass

    stub = _Stub()
    stub.notify_webhook_allow_private = False
    for key, value in overrides.items():
        setattr(stub, key, value)
    return stub


def test_unresolvable_target_is_storable_at_write_time(monkeypatch) -> None:
    """Write time keeps the permissive behaviour — DNS blips are not fatals."""
    monkeypatch.setattr(notify_delivery, "_resolve", lambda _h: None)
    notify_delivery.validate_webhook_target(
        "https://hooks.example.com/x", settings=_settings_stub(), at_send=False
    )


def test_unresolvable_target_fails_closed_at_send_time(monkeypatch) -> None:
    monkeypatch.setattr(notify_delivery, "_resolve", lambda _h: None)
    with pytest.raises(APIError) as exc:
        notify_delivery.validate_webhook_target(
            "https://hooks.example.com/x", settings=_settings_stub(), at_send=True
        )
    assert exc.value.code == "webhook_target_unverifiable"
    assert exc.value.status_code == 403


def test_private_target_is_refused_at_both_ends(monkeypatch) -> None:
    """The pre-existing deny still applies — this is the regression guard."""
    import ipaddress

    monkeypatch.setattr(notify_delivery, "_resolve", lambda _h: [ipaddress.ip_address("10.0.0.5")])
    for at_send in (False, True):
        with pytest.raises(APIError) as exc:
            notify_delivery.validate_webhook_target(
                "https://internal.example.com/x", settings=_settings_stub(), at_send=at_send
            )
        assert exc.value.code == "webhook_target_forbidden"


def test_operator_allow_private_still_short_circuits(monkeypatch) -> None:
    """`notify_webhook_allow_private` is an operator opt-out and is honoured
    BEFORE any DNS work, so it must keep working unchanged."""

    def _explode(_h):  # pragma: no cover - must never be reached
        raise AssertionError("allow_private must short-circuit before resolving")

    monkeypatch.setattr(notify_delivery, "_resolve", _explode)
    notify_delivery.validate_webhook_target(
        "https://internal.example.com/x",
        settings=_settings_stub(notify_webhook_allow_private=True),
        at_send=True,
    )


def test_send_webhook_does_not_post_to_an_unresolvable_host(monkeypatch) -> None:
    """End-to-end: the seam must never reach httpx for an unverifiable host.

    The outcome stays RETRYABLE — a DNS blip is transient, and before the
    check an unresolvable host reached `httpx.post` and came back as
    `webhook_unreachable` / retryable. Failing the delivery permanently on a
    temporary condition would lose the notification, so the refusal is what
    changed, not the retry semantics.
    """
    monkeypatch.setattr(notify_delivery, "_resolve", lambda _h: None)

    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("httpx.post called for an unresolvable webhook host")

    monkeypatch.setattr(notify_delivery.httpx, "post", _boom)

    result = notify_delivery.send_webhook(
        {"hello": "world"},
        "https://hooks.example.com/x",
        user_id=1,
        delivery_id=1,
        settings=_settings_stub(),
    )
    assert result.outcome == "retryable"
    assert result.error_code == "webhook_target_unverifiable"


def test_a_denied_address_stays_fatal(monkeypatch) -> None:
    """A policy refusal is permanent, so it must NOT be retried."""
    import ipaddress

    monkeypatch.setattr(notify_delivery, "_resolve", lambda _h: [ipaddress.ip_address("10.0.0.5")])

    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("httpx.post called for a denied webhook address")

    monkeypatch.setattr(notify_delivery.httpx, "post", _boom)

    result = notify_delivery.send_webhook(
        {"hello": "world"},
        "https://internal.example.com/x",
        user_id=1,
        delivery_id=1,
        settings=_settings_stub(),
    )
    assert result.outcome == "fatal"
    assert result.error_code == "webhook_target_forbidden"
