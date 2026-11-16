"""Pin the live BoC Valet endpoint (docs/07).

Regression for the W2-era bug: VALET_URL pointed at `/valet/rates/…`, which
returns 404 — fetch-on-miss silently failed for the deployment's entire
history and was masked by the (since-removed) demo seed rows. The data
endpoint is `/valet/observations/…` (live-probed 200 OK, 2026-09-24).
"""

from datetime import date
from decimal import Decimal

from app.services import fx


def test_valet_url_is_the_observations_endpoint() -> None:
    url = fx.VALET_URL.format(pair="USDCAD")
    assert url == "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
    assert "/valet/rates/" not in url


def test_fetch_valet_series_hits_observations_and_parses(monkeypatch) -> None:
    seen: dict = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "observations": [
                    {"d": "2026-09-22", "FXUSDCAD": {"v": "1.3501"}},
                    {"d": "2026-09-23", "FXUSDCAD": {"v": None}},  # holiday: skipped
                ]
            }

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(fx.requests, "get", fake_get)

    rates = fx.fetch_valet_series("USD", "CAD", date(2026, 9, 22), date(2026, 9, 23))

    assert seen["url"] == fx.VALET_URL.format(pair="USDCAD")
    assert seen["params"] == {
        "start_date": "2026-09-22",
        "end_date": "2026-09-23",
    }
    assert rates == {date(2026, 9, 22): Decimal("1.3501")}


def test_fetch_retries_use_the_same_url(monkeypatch) -> None:
    """A 404 at the observations endpoint would surface as ValetUnavailable;
    ensure every retry attempt targets the same (correct) URL."""
    urls: list[str] = []

    class Fake404:
        status_code = 404

        def raise_for_status(self) -> None:
            import requests

            raise requests.HTTPError("404 Client Error")

    def fake_get(url, params=None, timeout=None):
        urls.append(url)
        return Fake404()

    monkeypatch.setattr(fx.requests, "get", fake_get)
    monkeypatch.setattr(fx.time, "sleep", lambda _s: None)  # no backoff waits in tests

    try:
        fx.fetch_valet_series("USD", "CAD", date(2026, 9, 22), date(2026, 9, 23))
    except fx.ValetUnavailable:
        pass

    assert len(urls) == fx.MAX_RETRIES + 1
    assert all(u == fx.VALET_URL.format(pair="USDCAD") for u in urls)
