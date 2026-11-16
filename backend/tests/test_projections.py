"""Investment projections backend contract (M15, docs/12): pure monthly-
compounding math pinned by hand-computed cases, an independent monthly
summation cross-check, 422 validation envelopes, and a tenancy-scoped
`/defaults` prefill over real seeded positions.

Runs on the slice-private fintrack_m15(_test) database (see conftest).
"""

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from json import loads as _loads
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import app.services.market_data as market_data
from app.models.prices import Price
from app.services.projections import project_scenario

API = "/api/v1"
RUN_URL = f"{API}/projections/run"
DEFAULTS_URL = f"{API}/projections/defaults"

# The M65 golden vectors: 7 requests whose responses were produced by THIS
# implementation (`run_projections` → `project_scenario`) and frozen in
# frontend/tests/fixtures/projection-parity.json, where the client engine's
# parity suite (`frontend/tests/projectionEngine.test.ts`) reads them. M135
# §4.3 names exactly this fixture — "7/7 cases, 0 diffs" — as the gate before
# the panel stops calling the client engine, and M135 §4.1 forbids
# re-capturing a golden to satisfy it. Reading it from the backend is what
# makes the route itself the pin: the same file now guards BOTH directions.
_M65_GOLDENS = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "tests"
    / "fixtures"
    / "projection-parity.json"
)

