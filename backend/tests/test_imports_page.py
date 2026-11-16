"""Edge tests for the typed GET /imports batch list (docs/03, M2 pagination).

Verifies the staged-import batch history now returns an `ImportBatchPage`
(response_model) rather than a raw dict, that pagination is honored, that the
list is tenant-isolated, and that the shared `owned_or_404` guard rejects
cross-user access with a `not_found` envelope.
"""

from pathlib import Path

from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "imports-page@example.com"
PASSWORD = "correct-horse-battery"


def _register(client: TestClient, email: str = EMAIL) -> dict:
    client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_account(client: TestClient, token: str, currency: str = "CAD") -> int:
    inst = client.post("/api/v1/institutions", json={"name": "Test Bank"}, headers=_headers(token))
    assert inst.status_code == 201, inst.text
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "Checking",
            "type": "chequing",
            "currency": currency,
            "institution_id": inst.json()["id"],
        },
        headers=_headers(token),
    )
    assert account.status_code == 201, account.text
    return account.json()["id"]


def _upload_qfx(client: TestClient, token: str, account_id: int) -> dict:
    raw = (FIXTURES / "sample.qfx").read_bytes()
    resp = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": ("sample.qfx", raw, "application/octet-stream")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_list_batches_returns_typed_page(client: TestClient) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    _upload_qfx(client, auth["access_token"], account_id)

    body = client.get("/api/v1/imports", headers=_headers(auth["access_token"])).json()
    assert isinstance(body["total"], int)
    assert isinstance(body["limit"], int)
    assert isinstance(body["offset"], int)
    assert isinstance(body["items"], list)
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] > 0
    assert item["account_id"] == account_id
    assert item["status"] == "pending_review"
    assert set(item) >= {
        "id",
        "account_id",
        "institution",
        "file_name",
        "status",
        "imported",
        "duplicates_skipped",
        "unresolved",
    }


def test_list_batches_pagination_is_honored(client: TestClient) -> None:
    auth = _register(client)
    account_id = _make_account(client, auth["access_token"])
    for _ in range(3):
        _upload_qfx(client, auth["access_token"], account_id)

    headers = _headers(auth["access_token"])
    full = client.get("/api/v1/imports", headers=headers).json()
    assert full["total"] == 3

    limited = client.get("/api/v1/imports?limit=1&offset=1", headers=headers).json()
    assert limited["total"] == 3
    assert len(limited["items"]) == 1
    # order_by id desc: rows are [3, 2, 1]; offset 1 lands on the middle row.
    ids = sorted(b["id"] for b in full["items"])
    assert limited["items"][0]["id"] == ids[1]

    page2 = client.get("/api/v1/imports?limit=2", headers=headers).json()
    assert len(page2["items"]) == 2
    assert page2["limit"] == 2


def test_list_batches_is_tenant_isolated(client: TestClient) -> None:
    a = _register(client, email="a-imports@example.com")
    account_id = _make_account(client, a["access_token"])
    _upload_qfx(client, a["access_token"], account_id)

    b = _register(client, email="b-imports@example.com")
    resp = client.get("/api/v1/imports", headers=_headers(b["access_token"])).json()
    assert resp["total"] == 0
    assert resp["items"] == []


def test_get_other_users_batch_is_404(client: TestClient) -> None:
    a = _register(client, email="owner-imports@example.com")
    account_id = _make_account(client, a["access_token"])
    batch_id = _upload_qfx(client, a["access_token"], account_id)["id"]

    b = _register(client, email="intruder-imports@example.com")
    resp = client.get(f"/api/v1/imports/{batch_id}", headers=_headers(b["access_token"]))
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_upload_to_other_users_account_is_404(client: TestClient) -> None:
    owner = _register(client, email="owner-acct@example.com")
    owner_acct = _make_account(client, owner["access_token"])

    intruder = _register(client, email="intruder-acct@example.com")
    raw = (FIXTURES / "sample.qfx").read_bytes()
    resp = client.post(
        "/api/v1/imports/upload",
        headers=_headers(intruder["access_token"]),
        data={"account_id": str(owner_acct)},
        files={"file": ("sample.qfx", raw, "application/octet-stream")},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
