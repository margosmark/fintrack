"""Budgets: CRUD + spent computation with seeded transactions."""

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.fx_rates import FxRate, FxSource
from app.models.transactions import Transaction
from app.services.txn_filters import today_et
from tests.test_deep_common import auth, create_account, get_category, register, seed_transaction


def _today() -> date:
    """The clock, read at CALL time — never captured at import (t_435c2d14)."""
    return today_et()


def _setup(client: TestClient, session: Session) -> tuple[dict, int, int]:
    """Register user, create account, return (headers, user_id, groceries_cat_id)."""
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    create_account(client, headers)
    cat_id = get_category(session, user_id, "groceries")
    return headers, user_id, cat_id


def _prev_month_day(anchor: date, day: int) -> date:
    """`day`-ish in the month before the one containing `anchor`."""
    return (anchor.replace(day=1) - timedelta(days=1)).replace(day=min(day, 28))


def _tx(
    session: Session,
    user_id: int,
    account_id: int,
    payee: str,
    amount: str,
    day: date,
    category_id: int | None = None,
    currency: str = "CAD",
    cad_amount: str | None = None,
) -> None:
    """Seed one manual (non-batch) transaction with an explicit CAD mirror."""
    session.add(
        Transaction(
            user_id=user_id,
            account_id=account_id,
            date=day,
            posting_date=day,
            raw_description=payee,
            normalized_payee=payee,
            amount_native=Decimal(amount),
            currency=currency,
            cad_amount=Decimal(cad_amount) if cad_amount is not None else None,
            dedup_hash=hashlib.sha256(f"{payee}-{day}-{amount}-{account_id}".encode()).hexdigest(),
            category_id=category_id,
        )
    )
    session.commit()


def _budget(client: TestClient, headers: dict, category_id: int, amount: str = "200") -> None:
    r = client.post(
        "/api/v1/budgets",
        headers=headers,
        json={"category_id": category_id, "amount": float(amount), "period": "monthly"},
    )
    assert r.status_code == 201


def _as_currency(session: Session, account_id: int, currency: str) -> None:
    """Flip an account's currency directly in the session.

    Creating a foreign-currency account through the API makes the response
    serializer call `fx.latest_rate(..., allow_fetch=True)`, which performs a
    real Bank of Canada fetch and writes rows into the SHARED `fx_rates` table
    (docs/07). Those rows would silently out-rank this test's own rate. Setting
    the column here keeps the test hermetic — no network, no shared-table writes.
    """
    from app.models.accounts import Account

    account = session.get(Account, account_id)
    account.currency = currency
    session.add(account)
    session.commit()


def test_budget_spent_computation(client: TestClient, session: Session) -> None:
    """Budget spent amount reflects seeded transactions in current period."""
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Savings")

    # Seed transactions in the current period (relative to the real clock —
    # budgets bound the window below only, so any day of this month counts).
    seed_transaction(
        session,
        user_id,
        acct,
        "Walmart",
        amount="-85.00",
        day=_today().replace(day=1),
        category_id=cat_id,
    )
    seed_transaction(
        session,
        user_id,
        acct,
        "No Frills",
        amount="-42.50",
        day=_today().replace(day=5),
        category_id=cat_id,
    )
    seed_transaction(
        session,
        user_id,
        acct,
        "Other",
        amount="-20.00",
        day=_today().replace(day=3),
        category_id=get_category(session, user_id, "dining"),
    )

    # Create budget for groceries
    r = client.post(
        "/api/v1/budgets",
        headers=headers,
        json={
            "category_id": cat_id,
            "amount": 200,
            "period": "monthly",
        },
    )
    assert r.status_code == 201

    # Check spent computation
    r = client.get("/api/v1/budgets", headers=headers)
    assert r.status_code == 200
    budgets = r.json()
    assert len(budgets) == 1
    b = budgets[0]
    assert b["category_name"] == "groceries"
    assert float(b["spent"]) == 127.50  # 85 + 42.50
    assert float(b["remaining"]) == 72.50
    assert b["pct_used"] == 63.75  # 127.50 / 200 * 100


