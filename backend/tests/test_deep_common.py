"""Shared helpers for deep feature tests."""

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.categories import Category
from app.models.transactions import Transaction


def register(client: TestClient) -> dict:
    email = f"deep-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_account(client: TestClient, headers: dict, name: str = "Chequing") -> int:
    r = client.post(
        "/api/v1/accounts",
        json={"name": name, "type": "chequing", "currency": "CAD"},
        headers=headers,
    )
    assert r.status_code == 201
    return r.json()["id"]


def get_category(session: Session, user_id: int, name: str) -> int:
    cat = session.exec(
        select(Category).where(Category.user_id == user_id, Category.name == name)
    ).first()
    if cat is None:
        cat = Category(user_id=user_id, name=name)
        session.add(cat)
        session.commit()
        session.refresh(cat)
    return cat.id


def seed_transaction(
    session: Session,
    user_id: int,
    account_id: int,
    payee: str,
    amount: str = "-10.00",
    day: date = date(2026, 8, 1),
    category_id: int | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        account_id=account_id,
        date=day,
        posting_date=day,
        raw_description=payee,
        normalized_payee=payee,
        amount_native=Decimal(amount),
        currency="CAD",
        dedup_hash=hashlib.sha256(f"{payee}-{day}-{amount}".encode()).hexdigest(),
        category_id=category_id,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn
