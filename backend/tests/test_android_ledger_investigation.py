from datetime import date
from decimal import Decimal

from sqlmodel import select

from app.models.transactions import Transaction
from app.models.users import User


def test_android_ledger_contract(client, session):
    def register(email):
        payload = {"email": email, "password": "correct-horse-battery"}
        assert client.post("/api/v1/auth/register", json=payload).status_code == 200
        response = client.post("/api/v1/auth/login", json=payload)
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    owner = register("ledger-owner@example.com")
    other = register("ledger-other@example.com")
    account = client.post(
        "/api/v1/accounts",
        headers=owner,
        json={"name": "Synthetic", "type": "chequing", "currency": "CAD"},
    ).json()
    url = "/api/v1/accounts/ledger"
    params = {"account_id": account["id"]}
    empty = client.get(url, headers=owner, params=params)
    assert empty.status_code == 200
    assert empty.json()["items"] == [] and empty.json()["total"] == 0
    assert client.get(url, headers=other, params=params).status_code == 404
    assert client.get(url, headers=owner).status_code == 422
    user = session.exec(select(User).where(User.email == "ledger-owner@example.com")).one()
    for i in range(125):
        session.add(
            Transaction(
                user_id=user.id,
                account_id=account["id"],
                date=date(2024, 1, 1),
                raw_description=f"item {i}",
                normalized_payee=f"item {i}",
                amount_native=Decimal("1.00"),
                currency="CAD",
                dedup_hash=f"{i:064x}",
            )
        )
    session.flush()
    response = client.get(url, headers=owner, params=params)
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) == page["total"] == 125
    assert page["current_balance"] == "125.00"
    assert page["items"][0]["running_balance"] == "1.00"
    assert client.get(url, headers=other, params=params).status_code == 404