def test_budget_overspend_reports_true_pct(client: TestClient, session: Session) -> None:
    """M61 #3 / M65: `pct_used` is the true ratio, never clamped.

    The BAR caps its width at 100% (`Math.min` in Budgets.tsx) but the label
    prints `Math.round(pct_used)` — capping the number made a 250% overspend
    read as "100% used", i.e. exactly on budget. docs/08 pins `pct_used` as
    `float(spent / amount * 100)` with no cap.
    """
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Savings")

    seed_transaction(
        session,
        user_id,
        acct,
        "Big shop",
        amount="-500.00",
        day=_today().replace(day=1),
        category_id=cat_id,
    )

    client.post(
        "/api/v1/budgets",
        headers=headers,
        json={
            "category_id": cat_id,
            "amount": 200,
            "period": "monthly",
        },
    )

    r = client.get("/api/v1/budgets", headers=headers)
    b = r.json()[0]
    assert float(b["spent"]) == 500.00
    assert b["pct_used"] == 250.0  # 500.00 / 200.00 * 100 — unclamped


def test_budget_only_counts_current_period(client: TestClient, session: Session) -> None:
    """Transactions from previous month don't count toward current budget."""
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Savings")

    # Previous month transaction
    seed_transaction(
        session,
        user_id,
        acct,
        "Old",
        amount="-300.00",
        day=_prev_month_day(_today(), 15),
        category_id=cat_id,
    )
    # Current month transaction
    seed_transaction(
        session,
        user_id,
        acct,
        "New",
        amount="-50.00",
        day=_today().replace(day=1),
        category_id=cat_id,
    )

    client.post(
        "/api/v1/budgets",
        headers=headers,
        json={
            "category_id": cat_id,
            "amount": 200,
            "period": "monthly",
        },
    )

    r = client.get("/api/v1/budgets", headers=headers)
    b = r.json()[0]
    assert float(b["spent"]) == 50.00  # only current month


def test_budget_tenancy_isolation(client: TestClient, session: Session) -> None:
    """User A's budgets are not visible to User B."""
    h_a = register(client)
    headers_a = auth(h_a["access_token"])
    cat_a = get_category(session, h_a["user"]["id"], "groceries")
    client.post("/api/v1/budgets", headers=headers_a, json={"category_id": cat_a, "amount": 100})

    h_b = register(client)
    headers_b = auth(h_b["access_token"])
    r = client.get("/api/v1/budgets", headers=headers_b)
    assert r.json() == []


def test_budget_counts_null_posting_date(client: TestClient, session: Session) -> None:
    """Manual rows with posting_date=None still count toward spent.

    Regression: _budget_spent filtered on posting_date, silently dropping
    every manual/demo transaction from budget progress.
    """
    import hashlib
    from decimal import Decimal

    from app.models.transactions import Transaction

    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Savings")

    session.add(
        Transaction(
            user_id=user_id,
            account_id=acct,
            date=_today().replace(day=2),
            posting_date=None,
            raw_description="MANUAL SHOP",
            normalized_payee="MANUAL SHOP",
            amount_native=Decimal("-60.00"),
            currency="CAD",
            dedup_hash=hashlib.sha256(b"manual-shop-null-posting-date").hexdigest(),
            category_id=cat_id,
        )
    )
    session.commit()

    client.post(
        "/api/v1/budgets",
        headers=headers,
        json={
            "category_id": cat_id,
            "amount": 200,
            "period": "monthly",
        },
    )

    r = client.get("/api/v1/budgets", headers=headers)
    assert r.status_code == 200
    assert float(r.json()[0]["spent"]) == 60.00


# ------------------------------------------- signed CAD spending regressions


def test_budget_refund_offsets_spending_not_adds_to_it(
    client: TestClient, session: Session
) -> None:
    """A refund (positive txn) REDUCES spent — `abs()` made it add instead.

    Regression: _budget_spent summed `abs(amount_native)`, so a +50.00 return
    inflated a -100.00 purchase to 150.00 of "spent".
    """
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Savings")

    _tx(
        session,
        user_id,
        acct,
        "Walmart",
        "-100.00",
        _today().replace(day=2),
        cat_id,
        cad_amount="-100.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Walmart Refund",
        "50.00",
        _today().replace(day=3),
        cat_id,
        cad_amount="50.00",
    )

    _budget(client, headers, cat_id)

    b = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert float(b["spent"]) == 50.00  # net 100 - 50
    assert float(b["remaining"]) == 150.00


def test_budget_refund_below_zero_spend_clamps_at_zero(
    client: TestClient, session: Session
) -> None:
    """A refund larger than the purchase yields spent 0, never a negative bar."""
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Savings")

    _tx(
        session,
        user_id,
        acct,
        "Small Shop",
        "-30.00",
        _today().replace(day=2),
        cat_id,
        cad_amount="-30.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Full Refund",
        "45.00",
        _today().replace(day=4),
        cat_id,
        cad_amount="45.00",
    )

    _budget(client, headers, cat_id)

    b = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert float(b["spent"]) == 0.00
    assert float(b["remaining"]) == 200.00
    assert b["pct_used"] == 0


