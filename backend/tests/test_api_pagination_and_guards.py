"""Pagination + ownership-guard behavior for the refactored routers.

Verifies that the shared ``Pagination``/``paginate`` helpers and
``owned_or_404`` guard (docs/08 contract) preserve observable behavior:
paged list endpoints return the right *Page shape, and a row owned by another
user yields 404 rather than leaking.
"""

from datetime import datetime

from fastapi.testclient import TestClient

API = "/api/v1"
FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "investment_sample.csv"


def _auth(client: TestClient) -> dict[str, str]:
    email = f"pg-{datetime.now().timestamp()}-{id(client)}@example.com"
    client.post(
        f"{API}/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    body = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(client: TestClient, headers: dict[str, str], type_: str) -> dict:
    body = client.post(
        f"{API}/accounts",
        json={"name": f"Acct {type_}", "type": type_, "currency": "CAD"},
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _upload(client: TestClient, headers: dict[str, str], account_id: int):
    with open(FIXTURE, "rb") as f:
        return client.post(
            f"{API}/trades/import",
            files={"file": ("investment_sample.csv", f, "text/csv")},
            data={"account_id": account_id},
            headers=headers,
        )


def _seed(client: TestClient, headers: dict[str, str]):
    """Upload the sample CSV and return (account_id, batch_id)."""
    acct = _account(client, headers, "brokerage_non_registered")
    resp = _upload(client, headers, acct["id"])
    assert resp.status_code == 201, resp.text
    return acct["id"], resp.json()["batch_id"]


def test_list_trades_pagination(client: TestClient):
    headers = _auth(client)
    _seed(client, headers)

    first = client.get(f"{API}/trades", params={"limit": 2, "offset": 0}, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert body["total"] >= 5
    assert len(body["items"]) == 2

    second = client.get(f"{API}/trades", params={"limit": 2, "offset": 2}, headers=headers)
    assert second.status_code == 200
    assert second.json()["offset"] == 2
    # Pages are disjoint.
    first_ids = {i["id"] for i in first.json()["items"]}
    second_ids = {i["id"] for i in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_list_securities_pagination(client: TestClient):
    headers = _auth(client)
    _seed(client, headers)

    resp = client.get(f"{API}/securities", params={"limit": 2, "offset": 0}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert body["total"] >= 4  # VEQT, XEQT, ZGRO, CASH at least
    assert len(body["items"]) == 2


def test_owned_trade_returns_404_for_other_user(client: TestClient):
    owner = _auth(client)
    _account_id, _ = _seed(client, owner)

    mine = client.get(f"{API}/trades", params={"limit": 1}, headers=owner)
    trade_id = mine.json()["items"][0]["id"]

    other = _auth(client)
    resp = client.get(f"{API}/trades/{trade_id}", headers=other)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_list_prices_pagination_shape(client: TestClient):
    headers = _auth(client)
    resp = client.get(f"{API}/prices", params={"limit": 3, "offset": 0}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 3
    assert body["offset"] == 0
    assert "items" in body and "total" in body


def test_list_acb_pools_pagination_after_rebuild(client: TestClient):
    """ACB pools are derived from COMMITTED trades only (docs/03 staging model).

    A freshly uploaded investment CSV sits in `pending_review`, and
    `app.services.trade_filters.committed_trade_conditions` — the shared
    predicate every trade-derived live read must apply — makes its rows
    invisible to the ACB replay. Rebuilding an uncommitted import therefore
    correctly persists nothing; the pool page is populated only once the
    batch is committed. Both halves are pinned below so the page shape and
    the staging scope cannot regress independently.
    """
    headers = _auth(client)
    _, batch_id = _seed(client, headers)

    # Before commit: the rebuild is a no-op by design, not a broken write path.
    early = client.post(f"{API}/acb/rebuild", headers=headers)
    assert early.status_code == 200, early.text
    assert early.json()["pools"] == 0
    staged_page = client.get(f"{API}/acb", params={"limit": 5, "offset": 0}, headers=headers)
    assert staged_page.status_code == 200, staged_page.text
    assert staged_page.json()["total"] == 0
    assert staged_page.json()["items"] == []

    commit = client.post(f"{API}/trades/import/{batch_id}/commit", headers=headers)
    assert commit.status_code == 200, commit.text
    assert commit.json()["status"] == "committed"

    rebuild = client.post(f"{API}/acb/rebuild", headers=headers)
    assert rebuild.status_code == 200, rebuild.text
    assert rebuild.json()["pools"] >= 1

    resp = client.get(f"{API}/acb", params={"limit": 5, "offset": 0}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert body["total"] >= 1
    # The *Page shape is the point of this file: items are the page window.
    assert len(body["items"]) == min(5, body["total"])
    assert {i["symbol"] for i in body["items"]} <= {"VEQT", "XEQT", "ZGRO"}

    # A second page stays disjoint and respects the offset.
    page2 = client.get(f"{API}/acb", params={"limit": 5, "offset": 5}, headers=headers)
    assert page2.status_code == 200, page2.text
    assert page2.json()["offset"] == 5
    assert {i["id"] for i in body["items"]}.isdisjoint({i["id"] for i in page2.json()["items"]})


def test_offset_past_the_ceiling_is_422_not_500(client: TestClient):
    """M98b F-4: `offset` was unbounded while `limit` was not.

    `offset >= 2**63` was rendered as a bigint OFFSET Postgres rejects
    ("integer out of range") — an unhandled 500 `internal_error` envelope,
    reproduced live on prod for `/tags` and `/transactions` before this fix.
    The bound now lives on the shared pager, so both representative paged
    routes answer the standard 422 validation envelope instead.
    """
    headers = _auth(client)
    for route in ("/tags", "/transactions"):
        resp = client.get(f"{API}{route}", params={"offset": 2**63}, headers=headers)
        assert resp.status_code == 422, f"{route}: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["code"] == "validation_error"
        assert isinstance(body["detail"], list) and body["detail"]
        assert body["detail"][0]["loc"] == ["query", "offset"]


def test_offset_zero_and_the_ceiling_still_page(client: TestClient):
    """The ceiling is a bound, not a page lock: legal offsets keep working."""
    headers = _auth(client)
    for offset in (0, 1_000_000):
        resp = client.get(f"{API}/tags", params={"limit": 1, "offset": offset}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["offset"] == offset

    # One past the ceiling is refused with the same envelope as far past it.
    resp = client.get(f"{API}/tags", params={"offset": 1_000_001}, headers=headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "validation_error"


def test_own_query_param_routes_offset_past_the_ceiling_is_422_not_500(client: TestClient):
    """M98b F-4 tail: two routes declare their OWN `offset` query param.

    `/mcp-audit` (mcp_audit.py) and `/notifications/deliveries`
    (notifications.py) do not go through `deps.get_pagination`, so the
    shared bound never reached them: `offset >= 2**63` was still rendered as
    a bigint OFFSET Postgres rejects ("integer out of range") — the
    catch-all 500 `internal_error` envelope, reproduced live on prod for
    `/mcp-audit?offset=9223372036854775808` before this fix. Both now carry
    `le=MAX_OFFSET` on their own Query and answer the standard 422 envelope.
    """
    headers = _auth(client)
    for route in ("/mcp-audit", "/notifications/deliveries"):
        resp = client.get(f"{API}{route}", params={"offset": 2**63}, headers=headers)
        assert resp.status_code == 422, f"{route}: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["code"] == "validation_error"
        assert isinstance(body["detail"], list) and body["detail"]
        assert body["detail"][0]["loc"] == ["query", "offset"]


def test_own_query_param_routes_legal_offsets_still_page(client: TestClient):
    """The ceiling is a bound, not a page lock, on the self-declaring routes."""
    headers = _auth(client)
    for route in ("/mcp-audit", "/notifications/deliveries"):
        for offset in (0, 1_000_000):
            resp = client.get(
                f"{API}{route}", params={"limit": 1, "offset": offset}, headers=headers
            )
            assert resp.status_code == 200, (
                f"{route} offset={offset}: {resp.status_code} {resp.text}"
            )
            if route == "/notifications/deliveries":
                assert resp.json()["offset"] == offset

        resp = client.get(f"{API}{route}", params={"offset": 1_000_001}, headers=headers)
        assert resp.status_code == 422, f"{route}: {resp.status_code} {resp.text}"
        assert resp.json()["code"] == "validation_error"
