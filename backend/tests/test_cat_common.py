"""Shared helpers + fake LLM provider for the categorization test suite.

Never touches the network: the pipeline's provider seam is monkeypatched.
"""

import contextlib
import hashlib
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app import services
from app.llm.base import ResolvedLlm
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.transactions import Transaction

EMAIL = "cat@example.com"
PASSWORD = "correct-horse-battery"


def register_and_login(client: TestClient) -> dict:
    """Register a fresh user; returns the token body {access_token, ...}."""
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_account(client: TestClient, headers: dict, name: str = "Chequing") -> int:
    response = client.post(
        "/api/v1/accounts",
        json={"name": name, "type": "chequing", "currency": "CAD"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def category_by_name(session: Session, name: str, user_id: int) -> Category:
    """This tenant's category — the filter is REQUIRED, not optional (M178).

    `Category.name` is not unique, and the suite database deliberately carries a
    foreign tenant's 10 default categories (conftest `_decoy_tenant`), so the
    unfiltered version of this helper was `MultipleResultsFound` the moment any
    module leaked a registered tenant — which is what made
    `test_ynab_migration`'s seed snapshot red on 2 of ~8 full runs and never
    solo (t_a451f171, t_17d025b2, t_70d46e5e).
    """
    return session.exec(
        select(Category).where(Category.user_id == user_id, Category.name == name)
    ).one()


def make_transaction(
    session: Session,
    user_id: int,
    account_id: int,
    payee: str,
    amount: str = "-10.00",
    raw: str | None = None,
    day: date = date(2026, 8, 1),
    **overrides: Any,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        account_id=account_id,
        date=day,
        raw_description=raw if raw is not None else payee,
        normalized_payee=payee,
        amount_native=Decimal(amount),
        currency="CAD",
        dedup_hash=hashlib.sha256(f"{payee}-{day}-{amount}".encode()).hexdigest(),
        **overrides,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


class FakeProvider:
    """Scripted in-memory provider; records every call (messages, schema)."""

    def __init__(self, responder: Any = None) -> None:
        self.calls: list[tuple[list[dict], dict]] = []
        self.responder = responder or (lambda messages, schema: {"suggestions": []})

    def chat(self, messages: list[dict], json_schema: dict) -> dict:
        self.calls.append((messages, json_schema))
        return self.responder(messages, json_schema)


@pytest.fixture(name="fake_provider")
def fake_provider_fixture(monkeypatch):
    """Inject a FakeProvider into the pipeline (no network, no Ollama)."""
    provider = FakeProvider()
    monkeypatch.setattr(
        services.categorization,
        "build_provider",
        lambda user, settings: ResolvedLlm(provider, "fake-model"),
    )
    return provider


@pytest.fixture(name="background_session")
def background_session_fixture(monkeypatch, session: Session):
    """Background layer-3 work reuses the request-scoped test session.

    `_new_session` is used as a context manager by the worker; yielding the
    shared session from a @contextmanager keeps it open for assertions.
    """

    @contextlib.contextmanager
    def _shared_session():
        yield session

    monkeypatch.setattr(services.categorization, "_new_session", _shared_session)
    return session


def autopilot_account(session: Session, user_id: int, enabled: bool) -> Account:
    account = Account(
        user_id=user_id,
        name="Auto",
        type=AccountType.chequing,
        currency="CAD",
        llm_autopilot=enabled,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account