def test_budget_excludes_income_category_rows(client: TestClient, session: Session) -> None:
    """Payroll/booked-income rows in a budgeted category are not "spent"."""
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Savings")

    _tx(
        session,
        user_id,
        acct,
        "Refund-like income",
        "75.00",
        _today().replace(day=2),
        cat_id,
        cad_amount="75.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Bonus",
        "300.00",
        _today().replace(day=3),
        cat_id,
        cad_amount="300.00",
    )

    _budget(client, headers, cat_id)

    b = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert float(b["spent"]) == 0.00


def test_budget_spent_uses_cad_amount_not_native_amount(
    client: TestClient, session: Session
) -> None:
    """USD purchases convert at the stored CAD mirror (reporting currency).

    Regression: spent summed `amount_native`, so a USD -100.00 charge at 1.35
    counted 100.00 against a CAD budget instead of 135.00.
    """
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "USD Card")
    _as_currency(session, acct, "USD")

    _tx(
        session,
        user_id,
        acct,
        "US STORE",
        "-100.00",
        _today().replace(day=2),
        cat_id,
        currency="USD",
        cad_amount="-135.00",
    )

    _budget(client, headers, cat_id, amount="200")

    b = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert float(b["spent"]) == 135.00
    assert float(b["remaining"]) == 65.00


def test_budget_multi_currency_sum_uses_cad_mirror(client: TestClient, session: Session) -> None:
    """A CAD + USD month sums in CAD, not by adding native numbers together."""
    headers, user_id, cat_id = _setup(client, session)
    cad_acct = create_account(client, headers, "Chequing")
    usd_acct = create_account(client, headers, "USD Card")
    _as_currency(session, usd_acct, "USD")

    _tx(
        session,
        user_id,
        cad_acct,
        "Metro",
        "-40.00",
        _today().replace(day=2),
        cat_id,
        cad_amount="-40.00",
    )
    _tx(
        session,
        user_id,
        usd_acct,
        "US MARKET",
        "-20.00",
        _today().replace(day=3),
        cat_id,
        currency="USD",
        cad_amount="-27.00",
    )

    _budget(client, headers, cat_id, amount="200")

    b = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert float(b["spent"]) == 67.00  # 40 CAD + 27 CAD, not 60 native


def test_budget_unresolved_fx_row_is_excluded_not_native(
    client: TestClient, session: Session
) -> None:
    """A foreign row with no resolvable CAD rate is EXCLUDED (docs/12).

    Regression: the native amount was summed, so an unconvertible USD charge
    silently billed the budget at its raw USD face value.
    """
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "USD Card")
    _as_currency(session, acct, "USD")

    _tx(
        session,
        user_id,
        acct,
        "Known",
        "-50.00",
        _today().replace(day=2),
        cat_id,
        cad_amount="-50.00",
    )
    _tx(
        session,
        user_id,
        acct,
        "Unresolved",
        "-999.00",
        _today().replace(day=3),
        cat_id,
        currency="USD",
        cad_amount=None,
    )

    _budget(client, headers, cat_id, amount="200")

    b = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert float(b["spent"]) == 50.00


def test_budget_falls_back_to_newest_cached_fx_rate_on_or_before_day(
    client: TestClient, session: Session
) -> None:
    """A row with no stored CAD mirror converts at the newest rate on/before its date.

    `cached_cad_amount` mirrors only the EXACT transaction date, so a manual
    weekend row can be stored with `cad_amount = NULL`; the budget must still
    price it (FxCache newest-on-or-before, docs/12) instead of dropping it.
    """
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "USD Card")
    day = _today().replace(day=5)

    # Seeded on the row's own date, and hermetic (see _as_currency), so this is
    # provably the newest cached rate on or before the transaction date.
    session.add(
        FxRate(
            base="USD",
            quote="CAD",
            rate=Decimal("1.20"),
            date=day,
            source=FxSource.manual,
        )
    )
    session.commit()

    _tx(session, user_id, acct, "US STORE", "-100.00", day, cat_id, currency="USD", cad_amount=None)

    _budget(client, headers, cat_id, amount="200")

    b = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert float(b["spent"]) == 120.00  # 100 USD x 1.20


# --- M137 P0-2: GET /budgets/recurring-summary (docs/09 §Recurring prompt) ---


