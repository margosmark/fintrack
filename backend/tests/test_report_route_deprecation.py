"""M137 P1-1: the orphan `/reports/*` routes behind a flag (docs/08,
docs/33 §Server-first computing).

Both states are the contract: flag OFF (the default, and what a deployment
that never sets it gets) the routes must still RESOLVE — this card deprecates
them, it does not remove them — and flag ON each answers 410 with a body that
names it. The routes the UI consumes must be unaffected in BOTH states.

M160-R2 (t_85311c34) moved three of the four orphans OUT of the flag: the
Dividends / Sankey / Total-return panels read them now, so a live screen
route can never be gated. `/reports/networth-snapshot` is the one left, and
the three re-joined routes join the never-gated set below.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models.users import User

PASSWORD = "report-deprecation-pass-1"
API = "/api/v1"

#: The one zero-consumer route left + the params it needs to be asked, keyed
#: by the ROUTE (not the request URL) so the 410 body can be matched against
#: the key verbatim.
ORPHANS: dict[str, dict[str, str]] = {
    "/reports/networth-snapshot": {"target_date": "2026-06-30"},
}

#: The routes the screens actually consume (M134 §3.6.2's seven, plus the
#: three M160-R2 re-pointed) — never gated.
REPOINTED: dict[str, dict[str, str]] = {
    "/reports/dividends": {},
    "/reports/cashflow-sankey": {"from": "2026-01-01", "to": "2026-12-31"},
    "/reports/total-return": {"from": "2026-01-01", "to": "2026-12-31"},
    "/reports/spending": {"from": "2026-01-01", "to": "2026-12-31"},
    "/reports/spending/categories": {"from": "2026-01-01", "to": "2026-12-31"},
    "/reports/category-tag-matrix": {"from": "2026-01-01", "to": "2026-12-31"},
    "/reports/cashflow": {"from": "2026-01-01", "to": "2026-12-31"},
    "/reports/net-worth": {"from": "2026-01-01", "to": "2026-12-31"},
    "/reports/investment-performance": {"from": "2026-01-01", "to": "2026-12-31"},
    "/reports/capital-gains": {"year": "2026"},
}


def _register(client: TestClient, session: Session) -> tuple[dict, int]:
    email = f"deprecation-{uuid.uuid4().hex[:12]}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    body = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = session.exec(select(User).where(User.email == email)).one().id
    return headers, int(user_id)


@pytest.fixture(name="deprecated_flag")
def deprecated_flag_fixture():
    """Set/unset SERVER_REPORTS_DEPRECATED; clears the settings cache both ways.

    The flag is read LIVE inside the handler (AGENTS §5), so flipping the env
    and clearing `get_settings`' cache is the whole switch.
    """

    def _set(value: str | None) -> None:
        if value is None:
            os.environ.pop("SERVER_REPORTS_DEPRECATED", None)
        else:
            os.environ["SERVER_REPORTS_DEPRECATED"] = value
        get_settings.cache_clear()

    _set(None)
    try:
        yield _set
    finally:
        _set(None)


def test_flag_off_is_the_default_and_every_orphan_still_resolves(
    client: TestClient, session: Session, deprecated_flag
) -> None:
    """Deprecation without removal: OFF (never set) → healthy, not 404/410."""
    assert get_settings().server_reports_deprecated is False
    headers, _ = _register(client, session)
    for route, params in ORPHANS.items():
        resp = client.get(f"{API}{route}", params=params, headers=headers)
        assert resp.status_code == 200, f"{route}: {resp.status_code} {resp.text[:300]}"
        assert "report_route_deprecated" not in resp.text


def test_flag_on_410s_each_orphan_with_a_body_that_names_it(
    client: TestClient, session: Session, deprecated_flag
) -> None:
    deprecated_flag("1")
    assert get_settings().server_reports_deprecated is True
    headers, _ = _register(client, session)
    for route, params in ORPHANS.items():
        resp = client.get(f"{API}{route}", params=params, headers=headers)
        assert resp.status_code == 410, f"{route}: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        assert body["code"] == "report_route_deprecated"
        # The body names the route so an operator can act on it without logs.
        assert route in body["detail"]

    # …and back: the switch is reversible without a restart.
    deprecated_flag(None)
    for route, params in ORPHANS.items():
        assert client.get(f"{API}{route}", params=params, headers=headers).status_code == 200


def test_the_repointed_routes_are_never_gated(
    client: TestClient, session: Session, deprecated_flag
) -> None:
    """The screens depend on these — the flag must not reach them in EITHER
    state. P0-2's seven plus M160-R2's three."""
    headers, _ = _register(client, session)
    for state in (None, "1"):
        deprecated_flag(state)
        for route, params in REPOINTED.items():
            resp = client.get(f"{API}{route}", params=params, headers=headers)
            assert resp.status_code == 200, f"{state=} {route}: {resp.text[:300]}"
            assert "report_route_deprecated" not in resp.text


def test_a_repointed_route_cannot_be_gated_even_with_the_flag_on(
    deprecated_flag,
) -> None:
    """`assert_report_route_live` ignores anything outside the orphan allowlist
    — a typo (or a future caller) can never 410 a live screen route. The three
    M160-R2 re-points are named explicitly: they used to BE on that allowlist,
    and the whole point of the flip is that a deployment with the flag set
    still serves their panels."""
    from app.api._report_flags import assert_report_route_live

    deprecated_flag("1")
    assert_report_route_live("/reports/spending")
    assert_report_route_live("/reports/cashflow")
    assert_report_route_live("/reports/net-worth")
    assert_report_route_live("/reports/dividends")
    assert_report_route_live("/reports/cashflow-sankey")
    assert_report_route_live("/reports/total-return")
