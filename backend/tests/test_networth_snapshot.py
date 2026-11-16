"""Net worth snapshot endpoint."""

import hashlib
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.fx_rates import FxRate, FxSource
from app.models.transactions import Transaction


def _auth(client: TestClient) -> dict[str, str]:
    from datetime import UTC, datetime

    email = f"nws-{datetime.now(UTC).timestamp()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _as_currency(session: Session, account_id: int, currency: str) -> None:
    """Flip an account's currency directly in the session.

    Creating a foreign-currency account through the API makes the response
    serializer call `fx.latest_rate(..., allow_fetch=True)`, which performs a
    real Bank of Canada fetch and writes rows into the SHARED `fx_rates` table
    (docs/07) — those rows would out-rank the deterministic rate this module
    seeds. Setting the column here keeps the test hermetic.
    """
    from app.models.accounts import Account

    account = session.get(Account, account_id)
    account.currency = currency
    session.add(account)
    session.commit()


def _register_user(client: TestClient) -> tuple[dict, int]:
    headers = _auth(client)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    return headers, user_id


def _create_account(client: TestClient, headers: dict, name: str) -> int:
    r = client.post(
        "/api/v1/accounts",
        json={"name": name, "type": "chequing", "currency": "CAD"},
        headers=headers,
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_snapshot_empty_for_new_user(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/reports/networth-snapshot?target_date=2026-01-01", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["accounts"] == []
    assert float(body["total_cash"]) == 0
    assert body["date"] == "2026-01-01"


def test_snapshot_response_shape(client: TestClient) -> None:
    headers = _auth(client)
    r = client.get("/api/v1/reports/networth-snapshot?target_date=2026-06-15", headers=headers)
    body = r.json()
    assert "accounts" in body
    assert "total_cash" in body
    assert "date" in body
    assert isinstance(body["accounts"], list)


def test_snapshot_requires_auth(client: TestClient) -> None:
    r = client.get("/api/v1/reports/networth-snapshot?target_date=2026-01-01")
    assert r.status_code == 401


def test_snapshot_includes_null_posting_date_rows(client: TestClient, session) -> None:
    """Manual rows (posting_date=None) count; empty accounts list at zero.

    Regression: the snapshot filtered on posting_date, which excluded every
    null-posting row AND turned the outer join into an inner one — funded
    accounts vanished entirely (demo returned accounts=[]).
    """
    import hashlib
    from datetime import date
    from decimal import Decimal

    from app.models.transactions import Transaction

    headers = _auth(client)
    r = client.post(
        "/api/v1/accounts",
        json={"name": "Chequing", "type": "chequing", "currency": "CAD"},
        headers=headers,
    )
    assert r.status_code == 201
    funded_id = r.json()["id"]
    r = client.post(
        "/api/v1/accounts",
        json={"name": "Empty", "type": "savings", "currency": "CAD"},
        headers=headers,
    )
    assert r.status_code == 201

    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    session.add(
        Transaction(
            user_id=user_id,
            account_id=funded_id,
            date=date(2026, 9, 1),
            posting_date=None,
            raw_description="PAYROLL ACME CORP",
            normalized_payee="PAYROLL ACME CORP",
            amount_native=Decimal("2400.00"),
            currency="CAD",
            dedup_hash=hashlib.sha256(b"payroll-2026-09-01").hexdigest(),
        )
    )
    session.commit()

    r = client.get("/api/v1/reports/networth-snapshot?target_date=2026-09-03", headers=headers)
    assert r.status_code == 200
    by_name = {a["account_name"]: a for a in r.json()["accounts"]}
    assert float(by_name["Chequing"]["balance"]) == 2400.00
    assert float(by_name["Empty"]["balance"]) == 0
    assert float(r.json()["total_cash"]) == 2400.00


# ------------------------------------------- mixed-currency total regression


def test_snapshot_total_cash_converts_foreign_accounts_to_reporting_currency(
    client: TestClient, session: Session
) -> None:
    """`total_cash` sums in CAD, converting foreign accounts via the fx cache.

    Regression: foreign balances were added at face value, so a USD account
    inflated the CAD total 1:1.
    """
    headers, user_id = _register_user(client)
    cad_id = _create_account(client, headers, "Chequing")
    usd_id = _create_account(client, headers, "USD Brokerage")
    _as_currency(session, usd_id, "USD")

    day = date(2026, 9, 1)
    session.add(
        FxRate(base="USD", quote="CAD", rate=Decimal("1.35"), date=day, source=FxSource.manual)
    )
    session.commit()

    for acct, amount, cur in ((cad_id, "1000.00", "CAD"), (usd_id, "200.00", "USD")):
        session.add(
            Transaction(
                user_id=user_id,
                account_id=acct,
                date=day,
                posting_date=day,
                raw_description="BALANCE",
                normalized_payee="BALANCE",
                amount_native=Decimal(amount),
                currency=cur,
                dedup_hash=hashlib.sha256(f"bal-{acct}".encode()).hexdigest(),
            )
        )
    session.commit()

    body = client.get(
        "/api/v1/reports/networth-snapshot?target_date=2026-09-30", headers=headers
    ).json()
    by_name = {a["account_name"]: a for a in body["accounts"]}
    # Per-account balance stays in its NATIVE currency (display copy).
    assert float(by_name["USD Brokerage"]["balance"]) == 200.00
    assert float(by_name["Chequing"]["balance"]) == 1000.00
    # ...but the total is CAD: 1000 + 200 x 1.35 = 1270.
    assert body["reporting_currency"] == "CAD"
    assert float(body["total_cash"]) == 1270.00


def test_snapshot_excludes_accounts_with_no_resolvable_rate_from_total(
    client: TestClient, session: Session
) -> None:
    """An unconvertible foreign account is excluded from the total, not added raw."""
    headers, user_id = _register_user(client)
    cad_id = _create_account(client, headers, "Chequing")
    eur_id = _create_account(client, headers, "EUR Account")
    _as_currency(session, eur_id, "EUR")

    day = date(2026, 9, 1)
    for acct, amount, cur in ((cad_id, "1000.00", "CAD"), (eur_id, "500.00", "EUR")):
        session.add(
            Transaction(
                user_id=user_id,
                account_id=acct,
                date=day,
                posting_date=day,
                raw_description="BALANCE",
                normalized_payee="BALANCE",
                amount_native=Decimal(amount),
                currency=cur,
                dedup_hash=hashlib.sha256(f"eur-bal-{acct}".encode()).hexdigest(),
            )
        )
    session.commit()

    body = client.get(
        "/api/v1/reports/networth-snapshot?target_date=2026-09-30", headers=headers
    ).json()
    by_name = {a["account_name"]: a for a in body["accounts"]}
    assert float(by_name["EUR Account"]["balance"]) == 500.00  # still listed
    assert float(body["total_cash"]) == 1000.00  # excluded from the CAD total


def test_snapshot_total_cash_uses_rate_newest_on_or_before_target_date(
    client: TestClient, session: Session
) -> None:
    """Conversion uses the newest cached rate on or before the snapshot date."""
    headers, user_id = _register_user(client)
    usd_id = _create_account(client, headers, "USD")
    _as_currency(session, usd_id, "USD")

    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            rate=Decimal("1.20"),
            date=date(2026, 8, 1),
            source=FxSource.manual,
        )
    )
    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            rate=Decimal("1.40"),
            date=date(2026, 9, 20),
            source=FxSource.manual,
        )
    )
    session.add(
        Transaction(
            user_id=user_id,
            account_id=usd_id,
            date=date(2026, 9, 1),
            posting_date=date(2026, 9, 1),
            raw_description="BALANCE",
            normalized_payee="BALANCE",
            amount_native=Decimal("100.00"),
            currency="USD",
            dedup_hash=hashlib.sha256(b"nws-usd").hexdigest(),
        )
    )
    session.commit()

    early = client.get(
        "/api/v1/reports/networth-snapshot?target_date=2026-09-10", headers=headers
    ).json()
    assert float(early["total_cash"]) == 120.00  # 100 x 1.20

    late = client.get(
        "/api/v1/reports/networth-snapshot?target_date=2026-09-30", headers=headers
    ).json()
    assert float(late["total_cash"]) == 140.00  # 100 x 1.40