def test_recurring_summary_is_empty_on_a_tenant_with_no_tagged_rows(
    client: TestClient, session: Session
) -> None:
    """One tiny read — the Budgets page no longer downloads the ledger."""
    headers, _, _ = _setup(client, session)
    r = client.get("/api/v1/budgets/recurring-summary", headers=headers)
    assert r.status_code == 200, r.text
    # `monthly` is a Decimal, so it serializes as a decimal STRING — the wire
    # shape every other money field in this API already has (AGENTS.md §2).
    assert r.json() == {"count": 0, "monthly": "0.00"}


def test_recurring_summary_weights_cadence_and_excludes_unmirrorred_fx(
    client: TestClient, session: Session
) -> None:
    """M96's label, server-side: docs/12's amount rule + the cadence table."""
    from app.models.transactions import RecurringCadence

    headers, user_id, _ = _setup(client, session)
    acct = create_account(client, headers, "Chequing")
    base = _today().replace(day=5)

    rows = [
        # monthly at face value (reporting currency)
        ("Netflix", "-16.99", RecurringCadence.monthly, "CAD", "-16.99"),
        # weekly x 30.4375/7
        ("Transit Pass", "-10.00", RecurringCadence.weekly, "CAD", "-10.00"),
        # foreign, counts at its reporting-currency mirror
        ("Streaming USD", "-15.00", RecurringCadence.monthly, "USD", "-120.00"),
        # foreign with NO mirror: EXCLUDED, never counted at face value
        ("No Mirror", "-99.00", RecurringCadence.monthly, "USD", None),
    ]
    for index, (payee, amount, cadence, currency, cad_amount) in enumerate(rows):
        row = Transaction(
            user_id=user_id,
            account_id=acct,
            date=base.replace(day=5 - index),
            posting_date=base.replace(day=5 - index),
            raw_description=payee,
            normalized_payee=payee,
            amount_native=Decimal(amount),
            currency=currency,
            cad_amount=Decimal(cad_amount) if cad_amount is not None else None,
            dedup_hash=hashlib.sha256(f"{payee}-{index}".encode()).hexdigest(),
            is_recurring=True,
            recurring_cadence=cadence,
        )
        session.add(row)
    session.commit()

    body = client.get("/api/v1/budgets/recurring-summary", headers=headers).json()
    assert body["count"] == 3
    # -16.99 + (-10 x 30.4375/7) + (-120), rounded to the cent.
    #
    # Computed in Decimal here on purpose. The old expectation used binary
    # floats — which is how `float(value) * 30.4375/7` slipped into this
    # service in the first place (AGENTS.md §2). Comparing a Decimal against a
    # float here would compare exactly, so the expectation must be Decimal too;
    # the assertion below also pins the serialized string shape.
    expected = (
        Decimal("-16.99")
        + Decimal("-10.00") * (Decimal("30.4375") / Decimal(7))
        + Decimal("-120.00")
    ).quantize(Decimal("0.01"))
    assert body["monthly"] == str(expected)


def test_recurring_summary_never_reads_an_uncommitted_import(
    client: TestClient, session: Session
) -> None:
    """docs/03 staging applies to the label too."""
    from app.models.import_batches import ImportBatch, ImportBatchStatus
    from app.models.transactions import RecurringCadence

    headers, user_id, _ = _setup(client, session)
    acct = create_account(client, headers, "Chequing")
    pending = ImportBatch(
        user_id=user_id,
        account_id=acct,
        institution="test",
        file_name="stage.qfx",
        status=ImportBatchStatus.pending_review,
    )
    session.add(pending)
    session.commit()
    session.refresh(pending)
    session.add(
        Transaction(
            user_id=user_id,
            account_id=acct,
            date=_today().replace(day=6),
            posting_date=_today().replace(day=6),
            raw_description="Staged Recurring",
            normalized_payee="Staged Recurring",
            amount_native=Decimal("-10.00"),
            currency="CAD",
            dedup_hash=hashlib.sha256(b"staged-recurring").hexdigest(),
            import_batch_id=pending.id,
            is_recurring=True,
            recurring_cadence=RecurringCadence.monthly,
        )
    )
    session.commit()

    assert client.get("/api/v1/budgets/recurring-summary", headers=headers).json() == {
        "count": 0,
        "monthly": "0.00",
    }


