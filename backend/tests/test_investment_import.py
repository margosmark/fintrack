"""Investment CSV import: tolerant parser + per-row preview/unmapped endpoint.

Covers the rework in docs/05 / trades import: the parser must never raise on a
trailing blank line or unknown activity type, and the endpoint returns valid
trades alongside a skipped-rows list instead of a whole-file 422/500.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.importers.investment_profile import parse_investment_csv
from app.models.accounts import Account
from app.models.import_batches import ImportBatch
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.services import market_data

API = "/api/v1"
FIXTURE = Path(__file__).parent / "fixtures" / "investment_sample.csv"


def _auth(client: TestClient) -> dict[str, str]:
    email = f"imp-{datetime.now().timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
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


def _upload(client: TestClient, headers: dict[str, str], account_id: int, path=FIXTURE):
    with open(path, "rb") as f:
        return client.post(
            f"{API}/trades/import",
            files={"file": ("investment_sample.csv", f, "text/csv")},
            data={"account_id": account_id},
            headers=headers,
        )


@pytest.fixture(autouse=True)
def _mock_symbol_lookup(monkeypatch):
    """Resolve CDN tickers via the reliable `.history` seam; no real yfinance."""
    _CDN_TO = {"VEQT.TO", "XEQT.TO", "ZGRO.TO"}

    def fake_lookup(symbol: str, **kwargs):
        # Simulate this environment: `.info` unavailable -> no exchange.
        return None

    def fake_close(symbol, today=None):
        return (date.today(), Decimal("62.49")) if symbol.strip().upper() in _CDN_TO else None

    monkeypatch.setattr(market_data, "lookup_symbol", fake_lookup)
    monkeypatch.setattr(market_data, "fetch_latest_close", fake_close)


def test_import_reuses_existing_security_for_lower_case_symbol(client, session, tmp_path) -> None:
    """M117: a CSV cell reading `veqt` must resolve to the existing `VEQT` row.

    The import used to match and store the raw cell while `POST /securities`
    uppercased its input, and `uq_securities_symbol` is
    `UNIQUE (user_id, symbol, exchange)` — so the two spellings did not collide
    and the holding silently ended up split across two securities.
    """
    headers = _auth(client)
    uid = client.get(f"{API}/users/me", headers=headers).json()["id"]
    existing = Security(
        user_id=uid,
        symbol="VEQT",
        name="Vanguard All-Equity ETF",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(existing)
    session.flush()
    existing_id = existing.id

    lowercase = tmp_path / "lowercase.csv"
    lowercase.write_text(FIXTURE.read_text().replace(",VEQT,", ",veqt,"))

    account = _account(client, headers, "brokerage_non_registered")
    resp = _upload(client, headers, account["id"], lowercase)
    assert resp.status_code == 201, resp.text

    rows = session.exec(
        select(Security).where(Security.user_id == uid, Security.symbol == "VEQT")
    ).all()
    assert [row.id for row in rows] == [existing_id]

    staged = session.exec(select(Trade).where(Trade.user_id == uid)).all()
    # The fixture also carries XEQT / ZGRO / a cash row — those legitimately
    # create their own securities. What matters is that BOTH `veqt` rows
    # landed on the pre-existing VEQT instead of minting a second one.
    assert existing_id in {trade.security_id for trade in staged}


def test_trade_import_resolves_security_exchange(client, session) -> None:
    """Imported CDN tickers get a resolved exchange/currency, not UNKNOWN."""
    headers = _auth(client)
    account = _account(client, headers, "brokerage_non_registered")
    resp = _upload(client, headers, account["id"])
    assert resp.status_code == 201, resp.text
    sec = session.exec(select(Security).where(Security.symbol == "VEQT")).first()
    assert sec is not None
    assert sec.exchange == "TSX"
    assert sec.listing_currency == "CAD"


# --- Parser (unit) ------------------------------------------------------------


def test_parse_investment_csv_tolerant():
    raw = FIXTURE.read_bytes()  # includes trailing blank line
    trades, unmapped = parse_investment_csv(raw)

    # 5 valid rows: Buy, Dividend, Grant/CESG, SecurityTransfer, Trade/BUY.
    assert len(trades) == 5
    actions = {t.action for t in trades}
    assert "buy" in actions
    assert "dividend" in actions
    assert "contribution" in actions  # Grant/CESG
    assert "transfer_in" in actions  # SecurityTransfer
    # Grant/CESG has no symbol -> cash-only sentinel path (qty=0, price=net_cash).
    grant = next(t for t in trades if t.action == "contribution")
    assert grant.symbol == ""
    assert grant.quantity == 0

    # Unknown activity "Foo/WEIRD" is reported, never raises.
    assert len(unmapped) == 1
    assert unmapped[0].activity_type == "Foo"
    assert unmapped[0].activity_sub_type == "WEIRD"
    assert "Unknown activity" in unmapped[0].reason


def test_parse_investment_csv_date_formats():
    csv = (
        "activity_type,activity_sub_type,symbol,name,currency,description,"
        "effective_date,quantity,unit_price,commission,net_cash_amount,account_id\n"
        "Buy,,VEQT,Vanguard,CAD,BUY,01/15/2024,1,10,0,10,acc-1\n"
    )
    trades, unmapped = parse_investment_csv(csv.encode())
    assert len(trades) == 1
    assert trades[0].date == date(2024, 1, 15)
    assert unmapped == []


def test_parse_investment_csv_unparseable_date_unmapped():
    csv = (
        "activity_type,activity_sub_type,symbol,name,currency,description,"
        "effective_date,quantity,unit_price,commission,net_cash_amount,account_id\n"
        "Buy,,VEQT,Vanguard,CAD,BUY,not-a-date,1,10,0,10,acc-1\n"
    )
    trades, unmapped = parse_investment_csv(csv.encode())
    assert trades == []
    assert len(unmapped) == 1
    assert "Unparseable date" in unmapped[0].reason


def test_parse_investment_csv_non_cash_missing_symbol_unmapped():
    csv = (
        "activity_type,activity_sub_type,symbol,name,currency,description,"
        "effective_date,quantity,unit_price,commission,net_cash_amount,account_id\n"
        "Buy,,,Vanguard,CAD,BUY,2024-01-15,1,10,0,10,acc-1\n"
    )
    trades, unmapped = parse_investment_csv(csv.encode())
    assert trades == []
    assert len(unmapped) == 1
    assert "Missing symbol" in unmapped[0].reason


def test_parse_security_transfer_yields_signed_quantity():
    """SecurityTransfer is a share transfer: non-zero quantity, per-share cost."""
    csv = (
        "activity_type,activity_sub_type,symbol,name,currency,description,effective_date,"
        "quantity,unit_price,commission,net_cash_amount,account_id\n"
        "SecurityTransfer,,XEQT,iShares,CAD,TRANSFER IN,2024-04-15,5,25.00,0,125.00,acc-1\n"
    )
    trades, unmapped = parse_investment_csv(csv.encode())
    assert unmapped == []
    assert len(trades) == 1
    t = trades[0]
    assert t.action == "transfer_in"
    assert t.symbol == "XEQT"
    assert t.quantity == Decimal("5")  # positive (transfer in)
    assert t.price_native == Decimal("25.00")  # per-share cost basis
    assert t.commission == Decimal("0")


def test_parse_transfer_out_negative_quantity():
    """transfer_out keeps a negative signed quantity (share disposition)."""
    csv = (
        "activity_type,activity_sub_type,symbol,name,currency,description,effective_date,"
        "quantity,unit_price,commission,net_cash_amount,account_id\n"
        "SecurityTransfer,OUT,VTI,Vanguard,USD,TRANSFER OUT,2024-04-15,3,40.00,0,120.00,acc-1\n"
    )
    trades, unmapped = parse_investment_csv(
        csv.encode(),
        activity_map={("SecurityTransfer", "OUT"): TradeAction.transfer_out},
    )
    assert unmapped == []
    assert trades[0].action == "transfer_out"
    assert trades[0].quantity == Decimal("-3")
    assert trades[0].price_native == Decimal("40.00")


def test_parse_transfer_blank_symbol_unmapped():
    """A share transfer with no symbol cannot build a Security → skipped."""
    csv = (
        "activity_type,activity_sub_type,symbol,name,currency,description,effective_date,"
        "quantity,unit_price,commission,net_cash_amount,account_id\n"
        "SecurityTransfer,,,XName,CAD,TRANSFER IN,2024-04-15,5,25.00,0,125.00,acc-1\n"
    )
    trades, unmapped = parse_investment_csv(csv.encode())
    assert trades == []
    assert len(unmapped) == 1
    assert unmapped[0].activity_type == "SecurityTransfer"
    assert "Missing symbol" in unmapped[0].reason


# --- Endpoint (integration) ---------------------------------------------------


def test_trade_import_endpoint(client: TestClient, session):
    headers = _auth(client)
    account = _account(client, headers, "brokerage_non_registered")

    resp = _upload(client, headers, account["id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["imported"] == 5
    assert body["duplicates_skipped"] == 0
    assert len(body["unmapped"]) == 1
    assert body["unmapped"][0]["activity_type"] == "Foo"
    # ImportBatch persisted with the investment_csv institution marker.
    batch = session.exec(select(ImportBatch).where(ImportBatch.id == body["batch_id"])).one()
    assert batch.institution == "investment_csv"

    # Securities auto-created for each distinct symbol + the CASH sentinel.
    acc = session.get(Account, account["id"])
    symbols = session.exec(select(Security.symbol).where(Security.user_id == acc.user_id)).all()
    assert set(symbols) >= {"VEQT", "XEQT", "ZGRO", "CASH"}


def test_trade_import_reimport_duplicates(client: TestClient):
    headers = _auth(client)
    account = _account(client, headers, "brokerage_non_registered")

    first = _upload(client, headers, account["id"])
    assert first.status_code == 201, first.text
    second = _upload(client, headers, account["id"])
    assert second.status_code == 201, second.text
    assert second.json()["duplicates_skipped"] > 0


def test_trade_import_commit(client: TestClient):
    headers = _auth(client)
    account = _account(client, headers, "brokerage_non_registered")

    resp = _upload(client, headers, account["id"])
    assert resp.status_code == 201, resp.text
    batch_id = resp.json()["batch_id"]

    commit = client.post(f"{API}/trades/import/{batch_id}/commit", headers=headers)
    assert commit.status_code == 200, commit.text
    assert commit.json()["status"] == "committed"

    # Trades are now live and queryable; ACB pools rebuilt without error.
    trades = client.get(
        f"{API}/trades?account_id={account['id']}&limit=200&offset=0", headers=headers
    )
    assert trades.status_code == 200, trades.text
    assert trades.json()["total"] >= 5


def test_trade_import_non_investment_account_rejected(client: TestClient):
    headers = _auth(client)
    account = _account(client, headers, "chequing")
    resp = _upload(client, headers, account["id"])
    assert resp.status_code == 422


def test_trade_import_commit_can_force_include_duplicate(client: TestClient, session):
    headers = _auth(client)
    account = _account(client, headers, "brokerage_non_registered")

    first = _upload(client, headers, account["id"])
    assert first.status_code == 201, first.text
    second = _upload(client, headers, account["id"])
    assert second.status_code == 201, second.text
    body = second.json()
    dup_rows = [r for r in body["preview"] if r["duplicate"]]
    assert dup_rows, "expected staged duplicates to include ids"
    force_id = dup_rows[0]["id"]

    commit = client.post(
        f"{API}/trades/import/{body['batch_id']}/commit",
        headers=headers,
        json={"include_ids": [force_id]},
    )
    assert commit.status_code == 200, commit.text
    cj = commit.json()
    assert cj["duplicates_skipped"] == body["duplicates_skipped"] - 1
    assert cj["imported"] == 1

    forced = session.get(Trade, force_id)
    assert forced is not None and forced.is_duplicate and forced.force_include
    assert ":" in forced.dedup_hash  # salted, distinct from canonical
