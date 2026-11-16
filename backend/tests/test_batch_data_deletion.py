"""Batch-data deletion sweep: DISCARD/REVERT must take the batch's TRADES too (docs/03).

The reported defect (M112): `POST /imports/{batch_id}/revert` deleted only the
batch's `transactions`, so reverting a **trade** batch — the investment-CSV /
brokerage-profile path, which stages into `trades` with the same
`import_batch_id` (docs/03 §"The same staging rule governs trades") — left
every staged trade still linked to the now-`reverted` batch. Prod evidence:
batch 176 `reverted` keeps 71 trade rows, batch 177 `discarded` keeps 70.
`services/trade_filters.committed_trade_conditions` correctly excludes them
from every LIVE calculation, so positions looked right while the data the user
asked to take back was still sitting in `trades` and reachable from the trades
surfaces.

These tests pin the whole batch-data deletion sweep (AGENTS §9, one test per
path):

- DISCARD / REVERT take the batch's transactions AND trades (M112 fix).
- REVERT is position- and pool-neutral: it must return the account to its
  pre-import state, which means the ACB cache is rebuilt with it (docs/02
  invariant — `acb_pools` is derived state of `trades`).
- REVERT unlinks the transaction children `_purge_transaction` unlinks
  (docs/29 F3): tag links, LLM suggestions, reconciliation matches. Those FKs
  are NOT `ON DELETE CASCADE`, so skipping them is an IntegrityError → 500,
  not a silent orphan.
- The terminal-batch repair (`services.batch_hygiene`) removes pre-fix orphans
  WITHOUT moving positions or pools — the rows it deletes were already out of
  scope by status.

Account deletion is covered by `test_user_account_management::
test_delete_account_cascade`, which seeds a `Trade` and asserts every row of
`_USER_OWNED_MODELS` is gone for the deleted user.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.llm_suggestions import LlmSuggestion, SuggestionStatus
from app.models.prices import Price, PriceSource
from app.models.reconciliation import ReconciliationItem
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.models.transactions import Transaction
from app.services import valuation
from app.services.batch_hygiene import sweep_terminal_batch_trades
from app.tax.acb import rebuild_user_pools

API = "/api/v1"
FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "correct-horse-battery"


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _register(client: TestClient) -> dict:
    """A fresh tenant per test — module-level emails would share one tenant
    across files and make these assertions depend on test order."""
    from datetime import UTC, datetime

    email = f"batchdel-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    body = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return {"headers": headers, "user_id": user_id}


def _account(session: Session, user_id: int, type_: str = "brokerage_non_registered") -> Account:
    account = Account(
        user_id=user_id,
        name=f"Acct {type_} {user_id}",
        type=AccountType(type_),
        currency="CAD",
        taxable=type_ == "brokerage_non_registered",
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(session: Session, user_id: int, symbol: str) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Inc",
        kind="stock",
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _price(session: Session, user_id: int, security: Security, day: dt.date, price: str) -> None:
    session.add(
        Price(
            user_id=user_id,
            security_id=security.id,
            date=day,
            price=Decimal(price),
            source=PriceSource.manual,
        )
    )
    session.commit()


def _batch(session: Session, user_id: int, account_id: int, status: str) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="investment_csv",
        file_name="staged.csv",
        status=ImportBatchStatus(status),
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _trade(
    session: Session,
    user_id: int,
    account: Account,
    security: Security,
    action: str,
    *,
    batch_id: int | None,
    quantity: str = "0",
    price_native: str = "0",
) -> Trade:
    trade = Trade(
        user_id=user_id,
        account_id=account.id,
        security_id=security.id,
        action=TradeAction(action),
        date=_today(),
        quantity=Decimal(quantity),
        price_native=Decimal(price_native),
        commission=Decimal("0"),
        import_batch_id=batch_id,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _txn(
    session: Session,
    user_id: int,
    account: Account,
    *,
    batch_id: int,
    amount: str = "-42.50",
    dedup: str | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=dt.date(2026, 1, 5),
        raw_description="GROCERY MART #12",
        normalized_payee="grocery mart #12",
        amount_native=Decimal(amount),
        currency="CAD",
        dedup_hash=dedup or f"batchdel-{batch_id}-{amount}",
        import_batch_id=batch_id,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _positions(session: Session, user_id: int) -> list[dict]:
    return valuation.compute_positions(session, user_id, today=_today())


def _pools(session: Session, user_id: int) -> list[dict]:
    return [
        {
            "account_id": p.account_id,
            "security_id": p.security_id,
            "quantity": str(p.quantity),
            "total_acb_cad": str(p.total_acb_cad),
        }
        for p in session.exec(select(AcbPool).where(AcbPool.user_id == user_id)).all()
    ]


def _batch_trades(session: Session, batch_id: int) -> list[Trade]:
    return session.exec(select(Trade).where(Trade.import_batch_id == batch_id)).all()


# ---------------------------------------------------------------------------
# DISCARD / REVERT take the batch's trades (the M112 defect)
# ---------------------------------------------------------------------------


def test_revert_committed_trade_batch_deletes_trades(client: TestClient, session: Session) -> None:
    """The reported bug: revert a committed TRADE batch → zero trades remain.

    Today's test in `test_imports` asserts `transactions == 0` and passes
    while all 71 of the batch's trades stay linked — that is the exact gap.
    """
    auth = _register(client)
    headers, user_id = auth["headers"], auth["user_id"]
    account = _account(session, user_id, "tfsa")
    security = _security(session, user_id, "ABCD")
    batch = _batch(session, user_id, account.id, "pending_review")
    for _ in range(3):
        _trade(
            session,
            user_id,
            account,
            security,
            "buy",
            batch_id=batch.id,
            quantity="10",
            price_native="20.00",
        )
    assert len(_batch_trades(session, batch.id)) == 3

    committed = client.post(f"{API}/trades/import/{batch.id}/commit", headers=headers)
    assert committed.status_code == 200, committed.text
    assert committed.json()["status"] == "committed"

    resp = client.post(f"{API}/imports/{batch.id}/revert", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reverted"

    session.expire_all()
    assert _batch_trades(session, batch.id) == [], (
        "revert deleted the batch's transactions but left its trades linked"
    )


def test_discard_pending_trade_batch_deletes_trades(client: TestClient, session: Session) -> None:
    """Same leg on DISCARD: a pending trade batch discarded through the shared
    `POST /imports/{id}/discard` must not keep its staged trades."""
    auth = _register(client)
    headers, user_id = auth["headers"], auth["user_id"]
    account = _account(session, user_id, "tfsa")
    security = _security(session, user_id, "EFGH")
    batch = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        security,
        "buy",
        batch_id=batch.id,
        quantity="5",
        price_native="10.00",
    )
    assert len(_batch_trades(session, batch.id)) == 1

    resp = client.post(f"{API}/imports/{batch.id}/discard", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "discarded"

    session.expire_all()
    assert _batch_trades(session, batch.id) == []


# ---------------------------------------------------------------------------
# REVERT must return the account to its pre-import state (positions + pools)
# ---------------------------------------------------------------------------


def test_revert_restores_pre_import_positions_and_acb_pools(
    client: TestClient, session: Session
) -> None:
    """Byte sanity on both surfaces: positions AND the `acb_pools` cache go
    back to exactly what they were before the import.

    `acb_pools` is derived state of `trades` (docs/02 invariant) and the
    trades write path rebuilds it — but the batch paths live in
    `app/api/imports.py`, so a revert that deletes trades without rebuilding
    leaves the cache holding the pre-revert numbers. For a non-registered
    account `valuation` reads that cache for `book_cost_cad` (docs/06), i.e.
    the user SEES the stale cost basis.
    """
    auth = _register(client)
    headers, user_id = auth["headers"], auth["user_id"]
    account = _account(session, user_id)  # non-registered → pools are read
    security = _security(session, user_id, "ABCD")
    _price(session, user_id, security, _today(), "20.00")

    # A manual baseline position so the comparison is non-trivial.
    _trade(
        session,
        user_id,
        account,
        security,
        "buy",
        batch_id=None,
        quantity="10",
        price_native="20.00",
    )
    rebuild_user_pools(session, user_id)
    session.commit()
    baseline_positions = _positions(session, user_id)
    baseline_pools = _pools(session, user_id)
    assert baseline_positions and baseline_pools

    # Import a batch, commit it (the trades write path rebuilds pools).
    batch = _batch(session, user_id, account.id, "pending_review")
    _trade(
        session,
        user_id,
        account,
        security,
        "buy",
        batch_id=batch.id,
        quantity="100",
        price_native="20.00",
    )
    committed = client.post(f"{API}/trades/import/{batch.id}/commit", headers=headers)
    assert committed.status_code == 200, committed.text
    session.expire_all()
    assert _positions(session, user_id)[0]["quantity"] == Decimal("110.000000")

    # Revert → back to the baseline on BOTH surfaces.
    reverted = client.post(f"{API}/imports/{batch.id}/revert", headers=headers)
    assert reverted.status_code == 200, reverted.text
    session.expire_all()
    assert _positions(session, user_id) == baseline_positions, (
        "positions after revert differ from the pre-import state"
    )
    assert _pools(session, user_id) == baseline_pools, (
        "acb_pools after revert differ from the pre-import state — the cache "
        "was not rebuilt when the batch's trades were deleted"
    )


# ---------------------------------------------------------------------------
# REVERT unlinks the transaction children (docs/29 F3)
# ---------------------------------------------------------------------------


def test_revert_unlinks_llm_suggestions_and_reconciliation_matches(
    client: TestClient, session: Session
) -> None:
    """`_purge_transaction` (the single-row path) deletes tag links, LLM
    suggestions and reconciliation matches before the row — none of those FKs
    cascade. The batch paths only unlinked tags, so a committed batch whose
    transactions had been through the suggestion pipeline or a reconciliation
    could not be reverted at all: the DELETE trips an IntegrityError and the
    endpoint 500s.
    """
    auth = _register(client)
    headers, user_id = auth["headers"], auth["user_id"]
    account = _account(session, user_id, "chequing")
    batch = _batch(session, user_id, account.id, "committed")
    txn = _txn(session, user_id, account, batch_id=batch.id)

    category = Category(user_id=user_id, name="Groceries")
    session.add(category)
    session.commit()
    session.refresh(category)
    session.add(
        LlmSuggestion(
            user_id=user_id,
            transaction_id=txn.id,
            proposed_category_id=category.id,
            confidence=0.9,
            status=SuggestionStatus.suggested,
            model_used="test-model",
        )
    )
    session.add(
        ReconciliationItem(
            user_id=user_id,
            account_id=account.id,
            transaction_id=txn.id,
            statement_date="2026-01-05",
            statement_amount=Decimal("-42.50"),
            statement_description="GROCERY MART #12",
            status="matched",
        )
    )
    session.commit()
    txn_id = txn.id

    resp = client.post(f"{API}/imports/{batch.id}/revert", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reverted"

    session.expire_all()
    assert session.get(Transaction, txn_id) is None
    assert (
        session.exec(select(LlmSuggestion).where(LlmSuggestion.transaction_id == txn_id)).all()
        == []
    ), "llm_suggestions rows were left dangling on a reverted batch"
    assert (
        session.exec(
            select(ReconciliationItem).where(ReconciliationItem.transaction_id == txn_id)
        ).all()
        == []
    ), "reconciliation_items rows were left dangling on a reverted batch"


# ---------------------------------------------------------------------------
# The pre-fix orphans (prod batches 176/177) and the repair that clears them
# ---------------------------------------------------------------------------


def test_terminal_batch_sweep_clears_orphans_without_moving_positions(
    client: TestClient, session: Session
) -> None:
    """The one-off repair must be position- and pool-neutral.

    Rebuilds the exact prod shape — a `reverted` batch that still carries its
    trades (the pre-fix `POST /imports/{id}/revert` only deleted
    transactions) — then runs `sweep_terminal_batch_trades`. Those rows are
    already excluded by `committed_trade_conditions`, so deleting them can
    change nothing a user can see: positions, book cost and ACB pools must be
    byte-identical before and after, with the orphan rows gone.
    """
    auth = _register(client)
    user_id = auth["user_id"]
    account = _account(session, user_id)  # non-registered → pools are read
    security = _security(session, user_id, "ABCD")
    _price(session, user_id, security, _today(), "20.00")

    _trade(
        session,
        user_id,
        account,
        security,
        "buy",
        batch_id=None,
        quantity="10",
        price_native="20.00",
    )
    orphan_batch = _batch(session, user_id, account.id, "reverted")  # pre-fix state
    for _ in range(2):
        _trade(
            session,
            user_id,
            account,
            security,
            "buy",
            batch_id=orphan_batch.id,
            quantity="100",
            price_native="20.00",
        )
    rebuild_user_pools(session, user_id)
    session.commit()

    positions_before = _positions(session, user_id)
    pools_before = _pools(session, user_id)
    assert len(_batch_trades(session, orphan_batch.id)) == 2

    report = sweep_terminal_batch_trades(session, apply=True)
    session.commit()
    session.expire_all()

    assert report["applied"] is True
    assert report["trades_deleted"] == 2
    assert [row["batch_id"] for row in report["batches"]] == [orphan_batch.id]
    assert _batch_trades(session, orphan_batch.id) == []
    assert _positions(session, user_id) == positions_before, "the repair moved positions"
    assert _pools(session, user_id) == pools_before, "the repair moved acb_pools"

    # Idempotent: a second run finds nothing.
    again = sweep_terminal_batch_trades(session, apply=True)
    session.commit()
    assert again["trades_deleted"] == 0
    assert again["batches"] == []


def test_terminal_batch_sweep_reports_without_applying_by_default(
    client: TestClient, session: Session
) -> None:
    """Default is dry-run: the operator sees what WOULD go before `--apply`."""
    auth = _register(client)
    user_id = auth["user_id"]
    account = _account(session, user_id, "tfsa")
    security = _security(session, user_id, "IJKL")
    batch = _batch(session, user_id, account.id, "reverted")
    _trade(
        session,
        user_id,
        account,
        security,
        "buy",
        batch_id=batch.id,
        quantity="1",
        price_native="1.00",
    )

    report = sweep_terminal_batch_trades(session)
    session.commit()
    assert report["applied"] is False
    assert report["trades_matched"] == 1
    assert report["trades_deleted"] == 0, "dry-run must delete nothing"
    assert len(_batch_trades(session, batch.id)) == 1, "dry-run must delete nothing"


def test_terminal_batch_sweep_ignores_committed_and_pending_batches(
    client: TestClient, session: Session
) -> None:
    """A committed batch's trades are LIVE data — the sweep must never touch
    them, and neither may a pending one (it is the user's un-decided review)."""
    auth = _register(client)
    user_id = auth["user_id"]
    account = _account(session, user_id, "tfsa")
    security = _security(session, user_id, "MNOP")
    committed = _batch(session, user_id, account.id, "committed")
    pending = _batch(session, user_id, account.id, "pending_review")
    for b in (committed, pending):
        _trade(
            session,
            user_id,
            account,
            security,
            "buy",
            batch_id=b.id,
            quantity="1",
            price_native="1.00",
        )

    report = sweep_terminal_batch_trades(session, apply=True)
    session.commit()
    assert report["batches"] == []
    assert report["trades_deleted"] == 0
    assert len(_batch_trades(session, committed.id)) == 1
    assert len(_batch_trades(session, pending.id)) == 1