def test_recurring_summary_monthly_is_a_decimal_string_not_a_json_number(
    client: TestClient, session: Session
) -> None:
    """A money value must not cross the wire as a binary float.

    The service used to accumulate in `float` and `round(monthly, 2)`, which
    both violates AGENTS.md §2 (money is Decimal end-to-end) and handed the
    browser a JSON number it could not round the way the ledger rounds. It now
    serializes as a decimal string, like every other money field in docs/08.
    """
    from app.models.transactions import RecurringCadence

    headers, user_id, _ = _setup(client, session)
    acct = create_account(client, headers, "Chequing")
    day = _today().replace(day=5)
    row = Transaction(
        user_id=user_id,
        account_id=acct,
        date=day,
        posting_date=day,
        raw_description="Weekly",
        normalized_payee="Weekly",
        amount_native=Decimal("-10.00"),
        currency="CAD",
        cad_amount=Decimal("-10.00"),
        dedup_hash=hashlib.sha256(b"recurring-decimal").hexdigest(),
        is_recurring=True,
        recurring_cadence=RecurringCadence.weekly,
    )
    session.add(row)
    session.commit()

    raw = client.get("/api/v1/budgets/recurring-summary", headers=headers).text
    body = json.loads(raw)
    assert isinstance(body["monthly"], str), f"monthly must serialize as a string, got: {raw}"
    # 10 x 30.4375/7 = 43.482142857… -> 43.48
    assert body["monthly"] == "-43.48"


def test_budget_zero_rate_cad_row_needs_no_fx_row(client: TestClient, session: Session) -> None:
    """A CAD row with a NULL mirror is still spent (reporting currency = CAD)."""
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Chequing")

    _tx(
        session,
        user_id,
        acct,
        "Manually Added",
        "-60.00",
        _today().replace(day=2),
        cat_id,
        currency="CAD",
        cad_amount=None,
    )

    _budget(client, headers, cat_id, amount="200")

    b = client.get("/api/v1/budgets", headers=headers).json()[0]
    assert float(b["spent"]) == 60.00


# --- P1-5: budget progress is indifferent to the write guard ----------------
#
# `GET /budgets` serves BOTH the definitions and their progress (docs/08
# §budgets; M137 A13), and the progress reads the plaintext `amount_native` /
# `cad_amount` columns — never the vault. M134 P1-5 asks that to be proven per
# surface: the same `spent` / `remaining` / `pct_used` numbers under
# `encryption_required` on and off, not merely a 200.


def _set_encryption_required(client: TestClient, headers: dict, value: bool) -> None:
    r = client.patch("/api/v1/users/me", json={"encryption_required": value}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["encryption_required"] is value


@pytest.mark.parametrize("encryption_required", [False, True])
def test_budget_progress_is_indifferent_to_encryption_required(
    client: TestClient, session: Session, encryption_required: bool
) -> None:
    """P1-5: definitions AND progress are lock-invariant — the guard changes
    which WRITES are accepted, never what the progress query returns."""
    headers, user_id, cat_id = _setup(client, session)
    acct = create_account(client, headers, "Chequing")
    seed_transaction(
        session,
        user_id,
        acct,
        "Walmart",
        amount="-85.00",
        day=_today().replace(day=1),
        category_id=cat_id,
    )
    seed_transaction(
        session,
        user_id,
        acct,
        "No Frills",
        amount="-42.50",
        day=_today().replace(day=5),
        category_id=cat_id,
    )
    # A row in a DIFFERENT category in the same month: the budget must count
    # only its own category, under either value of the flag. (An income row
    # booked into the budgeted category would OFFSET the spend — the signed
    # net floors at zero, `test_budget_refund_below_zero_spend_clamps_at_zero`
    # — so this test keeps its positive amounts out of the budgeted category.)
    seed_transaction(
        session,
        user_id,
        acct,
        "Other",
        amount="-20.00",
        day=_today().replace(day=3),
        category_id=get_category(session, user_id, "dining"),
    )
    _budget(client, headers, cat_id, amount="200")

    _set_encryption_required(client, headers, encryption_required)
    under_flag = client.get("/api/v1/budgets", headers=headers)
    assert under_flag.status_code == 200, under_flag.text
    under_flag = under_flag.json()

    _set_encryption_required(client, headers, not encryption_required)
    under_other = client.get("/api/v1/budgets", headers=headers)
    assert under_other.status_code == 200, under_other.text
    assert under_flag == under_other.json(), "the guard must not move a progress number"

    assert len(under_flag) == 1
    budget = under_flag[0]
    assert budget["category_name"] == "groceries"
    assert Decimal(budget["spent"]) == Decimal("127.50")  # 85 + 42.50; dining row out
    assert Decimal(budget["remaining"]) == Decimal("72.50")
    assert budget["pct_used"] == 63.75
