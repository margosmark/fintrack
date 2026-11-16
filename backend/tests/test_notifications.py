"""Notification settings + the M51 in-app inbox (docs/35)."""

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.security import create_access_token, hash_password
from app.models.notifications import NotificationSetting
from app.models.spending_alerts import SpendingAlert
from app.models.users import User
from app.services.demo_seed import DEMO_EMAIL
from app.services.txn_filters import today_et
from tests.test_deep_common import auth, create_account, get_category, register, seed_transaction


def _today() -> date:
    """The clock, read at CALL time — never captured at import.

    pytest imports every test module long before it runs any of them, so a
    date frozen when this module is imported is already a day stale by the
    time a suite run crosses local midnight, while the product re-reads
    ``today_et()`` on every request. The two disagreed by a day and the
    bill-severity assertion flipped from ``warning`` to ``critical``
    (t_435c2d14).
    """
    return today_et()


_LEGACY_TYPES = {
    "large_transaction",
    "low_balance",
    "dividend_received",
    "price_alert",
    "monthly_summary",
}
_GENERATOR_TYPES = {"budget_threshold", "bill_due", "tax_loss"}


def _auth(client: TestClient) -> dict[str, str]:
    from datetime import UTC, datetime

    email = f"notif-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _setting_id(client: TestClient, headers: dict, alert_type: str) -> int:
    rows = client.get("/api/v1/notifications", headers=headers).json()
    return next(s["id"] for s in rows if s["alert_type"] == alert_type)


def _set_enabled(client: TestClient, headers: dict, alert_type: str, enabled: bool) -> None:
    r = client.patch(
        f"/api/v1/notifications/{_setting_id(client, headers, alert_type)}",
        headers=headers,
        json={"enabled": enabled},
    )
    assert r.status_code == 200, r.text


def _seed_budget_alert(client: TestClient, session: Session) -> tuple[dict, int]:
    """User + account + groceries budget(200) + a -337 CAD transaction today.

    337/200 = 168.5% >= the 100% critical threshold, so `_check_alerts` writes
    a `critical` `spending_alerts` row on the first inbox call.
    """
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
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
    return headers, user_id


def _make_user(session: Session, email: str) -> User:
    user = User(email=email, hashed_password=hash_password("correct-horse-battery"))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# --- settings ---------------------------------------------------------------


