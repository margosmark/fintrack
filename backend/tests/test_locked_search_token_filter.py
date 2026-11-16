from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.users import User
from tests.test_cat_common import auth, create_account, make_transaction
from tests.test_search import PASSWORD


def _register(client: TestClient, session: Session, email: str) -> tuple[dict, int]:
    client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    user_id = session.exec(select(User.id).where(User.email == email)).one()
    return auth(response.json()["access_token"]), user_id


def test_locked_token_search_excludes_encrypted_rows_and_is_tenant_scoped(
    client: TestClient, session: Session
) -> None:
    """The locked-vault filter applies to exact token hits as well as fuzzy hits."""
    headers_a, user_a = _register(client, session, "locked-token-a@example.com")
    account_a = create_account(client, headers_a)
    token = "same-blind-index-token"
    plaintext = make_transaction(
        session, user_a, account_a, "Merchant Plaintext", "-12.00", payee_token=token
    )
    encrypted = make_transaction(
        session,
        user_a,
        account_a,
        "Merchant Cipher",
        "-13.00",
        payee_token=token,
        payee_cipher="opaque-ciphertext",
    )

    headers_b, user_b = _register(client, session, "locked-token-b@example.com")
    account_b = create_account(client, headers_b)
    foreign = make_transaction(
        session, user_b, account_b, "Merchant Other Tenant", "-14.00", payee_token=token
    )

    response = client.get(
        "/api/v1/search",
        params={"q": "merchant", "payee_token": token, "hide_encrypted": "true", "limit": 10},
        headers=headers_a,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    result_ids = {row["id"] for row in body["transactions"]}

    assert result_ids == {plaintext.id}
    assert encrypted.id not in result_ids
    assert foreign.id not in result_ids
    assert all(row["encrypted"] is False for row in body["transactions"])
    assert body["withheld"] == 0
