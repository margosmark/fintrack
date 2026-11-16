"""Net-worth history contract tests (docs/12 §4, w6-contract pin 5).

Hand-calculated monthly buckets: cumulative committed cash per account
converted at the newest cached rate ≤ bucket end, positions valued at the
newest price ≤ bucket end (manual wins, earliest-price fallback) for ALL
account types including registered plans.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_tax_acb import _user, add_trade, make_account, make_security

from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.fx_rates import FxRate, FxSource
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.prices import Price, PriceSource
from app.models.trades import TradeAction as TA
from app.models.transactions import Transaction
from app.models.users import User

D = Decimal

RANGE = {"from": "2025-01-01", "to": "2025-03-31"}

# Hand-computed expectations:
# Jan cash: 5000 - 750 + 3000 + 990 USDx1.20(=1188)          = 8438.00
# Jan positions: RY 100x118(yfinance Jan 1) + XTU 50x50(fallback) = 14300.00
# Feb cash: 3800 - 750 + 3000 + 1188                          = 7238.00
# Feb positions: RY 100x125(manual Feb 1) + XTU 2500          = 15000.00
# Mar cash: 6050 + 990 USDx1.40(=1386)                        = 7436.00


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _seed(session: Session) -> User:
    user = _user(session)
    chequing = make_account(session, user, "Chequing", AccountType.chequing, taxable=False)
    cc = make_account(session, user, "Card", AccountType.credit_card, taxable=False)
    rrsp = make_account(session, user, "RRSP", AccountType.rrsp, taxable=False)
    tfsa = make_account(session, user, "TFSA", AccountType.tfsa, taxable=False)
    usd = Account(user_id=user.id, name="USD Savings", type=AccountType.savings, currency="USD")
    session.add(usd)
    session.commit()
    session.refresh(usd)
    brokerage = make_account(
        session, user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
    )

    def batch(account: Account, status: ImportBatchStatus = ImportBatchStatus.committed):
        b = ImportBatch(
            user_id=user.id,
            account_id=account.id,
            institution="T",
            file_name="f.csv",
            status=status,
        )
        session.add(b)
        session.commit()
        session.refresh(b)
        return b

    def txn(account: Account, b: ImportBatch, day: str, amount: str):
        session.add(
            Transaction(
                user_id=user.id,
                account_id=account.id,
                date=date.fromisoformat(day),
                raw_description="r",
                normalized_payee="p",
                amount_native=D(amount),
                currency=account.currency,
                cad_amount=None,
                import_batch_id=b.id,
                dedup_hash=f"{account.id}-{day}-{amount}",
            )
        )
        session.commit()

    txn(chequing, batch(chequing), "2025-01-05", "5000.00")
    txn(cc, batch(cc), "2025-01-25", "-750.00")
    txn(rrsp, batch(rrsp), "2025-01-10", "3000.00")
    txn(usd, batch(usd), "2025-01-20", "1000.00")
    # cad_amount unresolved → invisible to spending/cashflow but NOT to the
    # native-currency cash sum used here; also proves pending rows don't count.
    txn(usd, batch(usd), "2025-01-10", "-10.00")
    txn(chequing, batch(chequing, ImportBatchStatus.pending_review), "2025-02-01", "99999.00")
    txn(chequing, batch(chequing), "2025-02-10", "-1200.00")

    ry = make_security(session, user, "RY", "CAD")
    xtu = make_security(session, user, "XTU", "CAD")
    add_trade(session, user, brokerage, ry, TA.buy, "2025-01-15", "100", "120.00")
    add_trade(session, user, tfsa, xtu, TA.buy, "2024-12-01", "50", "40.00")

    session.add(
        Price(
            user_id=user.id,
            security_id=ry.id,
            date=date(2025, 1, 1),
            price=D("118"),
            source=PriceSource.yfinance,
        )
    )
    session.add(
        Price(
            user_id=user.id,
            security_id=ry.id,
            date=date(2025, 2, 1),
            price=D("125"),
            source=PriceSource.manual,
        )
    )
    session.add(
        Price(
            user_id=user.id,
            security_id=xtu.id,
            date=date(2024, 6, 1),
            price=D("50"),
            source=PriceSource.manual,
        )
    )
    session.add(
        FxRate(
            base="USD", quote="CAD", date=date(2025, 1, 1), rate=D("1.20"), source=FxSource.manual
        )
    )
    session.add(
        FxRate(
            base="USD", quote="CAD", date=date(2025, 3, 1), rate=D("1.40"), source=FxSource.manual
        )
    )
    session.commit()
    return user


def test_net_worth_monthly_buckets_exact(client: TestClient, session: Session) -> None:
    user = _seed(session)
    body = client.get("/api/v1/reports/net-worth", params=RANGE, headers=_headers(user)).json()

    assert (body["from"], body["to"], body["granularity"]) == ("2025-01-01", "2025-03-31", "month")
    rows = [
        (b["bucket_end"], D(str(b["cash_cad"])), D(str(b["positions_cad"])), D(str(b["net_cad"])))
        for b in body["buckets"]
    ]
    assert rows == [
        ("2025-01-31", D("8438.00"), D("14300.00"), D("22738.00")),
        ("2025-02-28", D("7238.00"), D("15000.00"), D("22238.00")),
        ("2025-03-31", D("7436.00"), D("15000.00"), D("22436.00")),
    ]


def test_net_worth_registered_positions_count(client: TestClient, session: Session) -> None:
    user = _seed(session)
    body = client.get("/api/v1/reports/net-worth", params=RANGE, headers=_headers(user)).json()
    jan = body["buckets"][0]
    # XTU sits in a TFSA (registered) and contributes 50 x 50 = 2500.
    assert D(str(jan["positions_cad"])) == D("14300.00")


def test_net_worth_csv_header_pinned(client: TestClient, session: Session) -> None:
    user = _seed(session)
    resp = client.get(
        "/api/v1/reports/net-worth", params={**RANGE, "format": "csv"}, headers=_headers(user)
    )
    assert resp.text.splitlines()[0] == "bucket_end,cash_cad,positions_cad,net_cad"


def test_net_worth_assets_liabilities_split(client: TestClient, session: Session) -> None:
    """M97 (docs/12 §4): the tooltip's A and L are the SAME signed sums.

    Every account contribution lands in assets or liabilities by sign — the
    credit card's -750 is the only liability in this seed — and positions ride
    the asset side, so `assets - liabilities` re-subtracts to `net_cad`.
    """
    user = _seed(session)
    body = client.get("/api/v1/reports/net-worth", params=RANGE, headers=_headers(user)).json()
    rows = [
        (
            b["bucket_end"],
            D(str(b["assets_cad"])),
            D(str(b["liabilities_cad"])),
            D(str(b["net_cad"])),
        )
        for b in body["buckets"]
    ]
    assert rows == [
        ("2025-01-31", D("23488.00"), D("750.00"), D("22738.00")),
        ("2025-02-28", D("22988.00"), D("750.00"), D("22238.00")),
        ("2025-03-31", D("23186.00"), D("750.00"), D("22436.00")),
    ]
    for _, assets, liabilities, net in rows:
        assert assets - liabilities == net
