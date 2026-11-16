"""M110 budget rollover — per-category carry-forward (docs/09 §Budget carry).

Each case maps to one acceptance line of the card:

* carry on  -> the source period's leftover lands in `carried_in` next period,
  and spendable = assigned + carried_in - spent (docs/09 layering)
* deficit   -> carried_in is negative, spendable shrinks, the sign the UI tones
  on is right there in the wire value
* carry off / no governed source period -> nothing carries, zero changes
* back-view -> the SNAPSHOT, not a recomputation (audit-honest)
* pct       -> the spendable denominator, still the unclamped true ratio, and
  `null` only where no denominator exists
* alerts    -> the same denominator as the card (M61 #3 extended by M110)
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.budgets import Budget, BudgetCarryover
from app.models.spending_alerts import SpendingAlert
from app.services.txn_filters import today_et
from tests.test_deep_common import auth, create_account, get_category, register, seed_transaction


def _today() -> date:
    """The clock, read at CALL time — never captured at import (t_435c2d14)."""
    return today_et()


def _prev_month_day(anchor: date, day: int) -> date:
    """`day`-ish in the month before the one containing `anchor`."""
    return (anchor.replace(day=1) - timedelta(days=1)).replace(day=min(day, 28))


def _prev_month_start(anchor: date) -> date:
    """First day of the month before `anchor`'s — the SOURCE of a monthly carry."""
    return (anchor.replace(day=1) - timedelta(days=1)).replace(day=1)


def _setup(client: TestClient, session: Session) -> tuple[dict, int, int, int]:
    """(headers, user_id, groceries category id, account id)."""
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    account_id = create_account(client, headers)
    cat_id = get_category(session, user_id, "groceries")
    return headers, user_id, cat_id, account_id


