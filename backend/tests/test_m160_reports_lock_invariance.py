"""M160 (P2-5) — the report routes are lock-invariant now that the mirrors are gone.

docs/33 §Hardening audit rows A4-A9, A14 and A15 used to be proven by
`frontend/tests/lockedVaultDegradation.test.ts` computing every report engine
TWICE — locked, then unlocked — and byte-comparing against the frozen golden.
M160 (M158 Decision 2) deleted those engines, so those halves retired with
them: a server replay cannot reproduce a client lock state (M160-R1 ruling 5).

What replaces them is the property the rows actually claim. A report number
cannot move with the vault because

* the browser no longer computes it at all — the route is the only
  implementation (docs/33 §Server-first rule 5), so there is no client half
  left to be vault-aware; and
* the route reads the mandatory **plaintext** money columns
  (docs/33 §Server-readable plaintext), never a cipher column, while
  `encryption_required` is a WRITE guard that branches no read path.

So the proof is the M134 P1-5 pattern used by every other row in the table
(`test_budget_progress_is_indifferent_to_encryption_required`,
`test_household_answers_are_indifferent_to_encryption_required`, …): seed one
tenant, flip `encryption_required` through `PATCH /users/me`, and assert the
route's own JSON is byte-identical under each value — plus a non-degeneracy
anchor, because an empty answer compared with itself proves nothing.

Surfaces, one test each, so `scripts/locked_vault_gate.py` can cite them
row-by-row:

  A4-A6  GET /reports/spending, /reports/spending/categories, /reports/cashflow
  A7     GET /reports/net-worth
  A8     GET /reports/investment-performance
  A9     GET /reports/dividends        (M160-R2: the client half retired with
                                        the `lib/reportClient.ts` shim, so this
                                        row joined the table here)
  A14    GET /reports/capital-gains
  A15    GET /acb

Run: cd backend && uv run pytest tests/test_m160_reports_lock_invariance.py -q
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.accounts import AccountType
from app.models.prices import Price, PriceSource
from app.models.trades import TradeAction as TA
from app.models.transactions import Transaction
from app.models.users import User
from tests.test_deep_common import auth, create_account, get_category, register
from tests.test_tax_acb import add_trade, make_account, make_security

API = "/api/v1"
D = Decimal
# One calendar year that contains every seeded date: the 2025-03 ledger rows,
# the 2025-02 buy, the 2025-06 sell and the 2025-06-01 manual price.
RANGE = {"from": "2025-01-01", "to": "2025-12-31"}


def _txn(
    session: Session,
    user_id: int,
    account_id: int,
    payee: str,
    amount: str,
    day: date,
    category_id: int,
) -> Transaction:
    """A committed CAD ledger row WITH its `cad_amount` mirror.

    The mirror matters: reports exclude a row whose reporting-currency
    amount is unresolved (`test_reports_spending_cashflow.py` — "Unresolved
    CAD (cad_amount NULL) → excluded from every aggregate"), so a seed that
    left it NULL would give the lock-invariance comparison an empty answer on
    both sides and prove nothing.
    """
    row = Transaction(
        user_id=user_id,
        account_id=account_id,
        date=day,
        posting_date=day,
        raw_description=payee,
        normalized_payee=payee,
        amount_native=D(amount),
        currency="CAD",
        cad_amount=D(amount),
        category_id=category_id,
        dedup_hash=hashlib.sha256(f"{user_id}-{payee}-{day}-{amount}".encode()).hexdigest(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _flip(client: TestClient, headers: dict, value: bool) -> None:
    r = client.patch(f"{API}/users/me", headers=headers, json={"encryption_required": value})
    assert r.status_code == 200, r.text
    assert r.json()["encryption_required"] is value


def _get(client: TestClient, headers: dict, path: str, **params: str):
    r = client.get(f"{API}{path}", headers=headers, params=params)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text}"
    return r.json()


def _seed(client: TestClient, session: Session) -> dict:
    """One tenant carrying every shape the five rows ask about: a cash
    ledger (spending / cashflow / net-worth) and a taxed brokerage round trip
    (investment-performance / capital-gains / acb)."""
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    user = session.get(User, user_id)
    assert user is not None

    chequing = create_account(client, headers, "Chequing")
    groceries = get_category(session, user_id, "groceries")
    salary = get_category(session, user_id, "salary")
    # Outflow first so the spending denominator is non-zero whatever sign
    # convention the route applies to income booked in its own category.
    _txn(session, user_id, chequing, "No Frills", "-85.00", date(2025, 3, 4), groceries)
    _txn(session, user_id, chequing, "Payroll", "1500.00", date(2025, 3, 1), salary)

    brokerage = make_account(
        session, user, "Brokerage", AccountType.brokerage_non_registered, taxable=True
    )
    ry = make_security(session, user, "RY", "CAD")
    add_trade(session, user, brokerage, ry, TA.buy, "2025-02-10", "50", "120.00", "9.95")
    add_trade(session, user, brokerage, ry, TA.sell, "2025-06-10", "20", "140.00")
    # A9's income row: /reports/dividends aggregates income TRADES, so the seed
    # needs one or its locked/unlocked comparison would be empty on both sides.
    add_trade(session, user, brokerage, ry, TA.dividend, "2025-04-15", "0", "12.34")
    session.add(
        Price(
            user_id=user_id,
            security_id=ry.id,
            date=date(2025, 6, 1),
            price=D("141"),
            source=PriceSource.manual,
        )
    )
    session.commit()

    # Direct row writes bypass the API's ACB hook, so rebuild through the one
    # sanctioned write path (docs/02 invariant) — A15 reads those pools.
    rebuild = client.post(f"{API}/acb/rebuild", headers=headers, json={})
    assert rebuild.status_code == 200, rebuild.text
    return headers


@pytest.mark.parametrize("encryption_required", [False, True])
def test_spending_reports_are_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """A4-A6: spending, its category breakdown and cashflow answer the same
    numbers under either value of the write guard."""
    headers = _seed(client, session)

    _flip(client, headers, encryption_required)
    under = (
        _get(client, headers, "/reports/spending", granularity="month", **RANGE),
        _get(client, headers, "/reports/spending/categories", **RANGE),
        _get(client, headers, "/reports/cashflow", granularity="month", **RANGE),
    )
    _flip(client, headers, not encryption_required)
    other = (
        _get(client, headers, "/reports/spending", granularity="month", **RANGE),
        _get(client, headers, "/reports/spending/categories", **RANGE),
        _get(client, headers, "/reports/cashflow", granularity="month", **RANGE),
    )

    assert under == other, "the write guard must not move a report number"
    # Non-degenerate: an empty answer compared with itself proves nothing.
    assert D(under[0]["spend_total_cad"]) > 0, under[0]
    income = sum(D(b["income_cad"]) for b in under[2]["buckets"])
    assert income > 0, under[2]["buckets"]
    assert D(under[0]["spend_total_cad"]) < income, under[0]


@pytest.mark.parametrize("encryption_required", [False, True])
def test_net_worth_is_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """A7: net-worth buckets are cash + positions, both read from plaintext."""
    headers = _seed(client, session)

    _flip(client, headers, encryption_required)
    under = _get(client, headers, "/reports/net-worth", granularity="month", **RANGE)
    _flip(client, headers, not encryption_required)
    other = _get(client, headers, "/reports/net-worth", granularity="month", **RANGE)

    assert under == other, "the write guard must not move a net-worth bucket"
    last = under["buckets"][-1]
    assert D(last["net_cad"]) != 0, last
    assert D(last["assets_cad"]) > 0, last


@pytest.mark.parametrize("encryption_required", [False, True])
def test_investment_performance_is_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """A8: the ACB replay + market value read committed trades, cache-only
    prices and plaintext money — never the vault."""
    headers = _seed(client, session)

    _flip(client, headers, encryption_required)
    under = _get(client, headers, "/reports/investment-performance", **RANGE)
    _flip(client, headers, not encryption_required)
    other = _get(client, headers, "/reports/investment-performance", **RANGE)

    assert under == other, "the write guard must not move a performance number"
    assert len(under["items"]) >= 1, under["items"]
    assert D(under["totals"]["realized_cad"]) > 0, under["totals"]


@pytest.mark.parametrize("encryption_required", [False, True])
def test_dividends_are_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """A9: the dividends route reads income trades and their trade-date rates
    — mandatory plaintext / cache-only — so the write guard cannot move a
    number. M160-R2 (t_85311c34) retired the client half this row used to pin
    (`lib/reportClient.ts::dividendReport`, deleted with its shim)."""
    headers = _seed(client, session)

    _flip(client, headers, encryption_required)
    under = _get(client, headers, "/reports/dividends", year="2025")
    _flip(client, headers, not encryption_required)
    other = _get(client, headers, "/reports/dividends", year="2025")

    assert under == other, "the write guard must not move a dividend number"
    assert len(under["items"]) >= 1, under["items"]
    assert D(under["totals"]["total_cad"]) > 0, under["totals"]


@pytest.mark.parametrize("encryption_required", [False, True])
def test_capital_gains_is_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """A14: the annual gain summary is the server's single implementation
    (docs/06) over committed trades and cache-only fx."""
    headers = _seed(client, session)

    _flip(client, headers, encryption_required)
    under = _get(client, headers, "/reports/capital-gains", year="2025")
    _flip(client, headers, not encryption_required)
    other = _get(client, headers, "/reports/capital-gains", year="2025")

    assert under == other, "the write guard must not move a capital-gains number"
    assert len(under["items"]) >= 1, under["items"]
    assert D(under["totals"]["gain_loss_cad"]) > 0, under["totals"]


@pytest.mark.parametrize("encryption_required", [False, True])
def test_acb_pools_are_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """A15: `acb_pools` is a derived cache written only through
    `rebuild_user_pools`; the read is vault-independent."""
    headers = _seed(client, session)

    _flip(client, headers, encryption_required)
    under = _get(client, headers, "/acb", limit="200")
    _flip(client, headers, not encryption_required)
    other = _get(client, headers, "/acb", limit="200")

    assert under == other, "the write guard must not move an ACB pool"
    assert len(under["items"]) >= 1, under["items"]
    assert D(under["items"][0]["total_acb_cad"]) > 0, under["items"][0]
