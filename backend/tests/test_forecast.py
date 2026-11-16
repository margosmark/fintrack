"""Forecasting — M109 contract tests (docs/45 §Named tests F1-F14).

Every assertion is either a hand-derivable number (the interest on a flat-rate
mortgage, a weekly commitment's monthly total) or an identity the assumptions
panel promises (`income = recurring + residual`). No network is touched.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.accounts import Account, AccountType
from app.models.transactions import RecurringCadence, Transaction
from app.models.users import User
from app.services.demo_seed import DEMO_EMAIL

D = Decimal

API = "/api/v1"
FORECAST = f"{API}/forecast"
RUN = f"{FORECAST}/run"
SCENARIOS = f"{FORECAST}/scenarios"
TODAY = date(2026, 6, 15)  # FROZEN_TODAY from conftest

# A hand-checkable mortgage: $500k house, $100k down, 6% flat, 25 years.
PRICE = D("500000")
DOWN = D("100000")
PRINCIPAL = PRICE - DOWN
RATE = D("6")


# --------------------------------------------------------------------- seeds


_user_seq = 0


def _tax_user(session: Session) -> User:
    """Unique email per call — `test_tax_acb._user` keys off `id(session)`,
    so it collides with itself as soon as a test needs two tenants."""
    global _user_seq
    _user_seq += 1
    user = User(email=f"fc-{_user_seq}-{id(session)}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _account(
    session: Session,
    user: User,
    *,
    name: str = "Main",
    kind: AccountType = AccountType.chequing,
    starting: str = "0",
) -> Account:
    account = Account(
        user_id=user.id,
        name=name,
        type=kind,
        currency="CAD",
        starting_balance=D(starting),
        taxable=False,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


_seq = 0


def _txn(
    session: Session,
    user: User,
    account: Account,
    day: str,
    amount: str,
    *,
    payee: str = "Payee",
    recurring: bool = False,
    cadence: RecurringCadence | None = None,
) -> Transaction:
    global _seq
    _seq += 1
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date.fromisoformat(day),
        raw_description=f"raw {day} {_seq}",
        normalized_payee=payee,
        amount_native=D(amount),
        currency="CAD",
        cad_amount=D(amount),
        dedup_hash=f"fc-{user.id}-{day}-{amount}-{_seq}",
        is_recurring=recurring,
        recurring_cadence=cadence,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _seed_income(session: Session, user: User, account: Account, per_month: str = "3000") -> None:
    """One credit on the 1st of each month for a year — a salary the window
    can actually see, so `residual_income_monthly_cad` is non-zero."""
    year, month = 2025, 6
    for _ in range(13):
        month += 1
        if month > 12:
            month, year = 1, year + 1
        day = date(year, month, 1)
        if day > TODAY:
            break
        _txn(session, user, account, str(day), per_month, payee="Employer")


def _seed_expenses(session: Session, user: User, account: Account, per_month: str = "1500") -> None:
    """The mirror of `_seed_income`: one debit on the 15th of each month."""
    year, month = 2025, 6
    for _ in range(13):
        month += 1
        if month > 12:
            month, year = 1, year + 1
        day = date(year, month, 15)
        if day > TODAY:
            break
        _txn(session, user, account, str(day), f"-{per_month}", payee="Landlord")


def _run(client: TestClient, headers: dict[str, str], **params: str) -> dict:
    response = client.get(RUN, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _at(payload: dict, series: str, day: str) -> Decimal:
    """Value of `series` on the bucket that ENDS on `day`."""
    buckets = payload["buckets"]
    assert day in buckets, f"{day} not sampled (granularity?)"
    target = payload["baseline"] if series == "baseline" else _find(payload, series)
    return D(target["values"][buckets.index(day)])


def _find(payload: dict, name: str) -> dict:
    for entry in payload["scenarios"]:
        if entry["name"] == name or entry["id"] == name:
            return entry
    raise AssertionError(f"scenario {name!r} missing")


def _gap(payload: dict, name: str, day: str) -> Decimal:
    return _at(payload, name, day) - _at(payload, "baseline", day)


def _house(event_id: str = "ev-house01", day: str = "2026-09-01") -> dict:
    return {
        "id": event_id,
        "type": "house_purchase",
        "date": day,
        "home_price_cad": str(PRICE),
        "down_payment_cad": str(DOWN),
        "mortgage_rate_pct": str(RATE),
        "amortization_years": 25,
    }


def _create(
    client: TestClient, headers: dict[str, str], scenario_id: str, name: str, events: list[dict]
) -> dict:
    response = client.post(
        SCENARIOS, json={"id": scenario_id, "name": name, "events": events}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------- F1


def test_house_purchase_jumps_on_exactly_that_date(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    account = _account(session, user, starting="90000")
    _seed_income(session, user, account)
    headers = _headers(user)
    _create(client, headers, "sc-house01", "House", [_house()])

    payload = _run(client, headers, scenario_ids="sc-house01")

    # Before the purchase the two lines are identical…
    assert _gap(payload, "House", "2026-08-31") == D("0")
    # …and on purchase day the net worth does NOT move: cash became equity.
    assert _gap(payload, "House", "2026-09-01") == D("0")
    # One month later the only thing that has moved is INTEREST, and at a flat
    # 6% on $400k that is hand-checkable: 400000 x 0.06 / 12 = 2000.00.
    assert _gap(payload, "House", "2026-10-01") == D("-2000.00")
    # The month after that it is interest on the REDUCED balance — strictly
    # less than the first period, because principal has been repaid.
    incremental = _gap(payload, "House", "2026-11-01") - _gap(payload, "House", "2026-10-01")
    assert D("-2000.00") < incremental < D("-1990.00")


def test_mortgage_schedule_amortizes_the_principal_exactly() -> None:
    """The flat-rate model is still a real amortization: every dollar of the
    $400k is repaid, and it takes the full term."""
    from app.services.forecast import _mortgage_schedule, mortgage_payment

    payment = mortgage_payment(PRINCIPAL, RATE, 25)
    schedule = _mortgage_schedule(PRINCIPAL, payment, RATE, date(2026, 9, 1), date(2051, 9, 1))
    assert len(schedule) == 300  # 25 years x 12
    assert sum((principal for _, _, principal in schedule), D("0")) == PRINCIPAL
    # First period's interest is exactly 400000 x 0.06/12.
    _, first_cash, first_principal = schedule[0]
    assert first_cash - first_principal == D("2000.00")


# ---------------------------------------------------------------------- F2


def test_recurring_series_lands_on_its_own_cadence_without_double_counting(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    account = _account(session, user, starting="5000")
    _seed_income(session, user, account)
    # A weekly $50 grocery run, tagged by M96, last seen 2026-06-10.
    for day in ("2026-05-20", "2026-05-27", "2026-06-03", "2026-06-10"):
        _txn(
            session,
            user,
            account,
            day,
            "-50",
            payee="Grocery Store",
            recurring=True,
            cadence=RecurringCadence.weekly,
        )
    headers = _headers(user)
    payload = _run(client, headers)

    inputs = payload["inputs"]
    # M96's own normalization: 50 x 30.4375/7 = 217.4107… → 217.41.
    # Expense figures are MAGNITUDES throughout, exactly as `cashflow_report`
    # already publishes them, so the panel's three numbers add up.
    assert inputs["recurring_expense_monthly_cad"] == "217.41"
    # THE DOUBLE-COUNT RULE: the three published figures add up to the cent.
    assert D(inputs["expense_monthly_cad"]) == (
        D(inputs["recurring_expense_monthly_cad"]) + D(inputs["residual_expense_monthly_cad"])
    )
    assert D(inputs["income_monthly_cad"]) == (
        D(inputs["recurring_income_monthly_cad"]) + D(inputs["residual_income_monthly_cad"])
    )
    assert inputs["recurring_rows"] == 4

    # The cadence lands on ITS OWN dates in the projection: 06-17 is not a
    # monthly anchor (those fall on the 15th), so the whole step is the -50.
    step = _at(payload, "baseline", "2026-06-17") - _at(payload, "baseline", "2026-06-16")
    assert step == D("-50.00")
    # …and it repeats a week later, not on the anchor.
    step = _at(payload, "baseline", "2026-06-24") - _at(payload, "baseline", "2026-06-23")
    assert step == D("-50.00")


# ---------------------------------------------------------------------- F3


def test_same_inputs_give_a_byte_identical_envelope(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    account = _account(session, user, starting="1234.56")
    _seed_income(session, user, account)
    headers = _headers(user)
    _create(
        client,
        headers,
        "sc-det0001",
        "Determinism",
        [
            _house(),
            {
                "id": "ev-salary1",
                "type": "salary_change",
                "date": "2026-11-02",
                "monthly_delta_cad": "750.00",
            },
        ],
    )

    params = {"scenario_ids": "sc-det0001", "horizon_months": "18", "granularity": "day"}
    first = client.get(RUN, params=params, headers=headers)
    second = client.get(RUN, params=params, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert first.json()["disclaimer"] == "Projection, not a prediction."


# ---------------------------------------------------------------------- F4


def test_career_break_suppresses_income_only_inside_the_range(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    account = _account(session, user, starting="10000")
    _seed_income(session, user, account)
    _seed_expenses(session, user, account)
    headers = _headers(user)
    _create(
        client,
        headers,
        "sc-brk0001",
        "Break",
        [
            {
                "id": "ev-break01",
                "type": "career_break",
                "start_date": "2026-07-01",
                "end_date": "2026-12-31",
            }
        ],
    )

    payload = _run(client, headers, scenario_ids="sc-brk0001")
    residual_income = D(payload["inputs"]["residual_income_monthly_cad"])
    assert residual_income > 0

    # Nothing has happened yet.
    assert _gap(payload, "Break", "2026-06-30") == D("0")
    # Six monthly anchors fall inside [2026-07-01, 2026-12-31]: the gap is
    # exactly six suppressed income postings — no more, no less.
    assert _gap(payload, "Break", "2026-12-31") == -residual_income * 6
    # Income resumes: the gap stops growing after the range ends.
    assert _gap(payload, "Break", "2027-02-28") == -residual_income * 6
    # The gap deepens by exactly one suppressed posting per anchor inside the
    # range: 07-15, 08-15, 09-15 and 10-15 have gone by on 2026-10-15.
    assert _gap(payload, "Break", "2026-10-15") == -residual_income * 4
    # Expenses never stop: with income suppressed, the scenario's OWN step on
    # an in-break anchor is the expense posting alone.
    residual_expense = D(payload["inputs"]["residual_expense_monthly_cad"])
    assert residual_expense > 0
    step = _at(payload, "Break", "2026-10-15") - _at(payload, "Break", "2026-10-14")
    assert step == -residual_expense


# ---------------------------------------------------------------------- F5


def test_salary_change_and_cost_delta_fire_on_their_own_dates(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    account = _account(session, user, starting="20000")
    _seed_income(session, user, account)
    headers = _headers(user)
    _create(
        client,
        headers,
        "sc-sal0001",
        "Raise",
        [
            {
                "id": "ev-raise01",
                "type": "salary_change",
                "date": "2026-09-10",
                "monthly_delta_cad": "500.00",
            }
        ],
    )

    payload = _run(client, headers, scenario_ids="sc-sal0001")
    # The day BEFORE the change: identical.
    assert _gap(payload, "Raise", "2026-09-09") == D("0")
    # ON the change date it moves by exactly the delta…
    assert _gap(payload, "Raise", "2026-09-10") == D("500.00")
    # …and nothing happens on the 15th (the baseline's own anchor).
    assert _gap(payload, "Raise", "2026-10-09") == D("500.00")
    # A month later, on ITS OWN day-of-month, again.
    assert _gap(payload, "Raise", "2026-10-10") == D("1000.00")


def test_recurring_cost_delta_repeats_then_stops_at_end_date(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    _account(session, user, starting="8000")
    headers = _headers(user)
    _create(
        client,
        headers,
        "sc-cost001",
        "Child",
        [
            {
                "id": "ev-child01",
                "type": "recurring_cost_delta",
                "start_date": "2026-07-05",
                "monthly_amount_cad": "-900.00",
                "end_date": "2026-09-05",
            }
        ],
    )

    payload = _run(client, headers, scenario_ids="sc-cost001")
    assert _gap(payload, "Child", "2026-07-04") == D("0")
    assert _gap(payload, "Child", "2026-07-05") == D("-900.00")
    assert _gap(payload, "Child", "2026-08-05") == D("-1800.00")
    # end_date is INCLUSIVE, so September is charged too — and then it stops.
    assert _gap(payload, "Child", "2026-09-05") == D("-2700.00")
    assert _gap(payload, "Child", "2026-10-06") == D("-2700.00")


# ---------------------------------------------------------------------- F6


def test_one_off_cash_event_moves_exactly_one_point(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    _account(session, user, starting="15000")
    headers = _headers(user)
    _create(
        client,
        headers,
        "sc-one0001",
        "Windfall",
        [
            {
                "id": "ev-wind001",
                "type": "one_off_cash_event",
                "date": "2026-08-20",
                "amount_cad": "-25000.00",
            }
        ],
    )

    payload = _run(client, headers, scenario_ids="sc-one0001")
    assert _gap(payload, "Windfall", "2026-08-19") == D("0")
    assert _gap(payload, "Windfall", "2026-08-20") == D("-25000.00")
    # It never repeats — the gap is flat afterwards.
    assert _gap(payload, "Windfall", "2026-09-30") == D("-25000.00")
    assert _gap(payload, "Windfall", "2027-03-15") == D("-25000.00")


# ---------------------------------------------------------------------- F7


def test_two_interleaved_events_both_apply(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    """A raise that lands just before a career break: the raise sticks, the
    break then suppresses it (and the baseline income) until it ends."""
    user = _tax_user(session)
    account = _account(session, user, starting="12000")
    _seed_income(session, user, account)
    headers = _headers(user)
    _create(
        client,
        headers,
        "sc-int0001",
        "Interleaved",
        [
            {
                "id": "ev-raise02",
                "type": "salary_change",
                "date": "2026-09-10",
                "monthly_delta_cad": "500.00",
            },
            {
                "id": "ev-break02",
                "type": "career_break",
                "start_date": "2026-09-15",
                "end_date": "2026-11-30",
            },
        ],
    )

    payload = _run(client, headers, scenario_ids="sc-int0001")
    residual_income = D(payload["inputs"]["residual_income_monthly_cad"])

    # The raise lands five days before the break starts.
    assert _gap(payload, "Interleaved", "2026-09-14") == D("500.00")
    # Three anchors sit inside the break (09-15, 10-15, 11-15), so the line is
    # 500 up from the raise but down three suppressed income postings…
    assert _gap(payload, "Interleaved", "2026-11-30") == D("500.00") - residual_income * 3
    # …and the 10-10 raise occurrence is suppressed WITHIN the break, so it
    # adds nothing there.
    assert _gap(payload, "Interleaved", "2026-10-10") == _gap(payload, "Interleaved", "2026-09-20")
    # After the break ends the raise resumes: another +500 on 12-10.
    assert _gap(payload, "Interleaved", "2026-12-10") - _gap(
        payload, "Interleaved", "2026-12-09"
    ) == D("500.00")


# ---------------------------------------------------------------------- F8


def test_empty_single_and_negative_balance_tenants_all_project(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    # (a) no accounts, no transactions at all
    empty = _tax_user(session)
    payload = _run(client, _headers(empty))
    assert payload["inputs"]["net_worth_cad"] == "0.00"
    assert payload["buckets"][0] == str(TODAY)
    assert set(payload["baseline"]["values"]) == {"0.00"}
    assert payload["scenarios"] == []

    # (b) a single account
    single = _tax_user(session)
    _account(session, single, starting="42.00")
    payload = _run(client, _headers(single))
    assert payload["inputs"]["net_worth_cad"] == "42.00"
    assert payload["baseline"]["values"][0] == "42.00"

    # (c) a negative (debt) line projects as a negative start
    debtor = _tax_user(session)
    _account(session, debtor, name="Card", kind=AccountType.credit_card, starting="-5000")
    payload = _run(client, _headers(debtor))
    assert payload["inputs"]["cash_cad"] == "-5000.00"
    assert payload["baseline"]["values"][0] == "-5000.00"


# ---------------------------------------------------------------------- F9


def test_real_dollars_apply_exactly_one_constant_rate(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    _account(session, user, starting="1000")
    headers = _headers(user)

    flat = _run(client, headers, inflation_pct="0")
    assert flat["baseline"]["real_values"] == flat["baseline"]["values"]

    inflated = _run(client, headers, inflation_pct="2")
    # Day zero: no time has passed, so today's dollars == future dollars…
    assert inflated["baseline"]["real_values"][0] == inflated["baseline"]["values"][0]
    # …and later buckets are deflated by exactly one 2% constant.
    assert inflated["baseline"]["real_values"][-1] != inflated["baseline"]["values"][-1]
    assert D(inflated["baseline"]["real_values"][-1]) < D(inflated["baseline"]["values"][-1])
    # The rate is echoed so the UI can state it rather than imply it.
    assert inflated["inflation_pct"] == "2.00"


# --------------------------------------------------------------------- F10


def test_bounds_and_unknown_event_types_answer_the_pinned_envelopes(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    headers = _headers(user)
    _create(client, headers, "sc-bnd0001", "Bounds", [_house()])

    bad = [
        {"horizon_months": "121"},
        {"inflation_pct": "21"},
        {"annual_return_pct": "51"},
        {"flow_window_months": "37"},
        {"granularity": "year"},
        {"scenario_ids": ",".join(f"sc-{i:05d}" for i in range(7))},
    ]
    for params in bad:
        response = client.get(RUN, params=params, headers=headers)
        assert response.status_code == 422, (params, response.text)
        assert response.json()["code"] == "validation_error", params

    # An event type outside the closed set gets ITS OWN code, not the generic.
    unknown = client.post(
        SCENARIOS,
        json={
            "id": "sc-badtype1",
            "name": "Nope",
            "events": [{"id": "ev-astro01", "type": "astrology", "date": "2027-01-01"}],
        },
        headers=headers,
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "unknown_event_type"

    # Thirteen events is over the per-scenario cap.
    too_many = client.post(
        SCENARIOS,
        json={
            "id": "sc-many001",
            "name": "Many",
            "events": [
                {
                    "id": f"ev-many{i:03d}",
                    "type": "one_off_cash_event",
                    "date": "2027-01-01",
                    "amount_cad": "1.00",
                }
                for i in range(13)
            ],
        },
        headers=headers,
    )
    assert too_many.status_code == 422
    assert too_many.json()["code"] == "validation_error"

    # A bound that is simply wrong (down payment ≥ price) is refused too.
    broken = client.post(
        SCENARIOS,
        json={
            "id": "sc-brkprice",
            "name": "Broken",
            "events": [{**_house("ev-bad0001"), "down_payment_cad": "500000"}],
        },
        headers=headers,
    )
    assert broken.status_code == 422
    assert broken.json()["code"] == "validation_error"

    # And an unknown saved id at run time is a 404, not a 500.
    missing = client.get(RUN, params={"scenario_ids": "sc-gone0001"}, headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "scenario_not_found"


# --------------------------------------------------------------------- F11


def test_scenarios_are_tenant_scoped(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    alice = _tax_user(session)
    bob = _tax_user(session)
    alice_h, bob_h = _headers(alice), _headers(bob)
    _create(client, alice_h, "sc-alice001", "Alice's plan", [_house()])

    # Bob sees an empty list…
    listing = client.get(SCENARIOS, headers=bob_h).json()
    assert listing == {"items": [], "count": 0}
    # …and Alice's id is invisible to him: same 404 as a non-existent one.
    assert client.get(f"{SCENARIOS}/sc-alice001", headers=bob_h).status_code == 404
    run = client.get(RUN, params={"scenario_ids": "sc-alice001"}, headers=bob_h)
    assert run.status_code == 404
    assert run.json()["code"] == "scenario_not_found"
    # Alice still has hers.
    assert client.get(SCENARIOS, headers=alice_h).json()["count"] == 1


# --------------------------------------------------------------------- F12


def test_demo_tenant_cannot_write_but_can_read(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    from sqlmodel import select

    demo = session.exec(select(User).where(User.email == DEMO_EMAIL)).first()
    if demo is None:
        demo = User(email=DEMO_EMAIL, hashed_password="x")
        session.add(demo)
        session.commit()
        session.refresh(demo)
    headers = _headers(demo)

    refused = client.post(
        SCENARIOS,
        json={"id": "sc-demo0001", "name": "Nope", "events": []},
        headers=headers,
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "demo_account_restricted"

    # Reads stay open — the demo is FOR reading — with nothing saved.
    assert client.get(SCENARIOS, headers=headers).json()["count"] == 0
    payload = _run(client, headers)
    assert payload["scenarios"] == []
    assert payload["disclaimer"] == "Projection, not a prediction."


# --------------------------------------------------------------------- F13


def test_export_carries_the_scenarios(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    user = _tax_user(session)
    headers = _headers(user)
    _create(client, headers, "sc-exp0001", "Exported", [_house()])

    response = client.get(f"{API}/export/full", headers=headers)
    assert response.status_code == 200, response.text
    snapshot = response.json()
    # Derived table set (docs/11 §File list): a JSON column on `users` travels
    # inside the users row with no registry change.
    assert snapshot["users"][0]["forecast_scenarios"][0]["id"] == "sc-exp0001"
    assert snapshot["users"][0]["forecast_scenarios"][0]["events"][0]["type"] == "house_purchase"


# --------------------------------------------------------------------- F14


def test_partner_surface_carries_no_forecast_data(
    client: TestClient, session: Session, frozen_today: date
) -> None:
    from test_household_sharing import _accept, _invite, _register
    from test_household_sharing import _headers as _share_headers

    owner_email = "fc-owner@example.com"
    partner_email = "fc-partner@example.com"
    owner_token = _register(client, owner_email)["access_token"]
    partner_token = _register(client, partner_email)["access_token"]

    # The owner saves a forecast…
    owner_headers = _share_headers(owner_token)
    _create(client, owner_headers, "sc-partner1", "Owner decision", [_house()])

    # …and shares the household with the partner.
    invite = _invite(client, owner_token, partner_email)
    assert _accept(client, invite["token"], partner_token).status_code == 200

    partner_headers = _share_headers(partner_token)
    endpoints = ["/state", "/accounts", "/transactions", "/budgets", "/categories"]
    for path in endpoints:
        response = client.get(f"{API}/partner{path}", headers=partner_headers)
        assert response.status_code in (200, 404), (path, response.text)
        blob = response.text.lower()
        assert "forecast" not in blob, f"{path} leaked forecast data"
        assert "sc-partner1" not in response.text, f"{path} leaked a scenario id"

    # The owner's own forecast is untouched by sharing.
    assert client.get(SCENARIOS, headers=owner_headers).json()["count"] == 1