def _make_budget(
    client: TestClient, headers: dict, cat_id: int, amount: str = "600", carry: str | None = None
) -> dict:
    payload: dict = {"category_id": cat_id, "amount": float(amount), "period": "monthly"}
    if carry is not None:
        payload["carry"] = carry
    r = client.post("/api/v1/budgets", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _backdate(session: Session, budget_id: int, when: date) -> None:
    """Put `created_at` before `when` — docs/09's rule is that a budget only
    carries from a period it governed in full, so a budget "created today" can
    never have been in force last month. Tests move the clock here instead of
    waiting a calendar month."""
    budget = session.get(Budget, budget_id)
    assert budget is not None
    budget.created_at = datetime.combine(when, time.min, tzinfo=UTC)
    session.add(budget)
    session.commit()


def _snapshot(session: Session, budget_id: int, period_start: date) -> Decimal | None:
    """The frozen carried value for `period_start` — `None` when nothing was
    ever materialized for it."""
    rows = session.exec(
        select(BudgetCarryover).where(
            BudgetCarryover.budget_id == budget_id,
            BudgetCarryover.period_start == period_start,
        )
    ).all()
    assert len(rows) <= 1, f"the freeze allows ONE row per period, found {len(rows)}"
    return rows[0].amount if rows else None


def _snapshots(session: Session, budget_id: int) -> dict[date, Decimal]:
    rows = session.exec(select(BudgetCarryover).where(BudgetCarryover.budget_id == budget_id)).all()
    return {row.period_start: row.amount for row in rows}


def _get(client: TestClient, headers: dict) -> dict:
    r = client.get("/api/v1/budgets", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1, r.text
    return r.json()[0]


# --- carry on -----------------------------------------------------------------


def test_carry_forward_puts_the_source_months_leftover_in_carried_in(
    client: TestClient, session: Session
) -> None:
    """carry on -> end-of-month leftover appears as carried_in next month, and
    spendable = assigned + carried_in - spent is the number everything reads."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    # $600 budgeted, $540 spent last month -> $60.00 leftover to carry.
    seed_transaction(
        session,
        user_id,
        account_id,
        "Last month shop",
        amount="-540.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="600", carry="forward")
    assert budget["carry"] == "forward"
    # In force BEFORE the source month opened (docs/09 pin: rollover only
    # computes for a period the budget governed in full).
    _backdate(session, budget["id"], source_start - timedelta(days=1))

    row = _get(client, headers)
    assert row["carry"] == "forward"
    assert row["carried_in"] == "60.00"
    assert row["spent"] == "0.00"
    assert row["remaining"] == "660.00"  # 600 + 60 - 0
    assert row["pct_used"] == 0

    # Spend inside the current period: the denominator is the POOL, not 600.
    seed_transaction(
        session,
        user_id,
        account_id,
        "This month shop",
        amount="-100.00",
        day=_today().replace(day=min(_today().day, 28)),
        category_id=cat_id,
    )
    row = _get(client, headers)
    assert row["spent"] == "100.00"
    assert row["carried_in"] == "60.00"
    assert row["remaining"] == "560.00"  # 600 + 60 - 100
    # 100 / 660 * 100 — the anti-fabrication check on the NEW math.
    assert row["pct_used"] == float(Decimal("100") / Decimal("660") * 100)

    # The snapshot is written exactly once, for this period only.
    assert _snapshot(session, budget["id"], _today().replace(day=1)) == Decimal("60.00")


def test_deficit_carries_as_a_negative_carried_in(client: TestClient, session: Session) -> None:
    """A month that overspent carries a DEBT: negative `carried_in`, a smaller
    spendable, and a wire value whose sign the UI can tone on (docs/09 —
    never silently absorbed)."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    seed_transaction(
        session,
        user_id,
        account_id,
        "Overspent last month",
        amount="-700.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="600", carry="forward")
    _backdate(session, budget["id"], source_start - timedelta(days=1))

    row = _get(client, headers)
    assert Decimal(row["carried_in"]) < 0
    assert row["carried_in"] == "-100.00"
    # The debt eats the assignment: 600 - 100 - 0 spent.
    assert row["remaining"] == "500.00"
    assert row["pct_used"] == 0
    assert _snapshot(session, budget["id"], _today().replace(day=1)) == Decimal("-100.00")


def test_carry_off_changes_nothing_and_writes_no_snapshot(
    client: TestClient, session: Session
) -> None:
    """The default (`off`) is the pre-M110 row, byte for byte: `carried_in` is
    `null` (not a hidden zero), remaining/pct come from `amount` alone, and not
    one row lands in `budget_carryovers`."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    seed_transaction(
        session,
        user_id,
        account_id,
        "Last month shop",
        amount="-540.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    seed_transaction(
        session,
        user_id,
        account_id,
        "This month shop",
        amount="-100.00",
        day=_today().replace(day=min(_today().day, 28)),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="600")
    assert budget["carry"] == "off"
    _backdate(session, budget["id"], source_start - timedelta(days=1))

    row = _get(client, headers)
    assert row["carry"] == "off"
    assert row["carried_in"] is None
    assert row["remaining"] == "500.00"  # 600 - 100, exactly today's math
    assert row["pct_used"] == float(Decimal("100") / Decimal("600") * 100)
    assert _snapshots(session, budget["id"]) == {}


def test_no_budget_in_the_source_period_carries_nothing(
    client: TestClient, session: Session
) -> None:
    """docs/09 / the card's pin: rollover only computes for a period the budget
    governed in full — a budget opened this month has no prior leftover to
    inherit, so it never invents a zombie balance."""
    headers, _, cat_id, _ = _setup(client, session)
    budget = _make_budget(client, headers, cat_id, amount="600", carry="forward")

    row = _get(client, headers)
    assert row["carried_in"] is None
    assert row["remaining"] == "600.00"
    assert _snapshots(session, budget["id"]) == {}


def test_back_view_returns_the_carried_value_as_it_was(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """Going BACK a period shows the value materialized at THAT boundary —
    editing the source period afterwards cannot move it (audit-honest, never
    retroactively recalculated), and each period keeps its own snapshot."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    current_start = _today().replace(day=1)
    prior_start = _prev_month_start(current_start)  # the period we read "back" into
    source_start = _prev_month_start(prior_start)  # ITS source
    seed_transaction(
        session,
        user_id,
        account_id,
        "Source month spend",
        amount="-540.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    seed_transaction(
        session,
        user_id,
        account_id,
        "Prior month spend",
        amount="-100.00",
        day=prior_start.replace(day=5),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="600", carry="forward")
    _backdate(session, budget["id"], source_start - timedelta(days=1))

    # 1. Read while `prior_start`'s month IS the current one -> boundary switch:
    #    $600 - $540 = $60 materializes for the prior period.
    monkeypatch.setattr("app.services.budgets.today_et", lambda: prior_start.replace(day=15))
    row = _get(client, headers)
    assert row["carried_in"] == "60.00"
    assert _snapshot(session, budget["id"], prior_start) == Decimal("60.00")

    # 2. The SOURCE period is edited after the fact (-999 would recompute to
    #    600 - 1539 = -939.00 if anything still recalculated).
    seed_transaction(
        session,
        user_id,
        account_id,
        "Late correction in the source month",
        amount="-999.00",
        day=source_start.replace(day=20),
        category_id=cat_id,
    )

    # 3. The back-view returns the SNAPSHOT.
    row = _get(client, headers)
    assert row["carried_in"] == "60.00", "a back-view must never recompute history"
    assert _snapshot(session, budget["id"], prior_start) == Decimal("60.00")

    # 4. The current period is its OWN snapshot, computed from the period one
    #    hop back (600 - 100 = 500) — no chain reaches into the older month.
    monkeypatch.setattr("app.services.budgets.today_et", lambda: _today())
    row = _get(client, headers)
    assert row["carried_in"] == "500.00"
    assert _snapshots(session, budget["id"]) == {
        prior_start: Decimal("60.00"),
        current_start: Decimal("500.00"),
    }


# --- pct honesty --------------------------------------------------------------


def test_pct_uses_the_spendable_pool_and_stays_unclamped(
    client: TestClient, session: Session
) -> None:
    """M65's anti-fabrication check on the NEW denominator: the true ratio
    against `amount + carried_in`, never capped at 100."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    seed_transaction(
        session,
        user_id,
        account_id,
        "Source month spend",
        amount="-540.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    seed_transaction(
        session,
        user_id,
        account_id,
        "Blowout",
        amount="-1980.00",
        day=_today().replace(day=min(_today().day, 28)),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="600", carry="forward")
    _backdate(session, budget["id"], source_start - timedelta(days=1))

    row = _get(client, headers)
    # pool = 600 + 60 = 660; spent 1980 -> exactly 300%, not "100%".
    assert row["carried_in"] == "60.00"
    assert row["remaining"] == "-1320.00"
    assert row["pct_used"] == 300.0


def test_a_carried_debt_that_swallows_the_assignment_has_no_pct(
    client: TestClient, session: Session
) -> None:
    """When `amount + carried_in <= 0` there is no denominator to divide by, so
    `pct_used` is `null` — the honest answer is "no ratio exists", never a
    fabricated 0% and never a clamped 100% (docs/09). The debt itself is the
    number the row shows."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    seed_transaction(
        session,
        user_id,
        account_id,
        "Catastrophic source month",
        amount="-250.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="100", carry="forward")
    _backdate(session, budget["id"], source_start - timedelta(days=1))

    row = _get(client, headers)
    assert row["carried_in"] == "-150.00"  # 100 - 250
    assert row["remaining"] == "-50.00"  # pool = 100 - 150, nothing spent yet
    assert row["pct_used"] is None


# --- alerts: one denominator, two surfaces (M61 #3) ---------------------------


def test_alerts_measure_the_spendable_pool_like_the_card(
    client: TestClient, session: Session
) -> None:
    """The Dashboard alert and the Budgets card print ONE number. With a carry
    on, $600 spent against a $600 assignment + $60 carried is 90.9% (warning),
    not the 100% (critical) the bare assignment would report."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    seed_transaction(
        session,
        user_id,
        account_id,
        "Source month spend",
        amount="-540.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    seed_transaction(
        session,
        user_id,
        account_id,
        "This month spend",
        amount="-600.00",
        day=_today().replace(day=min(_today().day, 28)),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="600", carry="forward")
    _backdate(session, budget["id"], source_start - timedelta(days=1))

    card = _get(client, headers)
    alerts = client.get("/api/v1/budgets/alerts", headers=headers)
    assert alerts.status_code == 200, alerts.text
    payload = alerts.json()
    assert payload["count"] == 1, payload
    alert = payload["alerts"][0]
    assert alert["alert_type"] == "warning"
    assert alert["budget_amount"] == "660.00"  # the denominator actually used
    # Same predicate, same rounded number on both surfaces (M61 #3).
    assert float(alert["actual_pct"]) == round(card["pct_used"], 2)


def test_an_unpayable_carried_debt_alerts_critical(client: TestClient, session: Session) -> None:
    """`pct_used` is null for a non-positive spendable, but a threshold row
    cannot print "no percent": the row is over before it spent anything, so it
    fires critical at the documented storage ceiling (docs/08 §Budgets,
    docs/35)."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    seed_transaction(
        session,
        user_id,
        account_id,
        "Catastrophic source month",
        amount="-250.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="100", carry="forward")
    _backdate(session, budget["id"], source_start - timedelta(days=1))

    card = _get(client, headers)
    assert card["pct_used"] is None

    alerts = client.get("/api/v1/budgets/alerts", headers=headers)
    payload = alerts.json()
    assert payload["count"] == 1, payload
    assert payload["alerts"][0]["alert_type"] == "critical"
    assert payload["alerts"][0]["actual_pct"] == "999.99"


# --- lifecycle ----------------------------------------------------------------


def test_deleting_a_budget_removes_its_snapshots_and_alerts(
    client: TestClient, session: Session
) -> None:
    """Child rows go with the budget (docs/02): a frozen snapshot — or a
    threshold alert — left behind would make the DELETE fail on the FK."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    seed_transaction(
        session,
        user_id,
        account_id,
        "Source month spend",
        amount="-700.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    seed_transaction(
        session,
        user_id,
        account_id,
        "This month spend",
        amount="-600.00",
        day=_today().replace(day=min(_today().day, 28)),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="600", carry="forward")
    _backdate(session, budget["id"], source_start - timedelta(days=1))
    assert _get(client, headers)["carried_in"] == "-100.00"
    assert client.get("/api/v1/budgets/alerts", headers=headers).json()["count"] == 1
    assert session.exec(select(SpendingAlert)).all()

    deleted = client.delete(f"/api/v1/budgets/{budget['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    assert not session.exec(select(BudgetCarryover)).all()
    assert not session.exec(select(SpendingAlert)).all()
    assert client.get("/api/v1/budgets", headers=headers).json() == []


def test_toggling_carry_does_not_rewrite_a_frozen_snapshot(
    client: TestClient, session: Session
) -> None:
    """The flag controls whether a value is READ, not what it was (docs/09):
    turning carry off hides `carried_in` and restores today's math, turning it
    back on shows the same frozen number."""
    headers, user_id, cat_id, account_id = _setup(client, session)
    source_start = _prev_month_start(_today())
    seed_transaction(
        session,
        user_id,
        account_id,
        "Source month spend",
        amount="-540.00",
        day=source_start.replace(day=5),
        category_id=cat_id,
    )
    budget = _make_budget(client, headers, cat_id, amount="600", carry="forward")
    _backdate(session, budget["id"], source_start - timedelta(days=1))
    assert _get(client, headers)["carried_in"] == "60.00"

    off = client.patch(f"/api/v1/budgets/{budget['id']}", headers=headers, json={"carry": "off"})
    assert off.status_code == 200, off.text
    assert off.json()["carry"] == "off"
    row = _get(client, headers)
    assert row["carried_in"] is None
    assert row["remaining"] == "600.00"

    on = client.patch(f"/api/v1/budgets/{budget['id']}", headers=headers, json={"carry": "forward"})
    assert on.status_code == 200
    row = _get(client, headers)
    assert row["carried_in"] == "60.00"  # the frozen snapshot, not recomputed