def test_notification_settings_auto_created(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/notifications", headers=headers)
    assert r.status_code == 200
    settings = r.json()
    assert len(settings) == 8  # 5 legacy (M31) + 3 generator-backed (M51)
    types = {s["alert_type"] for s in settings}
    assert types == _LEGACY_TYPES | _GENERATOR_TYPES


def test_notification_settings_idempotent(client: TestClient) -> None:
    """Calling list twice doesn't create duplicate settings."""
    headers = _auth(client)
    r1 = client.get("/api/v1/notifications", headers=headers)
    r2 = client.get("/api/v1/notifications", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert len(r1.json()) == len(r2.json()) == 8


def test_notification_setting_response_shape(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/notifications", headers=headers)
    setting = r.json()[0]
    assert "id" in setting
    assert "user_id" in setting
    assert "alert_type" in setting
    assert "enabled" in setting
    assert isinstance(setting["enabled"], bool)

    by_type = {s["alert_type"]: s for s in r.json()}
    # Legacy M31 types have no generator and stay disabled (docs/35).
    assert all(by_type[t]["enabled"] is False for t in _LEGACY_TYPES)
    # The three generator-backed types ship enabled so alerts are visible by
    # default (docs/35 decision 2).
    assert all(by_type[t]["enabled"] is True for t in _GENERATOR_TYPES)


def test_toggle_notification_setting(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/notifications", headers=headers)
    setting_id = r.json()[0]["id"]

    r = client.patch(
        f"/api/v1/notifications/{setting_id}", headers=headers, json={"enabled": False}
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    # Toggle back
    r = client.patch(f"/api/v1/notifications/{setting_id}", headers=headers, json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_notification_foreign_setting_rejected(client: TestClient) -> None:
    headers = _auth(client)
    r = client.patch("/api/v1/notifications/99999", headers=headers, json={"enabled": True})
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_notification_tenancy_isolation(client: TestClient) -> None:
    """User A's settings are not visible to User B."""
    headers_a = _auth(client)
    headers_b = _auth(client)

    r_a = client.get("/api/v1/notifications", headers=headers_a)
    r_b = client.get("/api/v1/notifications", headers=headers_b)
    ids_a = {s["id"] for s in r_a.json()}
    ids_b = {s["id"] for s in r_b.json()}
    assert ids_a.isdisjoint(ids_b), "Settings from different users should not overlap"


# --- inbox: shape, auth -----------------------------------------------------


def test_inbox_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/notifications/inbox")
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_token"


def test_inbox_empty_shape(client: TestClient) -> None:
    """Fresh user, no data -> the pinned page envelope with every field."""
    headers = _auth(client)
    r = client.get("/api/v1/notifications/inbox", headers=headers)
    assert r.status_code == 200
    page = r.json()
    assert page["items"] == []
    assert page["total"] == 0
    assert page["unread_count"] == 0
    assert page["limit"] == 50
    assert page["cursor"] is None
    assert page["next_cursor"] is None


def test_inbox_excludes_legacy_types_entirely(client: TestClient, session: Session) -> None:
    """Turning all five legacy types ON produces no items: they have no
    generator (docs/35, "Plain statement of what is NOT wired")."""
    headers, _user_id = _seed_budget_alert(client, session)
    for alert_type in _LEGACY_TYPES:
        _set_enabled(client, headers, alert_type, True)

    r = client.get("/api/v1/notifications/inbox", headers=headers)
    assert r.status_code == 200
    page = r.json()
    assert page["total"] >= 1  # the budget alert is there...
    kinds = {item["kind"] for item in page["items"]}
    assert kinds == {"budget_threshold"}  # ...and nothing from the legacy five


# --- inbox: the toggle gates the surface, not the data ----------------------


def test_budget_toggle_gates_surface_not_generation(client: TestClient, session: Session) -> None:
    """Toggle OFF -> type excluded from the inbox, yet `spending_alerts` rows
    are still written. Toggle ON -> visible again."""
    headers, user_id = _seed_budget_alert(client, session)

    # --- toggle ON (default): visible
    r = client.get("/api/v1/notifications/inbox", headers=headers)
    assert r.status_code == 200
    page = r.json()
    budget_items = [i for i in page["items"] if i["kind"] == "budget_threshold"]
    assert len(budget_items) == 1, page
    item = budget_items[0]
    assert item["id"].startswith("budget_threshold:")
    assert item["severity"] == "critical"  # 168.5% >= 100% threshold
    assert item["link"] == "/budgets"
    assert item["amount_cad"] == "337.00"
    assert item["read"] is False
    assert item["read_at"] is None
    assert item["occurred_at"].endswith("+00:00") or "+" in item["occurred_at"]
    assert page["unread_count"] == 1

    rows_before = session.exec(select(SpendingAlert).where(SpendingAlert.user_id == user_id)).all()
    assert len(rows_before) >= 1  # generation already wrote the audit row

    # --- toggle OFF: surface hidden, data untouched
    _set_enabled(client, headers, "budget_threshold", False)
    r = client.get("/api/v1/notifications/inbox", headers=headers)
    assert r.status_code == 200
    page = r.json()
    assert all(i["kind"] != "budget_threshold" for i in page["items"])
    assert page["total"] == 0

    session.expire_all()
    rows_after = session.exec(select(SpendingAlert).where(SpendingAlert.user_id == user_id)).all()
    assert len(rows_after) >= 1, "generation must keep writing audit rows while gated"
    assert len(rows_after) >= len(rows_before)

    # --- toggle ON again: visible again
    _set_enabled(client, headers, "budget_threshold", True)
    r = client.get("/api/v1/notifications/inbox", headers=headers)
    kinds = {i["kind"] for i in r.json()["items"]}
    assert "budget_threshold" in kinds


# --- inbox: tenancy ---------------------------------------------------------


def test_inbox_tenancy_foreign_id_404(client: TestClient, session: Session) -> None:
    """A foreign inbox id must be 404 `not_found`, never a write."""
    headers_a, _user_a = _seed_budget_alert(client, session)
    headers_b = _auth(client)

    r = client.get("/api/v1/notifications/inbox", headers=headers_a)
    foreign_id = next(i["id"] for i in r.json()["items"] if i["kind"] == "budget_threshold")

    r = client.post(f"/api/v1/notifications/inbox/{foreign_id}/read", headers=headers_b)
    assert r.status_code == 404
    assert r.json() == {"detail": "Notification not found", "code": "not_found"}

    # B's inbox never contains A's items
    r = client.get("/api/v1/notifications/inbox", headers=headers_b)
    assert all(i["id"] != foreign_id for i in r.json()["items"])

    # Malformed / unknown ids are 404 too (not a 500)
    for bad in ("budget_threshold:999999", "nonsense", "tax_loss:not:integers"):
        r = client.post(f"/api/v1/notifications/inbox/{bad}/read", headers=headers_a)
        assert r.status_code == 404, bad
        assert r.json()["code"] == "not_found"

    # A's own id still works (control)
    r = client.post(f"/api/v1/notifications/inbox/{foreign_id}/read", headers=headers_a)
    assert r.status_code == 200, r.text
    assert r.json()["read"] is True


# --- inbox: cursor ----------------------------------------------------------


def test_inbox_invalid_cursor_422(client: TestClient) -> None:
    """Malformed / wrong-version cursors -> 422 envelope with a PINNED code."""
    headers = _auth(client)
    bad_cursors = [
        "!!!not-base64!!!",
        # valid base64url but not a v1 cursor object
        "WzEsMiwzXQ",
    ]
    for bad in bad_cursors:
        r = client.get("/api/v1/notifications/inbox", params={"cursor": bad}, headers=headers)
        assert r.status_code == 422, bad
        assert r.json()["code"] == "invalid_cursor", bad


def test_inbox_limit_out_of_range_422(client: TestClient) -> None:
    """`limit` keeps the docs/08 contract (1..200) -> validation_error."""
    headers = _auth(client)
    for bad in (0, 201, -1):
        r = client.get("/api/v1/notifications/inbox", params={"limit": bad}, headers=headers)
        assert r.status_code == 422, bad
        assert r.json()["code"] == "validation_error", bad


def test_inbox_cursor_walk_no_dup_no_skip(client: TestClient) -> None:
    """Walk pages with `next_cursor`: union == total, no dup, terminal null."""
    headers = _auth(client)
    # Four bills inside their reminder window -> four `bill_due` items and no
    # other source (no budgets, no taxable positions for a fresh user).
    for offset in range(1, 5):
        r = client.post(
            "/api/v1/bills",
            headers=headers,
            json={
                "name": f"Bill {offset}",
                "amount": "25.00",
                "frequency": "monthly",
                "next_due_date": (_today() + timedelta(days=offset)).isoformat(),
                "remind_days_before": 7,
            },
        )
        assert r.status_code == 201, r.text

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        r = client.get("/api/v1/notifications/inbox", params=params, headers=headers)
        assert r.status_code == 200, r.text
        page = r.json()
        assert page["total"] == 4
        seen.extend(i["id"] for i in page["items"])
        cursor = page["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "cursor never terminated"

    assert pages == 2  # 4 items / limit 2 -> two pages, terminal null
    assert len(seen) == 4
    assert len(set(seen)) == 4, f"duplicate items across pages: {seen}"
    assert all(i.startswith("bill_due:") for i in seen)


# --- inbox: read state ------------------------------------------------------


def test_inbox_read_idempotent_then_read_all(client: TestClient) -> None:
    headers = _auth(client)
    for offset in range(1, 4):
        r = client.post(
            "/api/v1/bills",
            headers=headers,
            json={
                "name": f"Bill {offset}",
                "amount": "10.00",
                "frequency": "monthly",
                "next_due_date": (_today() + timedelta(days=offset)).isoformat(),
                "remind_days_before": 3,
            },
        )
        assert r.status_code == 201, r.text

    page = client.get("/api/v1/notifications/inbox", headers=headers).json()
    assert page["total"] == 3
    assert page["unread_count"] == 3
    target = page["items"][0]

    r = client.post(f"/api/v1/notifications/inbox/{target['id']}/read", headers=headers)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first == {"id": target["id"], "read": True, "read_at": first["read_at"]}

    # idempotent: a repeat returns the ORIGINAL read_at
    r = client.post(f"/api/v1/notifications/inbox/{target['id']}/read", headers=headers)
    assert r.status_code == 200
    assert r.json()["read_at"] == first["read_at"]

    page = client.get("/api/v1/notifications/inbox", headers=headers).json()
    assert page["total"] == 3  # read items stay in the inbox
    assert page["unread_count"] == 2
    by_id = {i["id"]: i for i in page["items"]}
    assert by_id[target["id"]]["read"] is True
    assert by_id[target["id"]]["read_at"] == first["read_at"]

    r = client.post("/api/v1/notifications/inbox/read-all", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"marked": 2, "unread_count": 0}

    page = client.get("/api/v1/notifications/inbox", headers=headers).json()
    assert page["unread_count"] == 0
    assert all(i["read"] is True for i in page["items"])

    # read-all is idempotent too
    r = client.post("/api/v1/notifications/inbox/read-all", headers=headers)
    assert r.json() == {"marked": 0, "unread_count": 0}


def test_inbox_read_records_state_even_when_toggle_off(client: TestClient) -> None:
    """Read state is data: hiding the surface does not block recording it."""
    headers = _auth(client)
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": "Internet",
            "amount": "80.00",
            "frequency": "monthly",
            "next_due_date": (_today() + timedelta(days=1)).isoformat(),
            "remind_days_before": 3,
        },
    )
    assert r.status_code == 201
    item_id = f"bill_due:{r.json()['id']}"

    _set_enabled(client, headers, "bill_due", False)
    assert client.get("/api/v1/notifications/inbox", headers=headers).json()["total"] == 0

    r = client.post(f"/api/v1/notifications/inbox/{item_id}/read", headers=headers)
    assert r.status_code == 200, r.text

    _set_enabled(client, headers, "bill_due", True)
    page = client.get("/api/v1/notifications/inbox", headers=headers).json()
    assert page["unread_count"] == 0
    assert page["items"][0]["read"] is True


def test_alert_percent_equals_budget_card_percent(client: TestClient, session: Session) -> None:
    """M61 #3 (M65): ONE number, rendered on two surfaces.

    The Dashboard alert and the Budgets card must print the same percentage for
    the same period/category. Regression: `_check_alerts` summed
    `abs(amount_native)` over its own window while `GET /budgets` summed signed
    reporting-currency net spend, so a refund or a USD charge put 201.94% on
    the Dashboard next to 100% on the card. Both now call
    `services.budgets.budget_spent`.
    """
    headers, _user_id = _seed_budget_alert(client, session)

    card = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert card["pct_used"] == 168.5  # 337.00 / 200.00 * 100 — true ratio, unclamped

    page = client.get("/api/v1/notifications/inbox", headers=headers).json()
    item = next(i for i in page["items"] if i["kind"] == "budget_threshold")
    # Whole percent, ties up: `Budgets.tsx` prints `Math.round(168.5)` = "169%",
    # so the body must say 169 too (docs/35 `body` format).
    assert item["body"] == "Spent 337.00 of 200.00 CAD (169% of limit; threshold 100%)"


# --- inbox: bill_due source rules ------------------------------------------


def test_bill_due_window_and_severity(client: TestClient) -> None:
    headers = _auth(client)

    def _bill(name: str, due: date, enabled: bool = True, remind: int = 3) -> int:
        r = client.post(
            "/api/v1/bills",
            headers=headers,
            json={
                "name": name,
                "amount": "42.00",
                "frequency": "monthly",
                "next_due_date": due.isoformat(),
                "remind_days_before": remind,
                "enabled": enabled,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    soon_id = _bill("Due soon", _today() + timedelta(days=1))
    overdue_id = _bill("Overdue", _today() - timedelta(days=1))
    _bill("Too far", _today() + timedelta(days=20))
    _bill("Disabled", _today() + timedelta(days=1), enabled=False)

    page = client.get("/api/v1/notifications/inbox", headers=headers).json()
    assert page["total"] == 2, page
    by_id = {i["id"]: i for i in page["items"]}

    soon = by_id[f"bill_due:{soon_id}"]
    assert soon["severity"] == "warning"
    assert soon["title"] == "Bill due soon: Due soon"
    # M116 (docs/35/37): the inbox lands on the BILL's own detail page.
    assert soon["link"] == f"/bills/{soon_id}"
    assert soon["amount_cad"] == "42.00"

    overdue = by_id[f"bill_due:{overdue_id}"]
    assert overdue["severity"] == "critical"
    assert overdue["title"] == "Bill overdue: Overdue"


def test_bill_toggle_excludes_bill_due(client: TestClient) -> None:
    headers = _auth(client)
    r = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": "Rent",
            "amount": "1500.00",
            "frequency": "monthly",
            "next_due_date": _today().isoformat(),
            "remind_days_before": 3,
        },
    )
    assert r.status_code == 201
    assert client.get("/api/v1/notifications/inbox", headers=headers).json()["total"] == 1

    _set_enabled(client, headers, "bill_due", False)
    page = client.get("/api/v1/notifications/inbox", headers=headers).json()
    assert page["total"] == 0
    assert page["unread_count"] == 0

    _set_enabled(client, headers, "bill_due", True)
    page = client.get("/api/v1/notifications/inbox", headers=headers).json()
    assert page["total"] == 1
    assert page["unread_count"] == 1


# --- demo -------------------------------------------------------------------


def test_inbox_demo_reads_open_writes_blocked(client: TestClient, session: Session) -> None:
    """Demo GETs stay open; the inbox POSTs inherit the global demo guard."""
    user = _make_user(session, DEMO_EMAIL)
    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}

    r = client.get("/api/v1/notifications/inbox", headers=headers)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"items", "total", "unread_count", "limit", "cursor", "next_cursor"}

    for path in (
        "/api/v1/notifications/inbox/read-all",
        "/api/v1/notifications/inbox/budget_threshold:1/read",
    ):
        r = client.post(path, headers=headers)
        assert r.status_code == 403, path
        assert r.json() == {
            "detail": "The demo account is read-only; changes are not allowed",
            "code": "demo_account_restricted",
        }


# --- model / settings table guard ------------------------------------------


def test_settings_table_indexes_every_generator_type(client: TestClient, session: Session) -> None:
    """The inbox reads settings straight from the table; a missing row means
    enabled — this pins that the INBOX itself seeds all three generator types
    (a user who never opened Settings still gets them)."""
    headers, _user_id = _seed_budget_alert(client, session)
    assert client.get("/api/v1/notifications/inbox", headers=headers).status_code == 200
    rows = session.exec(select(NotificationSetting)).all()
    seeded = {row.alert_type for row in rows if row.user_id == _user_id}
    assert _GENERATOR_TYPES <= seeded


def test_inbox_endpoint_listed_in_openapi(client: TestClient) -> None:
    """docs/08 contract: all three inbox operations exist."""
    client.get("/api/v1/notifications")
    # M87 (docs/42): /openapi.json is authenticated-only over HTTP; the schema
    # itself is what is under test here.
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/notifications/inbox" in paths
    assert "get" in paths["/api/v1/notifications/inbox"]
    assert "/api/v1/notifications/inbox/read-all" in paths
    assert "post" in paths["/api/v1/notifications/inbox/read-all"]
    read_path = "/api/v1/notifications/inbox/{item_id}/read"
    assert read_path in paths and "post" in paths[read_path]


def test_unread_count_and_total_cover_whole_inbox(client: TestClient) -> None:
    """`?limit=1` must still report the full totals — that is what the badge
    in AppShell relies on."""
    headers = _auth(client)
    for offset in range(1, 4):
        r = client.post(
            "/api/v1/bills",
            headers=headers,
            json={
                "name": f"B {offset}",
                "frequency": "monthly",
                "next_due_date": (_today() + timedelta(days=offset)).isoformat(),
                "remind_days_before": 3,
            },
        )
        assert r.status_code == 201, r.text

    page = client.get("/api/v1/notifications/inbox", params={"limit": 1}, headers=headers).json()
    assert page["limit"] == 1
    assert len(page["items"]) == 1
    assert page["total"] == 3
    assert page["unread_count"] == 3