CENTS = Decimal("0.01")


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@pytest.fixture(autouse=True)
def fake_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline discipline (docs/07): valuation's network seam resolves to the
    newest MANUAL cached close instead of yfinance."""

    def fake_ensure(session, security, today, **_kwargs):
        return session.exec(
            select(Price)
            .where(Price.security_id == security.id, Price.date <= today)
            .order_by(Price.date.desc())
        ).first()

    def fake_backfill(session, security, start_date, end_date, **_kwargs):
        return None

    monkeypatch.setattr(market_data, "ensure_recent_price", fake_ensure)
    monkeypatch.setattr(market_data, "backfill_range", fake_backfill)


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m15-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _independent_monthly_summation(
    start_value: str,
    annual_contribution: str,
    annual_return_pct: str,
    years: int,
    inflation_pct: str,
) -> list[Decimal]:
    """Cross-check written independently of project_scenario: one accumulator
    stepped month by month (growth first, THEN month-end contribution), read
    at every 12th month and deflated by repeated multiplication."""
    monthly_rate = (Decimal("1") + Decimal(annual_return_pct) / Decimal("100")) ** (
        Decimal(1) / Decimal(12)
    ) - Decimal("1")
    monthly_contribution = Decimal(annual_contribution) / Decimal("12")

    value = Decimal(start_value)
    expected: list[Decimal] = []
    deflator = Decimal("1")
    for _year in range(years):
        for _month in range(12):
            value *= Decimal("1") + monthly_rate
            value += monthly_contribution
        deflator *= Decimal("1") + Decimal(inflation_pct) / Decimal("100")
        expected.append(value / deflator)
    return [_cents(v) for v in expected]


# --- pure projector: hand-computed math ----------------------------------------


def test_one_year_twelve_percent_no_contribution_is_exactly_11200() -> None:
    rows = project_scenario(
        start_value=Decimal("10000"),
        annual_contribution=Decimal("0"),
        annual_return_pct=Decimal("12"),
        years=1,
        inflation_pct=Decimal("0"),
    )
    assert len(rows) == 1
    assert rows[0]["year"] == 1
    assert rows[0]["value_cad"] == "11200.00"
    # Start value is NOT counted as contributed.
    assert rows[0]["contributed_cad"] == "0.00"
    # Zero inflation → real equals nominal.
    assert rows[0]["value_real_cad"] == "11200.00"


def test_two_years_with_contribution_matches_independent_summation() -> None:
    rows = project_scenario(
        start_value=Decimal("10000"),
        annual_contribution=Decimal("1200"),
        annual_return_pct=Decimal("12"),
        years=2,
        inflation_pct=Decimal("2"),
    )
    assert [row["year"] for row in rows] == [1, 2]
    assert [row["value_real_cad"] for row in rows] == [
        str(v) for v in _independent_monthly_summation("10000", "1200", "12", 2, "2")
    ]
    # Cumulative contributions exclude the start value.
    assert rows[0]["contributed_cad"] == "1200.00"
    assert rows[1]["contributed_cad"] == "2400.00"


def test_deflation_year_one_real_is_nominal_over_one_point_zero_two() -> None:
    rows = project_scenario(
        start_value=Decimal("10000"),
        annual_contribution=Decimal("0"),
        annual_return_pct=Decimal("0"),
        years=1,
        inflation_pct=Decimal("2"),
    )
    nominal = Decimal(rows[0]["value_cad"])
    expected = _cents(nominal / Decimal("1.02"))
    assert rows[0]["value_real_cad"] == str(expected)


def test_negative_return_declines_monotonically() -> None:
    rows = project_scenario(
        start_value=Decimal("10000"),
        annual_contribution=Decimal("0"),
        annual_return_pct=Decimal("-20"),
        years=5,
        inflation_pct=Decimal("0"),
    )
    values = [Decimal(row["value_cad"]) for row in rows]
    assert values == sorted(values, reverse=True)
    assert all(v < Decimal("10000") for v in values)


# --- POST /projections/run ------------------------------------------------------


def test_run_endpoint_payload_shape_and_totals(client: TestClient) -> None:
    headers = _auth(client)
    body = client.post(
        RUN_URL,
        json={
            "start_value_cad": "10000",
            "annual_contribution_cad": "1200",
            "years": 2,
            "inflation_pct": "0",
            "scenarios": [{"label": "Balanced", "annual_return_pct": "6.5"}],
        },
        headers=headers,
    )
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["start_value_cad"] == "10000.00"
    assert payload["years"] == 2
    assert payload["inflation_pct"] == "0.00"
    scenario = payload["scenarios"][0]
    assert scenario["label"] == "Balanced"
    assert len(scenario["series"]) == 2
    assert set(scenario["series"][0]) == {
        "year",
        "contributed_cad",
        "value_cad",
        "value_real_cad",
    }
    final = Decimal(scenario["final_value_cad"])
    contributed = Decimal(scenario["total_contributed_cad"])
    gain = Decimal(scenario["total_gain_cad"])
    assert final == Decimal(scenario["series"][-1]["value_cad"])
    assert contributed == Decimal("2400.00")
    assert gain == final - Decimal("10000.00") - contributed


def test_run_multi_scenarios_are_independent(client: TestClient) -> None:
    headers = _auth(client)
    scenarios = [
        {"label": "Conservative", "annual_return_pct": "4"},
        {"label": "Aggressive", "annual_return_pct": "9"},
    ]
    body = client.post(
        RUN_URL,
        json={
            "start_value_cad": "5000",
            "annual_contribution_cad": "600",
            "years": 10,
            "inflation_pct": "2",
            "scenarios": scenarios,
        },
        headers=headers,
    )
    assert body.status_code == 200, body.text
    combined = {s["label"]: s for s in body.json()["scenarios"]}
    assert Decimal(combined["Aggressive"]["final_value_cad"]) > Decimal(
        combined["Conservative"]["final_value_cad"]
    )
    # Each scenario must equal its own single-scenario run (no shared state).
    for scenario in scenarios:
        solo = client.post(
            RUN_URL,
            json={
                "start_value_cad": "5000",
                "annual_contribution_cad": "600",
                "years": 10,
                "inflation_pct": "2",
                "scenarios": [scenario],
            },
            headers=headers,
        ).json()["scenarios"][0]
        assert combined[scenario["label"]] == solo


@pytest.mark.parametrize(
    ("overrides", "case"),
    [
        ({"years": 0}, "years below bound"),
        ({"years": 51}, "years above bound"),
        ({"scenarios": []}, "empty scenarios"),
        (
            {"scenarios": [{"label": f"s{i}", "annual_return_pct": "5"} for i in range(9)]},
            "nine scenarios",
        ),
        (
            {"scenarios": [{"label": "x", "annual_return_pct": "51"}]},
            "return above bound",
        ),
        (
            {"scenarios": [{"label": "x", "annual_return_pct": "-51"}]},
            "return below bound",
        ),
        (
            {"scenarios": [{"label": "x" * 41, "annual_return_pct": "5"}]},
            "label too long",
        ),
        (
            {"scenarios": [{"label": "", "annual_return_pct": "5"}]},
            "label empty",
        ),
        ({"start_value_cad": "-1"}, "negative start value"),
        ({"inflation_pct": "20.01"}, "inflation above bound"),
    ],
)
def test_run_validation_envelopes(client: TestClient, overrides: dict, case: str) -> None:
    headers = _auth(client)
    payload = {
        "start_value_cad": "10000",
        "annual_contribution_cad": "0",
        "years": 5,
        "inflation_pct": "2",
        "scenarios": [{"label": "Balanced", "annual_return_pct": "6.5"}],
    }
    payload.update(overrides)
    body = client.post(RUN_URL, json=payload, headers=headers)
    assert body.status_code == 422, f"{case}: {body.text}"
    envelope = body.json()
    # Structured validation errors (errors.py): detail is a list of error dicts.
    assert envelope["code"] == "validation_error"
    assert isinstance(envelope["detail"], list)
    assert len(envelope["detail"]) >= 1


# --- M65 golden parity (M135 §4.3 gate; M157) ---------------------------------


def _m65_goldens() -> list[dict]:
    """The frozen M65 vectors — read, never re-derived."""
    assert _M65_GOLDENS.exists(), f"the frozen M65 golden fixture is missing: {_M65_GOLDENS}"
    cases = _loads(_M65_GOLDENS.read_text())
    assert len(cases) == 7, f"M135 §4.2 re-derives 7 projection cases, found {len(cases)}"
    return cases


@pytest.mark.parametrize(
    "golden",
    _m65_goldens(),
    ids=[case["name"] for case in _m65_goldens()],
)
def test_run_matches_the_frozen_m65_golden(client: TestClient, golden: dict) -> None:
    """7/7 frozen goldens reproduce byte-identically through the route (M135 §4.3).

    This is the gate the M135 restore plan names for K-12: the server response
    for every frozen case, 0 diffs, before the panel stops calling the client
    engine. `assert ==` on the parsed JSON is the byte comparison — a Decimal
    that rounds differently, a re-ordered key or a widened bound all land here.
    """
    headers = _auth(client)
    response = client.post(RUN_URL, json=golden["request"], headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == golden["response"]


def test_run_validation_envelope_is_byte_identical(client: TestClient) -> None:
    """The 422 envelope is a CONTRACT, not a courtesy (M157 Do #4).

    The client engine throws `ProjectionInputError` with `code:
    "validation_error"` for the same bound, and the parity harness pins the
    server's pydantic-shaped `detail` list for `POST /rules/preview` — so the
    projections envelope must not drift either. `detail` is a LIST of
    pydantic error dicts (errors.py), never a string, and the code is exactly
    `validation_error`.
    """
    headers = _auth(client)
    response = client.post(
        RUN_URL,
        json={
            "start_value_cad": "10000",
            "annual_contribution_cad": "0",
            "years": 51,
            "inflation_pct": "2",
            "scenarios": [{"label": "Balanced", "annual_return_pct": "6.5"}],
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "less_than_equal",
                "loc": ["body", "years"],
                "msg": "Input should be less than or equal to 50",
                "input": 51,
                "ctx": {"le": 50},
            }
        ],
        "code": "validation_error",
    }, response.text


# --- GET /projections/defaults ---------------------------------------------------


def _seed_holding(
    client: TestClient,
    headers: dict[str, str],
    symbol: str,
    name: str,
    buy_price: str,
    current_price: str | None,
    quantity: str = "1",
) -> None:
    account = client.post(
        f"{API}/accounts",
        json={"name": f"Holdings {symbol}", "type": "brokerage_non_registered", "currency": "CAD"},
        headers=headers,
    ).json()
    security = client.post(
        f"{API}/securities",
        json={
            "symbol": symbol,
            "name": name,
            "kind": "stock",
            "listing_currency": "CAD",
            "exchange": "TSX",
        },
        headers=headers,
    ).json()
    trade = client.post(
        f"{API}/trades",
        json={
            "account_id": account["id"],
            "security_id": security["id"],
            "action": "buy",
            "date": date.today().isoformat(),
            "quantity": quantity,
            "price_native": buy_price,
        },
        headers=headers,
    )
    assert trade.status_code == 201, trade.text
    if current_price is not None:
        price = client.post(
            f"{API}/prices",
            json={
                "security_id": security["id"],
                "date": date.today().isoformat(),
                "price": current_price,
            },
            headers=headers,
        )
        assert price.status_code == 201, price.text


def test_defaults_null_for_fresh_user(client: TestClient) -> None:
    headers = _auth(client)
    body = client.get(DEFAULTS_URL, headers=headers)
    assert body.status_code == 200, body.text
    assert body.json() == {"start_value_cad": None}


def test_defaults_sums_seeded_position_market_values(client: TestClient) -> None:
    headers = _auth(client)
    _seed_holding(client, headers, "XEQ", "iShares Core Equity", "50", "60", quantity="2")
    _seed_holding(client, headers, "RY", "Royal Bank", "100", "25.50")
    body = client.get(DEFAULTS_URL, headers=headers)
    assert body.json() == {"start_value_cad": "145.50"}


def test_defaults_tenancy_scoped(client: TestClient) -> None:
    owner_headers = _auth(client)
    _seed_holding(client, owner_headers, "XEQ", "iShares Core Equity", "50", "60", quantity="2")

    other_headers = _auth(client)
    _seed_holding(client, other_headers, "OTHER", "Other User Holding", "10", "999")

    owner = client.get(DEFAULTS_URL, headers=owner_headers).json()
    other = client.get(DEFAULTS_URL, headers=other_headers).json()
    assert owner == {"start_value_cad": "120.00"}
    assert other == {"start_value_cad": "999.00"}


def test_project_scenario_is_deterministic() -> None:
    """Same inputs yield byte-identical output (no randomness) — pinned so a
    future refactor can't silently introduce nondeterminism."""
    args = (Decimal("10000.00"), Decimal("1200.00"), Decimal("7.00"), 30, Decimal("2.00"))
    assert project_scenario(*args) == project_scenario(*args)
