"""GET /api/v1/fx/board contract (docs/15)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.fx as fx_service
from app.models.fx_rates import FxRate, FxSource


def _auth(client: TestClient) -> dict[str, str]:
    email = f"{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    body = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _seed(session: Session, base: str, rate: str, day: date) -> None:
    session.add(
        FxRate(
            base=base,
            quote="CAD",
            date=day,
            rate=Decimal(rate),
            source=FxSource.boc_valet,
        )
    )
    session.commit()


#: M151 — the board's membership written as a LITERAL, so neither the
#: constant nor the assertion can be edited to make the other one pass.
#: Membership rule (docs/07 §Curated board membership): BoC publishes a live
#: ``FX{base}CAD`` daily series for every member. Live-probed 2026-10-11.
CURATED_BOARD_BASES: tuple[str, ...] = (
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CHF",
    "CNY",
    "AUD",
    "NZD",
    "HKD",
    "SGD",
    "INR",
    "MXN",
    "BRL",
    "ZAR",
    "KRW",
    "NOK",
    "SEK",
)


def test_the_curated_set_is_exactly_the_live_valet_catalog_m151() -> None:
    """DKK is out of the board, and the set is 17 live series.

    The defect this pins: M150's first live tick found ``DKK`` in
    ``CURATED_FX_BASES`` while BoC publishes no ``FXDKKCAD`` series (404),
    so ``pairs_behind`` reported it behind forever — the refresh spent a
    call and a warning on a 404 every tick, for a row ``GET /fx/board``
    could never render anyway. The fix is the membership rule, not a
    special case: a base whose series does not exist is not on the board.
    """
    assert fx_service.CURATED_FX_BASES == CURATED_BOARD_BASES
    assert len(fx_service.CURATED_FX_BASES) == 17
    assert "DKK" not in fx_service.CURATED_FX_BASES
    # The refresh derives its series name as ``FX{base}CAD``, so a member
    # must be a bare uppercase 3-letter code (docs/07, docs/15).
    assert all(base.isalpha() and base.isupper() and len(base) == 3 for base in CURATED_BOARD_BASES)
    # CAD is excluded on purpose: its rate against itself is exactly 1.00.
    assert "CAD" not in fx_service.CURATED_FX_BASES


def test_board_never_returns_a_row_for_a_currency_bo_cannot_price_m151(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """A cached/manual DKK row does not resurrect the 18th board row (M151).

    The board iterates the curated set, never the cache, so a manual
    ``POST /fx`` gap-fill a DKK holder made still cannot advertise DKK as a
    currency the board tracks — BoC publishes no series, so no tick would
    ever refresh it. The rate itself stays in ``fx_rates`` and keeps
    converting; it just never becomes a board row.
    """
    # Force privacy-locked so no BoC fetch is attempted (deterministic, no network).
    monkeypatch.setattr(fx_service, "privacy_locked", lambda: True)
    session.add(
        FxRate(
            base="DKK",
            quote="CAD",
            date=datetime.now(UTC).date(),
            rate=Decimal("4.9000"),
            source=FxSource.manual,
        )
    )
    session.commit()

    response = client.get("/api/v1/fx/board", headers=_auth(client))
    assert response.status_code == 200
    bases = [c["base"] for c in response.json()["currencies"]]

    assert len(bases) == len(CURATED_BOARD_BASES)
    assert "DKK" not in bases


def test_board_returns_currated_currencies(
    client: TestClient, session: Session, monkeypatch
) -> None:
    # Force privacy-locked so no BoC fetch is attempted (deterministic, no network).
    monkeypatch.setattr(fx_service, "privacy_locked", lambda: True)
    today = datetime.now(UTC).date()
    _seed(session, "USD", "1.37", today)
    _seed(session, "EUR", "1.47", today)

    response = client.get("/api/v1/fx/board", headers=_auth(client))
    assert response.status_code == 200
    body = response.json()
    currencies = {c["base"]: c for c in body["currencies"]}

    # Every curated base is present; CAD is intentionally excluded.
    assert len(body["currencies"]) == len(fx_service.CURATED_FX_BASES)
    assert "CAD" not in currencies
    assert "USD" in currencies and "EUR" in currencies

    usd = currencies["USD"]
    assert Decimal(usd["latest_rate"]) == Decimal("1.37")
    assert usd["as_of"] == today.isoformat()
    assert len(usd["series"]) == 1
    assert Decimal(usd["series"][0]["rate"]) == Decimal("1.37")
    assert usd["series"][0]["date"] == today.isoformat()


def test_board_privacy_degrade_nulls(client: TestClient, session: Session, monkeypatch) -> None:
    # Privacy-locked with nothing cached → graceful nulls, never a 5xx/403.
    monkeypatch.setattr(fx_service, "privacy_locked", lambda: True)
    response = client.get("/api/v1/fx/board", headers=_auth(client))
    assert response.status_code == 200
    body = response.json()
    currencies = {c["base"]: c for c in body["currencies"]}

    for base in ("USD", "GBP", "JPY"):
        assert currencies[base]["latest_rate"] is None
        assert currencies[base]["as_of"] is None
        assert currencies[base]["series"] == []


def test_board_window_filters_dates(client: TestClient, session: Session, monkeypatch) -> None:
    monkeypatch.setattr(fx_service, "privacy_locked", lambda: True)
    today = datetime.now(UTC).date()
    start = today - timedelta(days=30)
    _seed(session, "USD", "1.35", start)
    _seed(session, "USD", "1.36", today)

    response = client.get("/api/v1/fx/board", params={"days": 30}, headers=_auth(client))
    assert response.status_code == 200
    body = response.json()
    usd = next(c for c in body["currencies"] if c["base"] == "USD")
    dates = [date.fromisoformat(d["date"]) for d in usd["series"]]
    assert dates  # non-empty
    assert all(d >= start for d in dates)
    assert all(d <= today for d in dates)
