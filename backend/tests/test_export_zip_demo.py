"""M75 acceptance on the REAL demo tenant — export is a read path (docs/11).

The portability promise says the export works on the demo tenant, because the
demo is the front door: a visitor who wants to see what "your data, any time"
looks like must be able to press the button. These tests seed the demo through
`POST /auth/demo` in a hermetic `<suite-db>_demo` database (the pattern
`tests/test_demo_seed.py` / `tests/test_cashflow_calendar_demo.py`
established) so committed demo rows never leak into the suite database the
other modules roll back against.

The mirror-image assertion is here too: reading is open, writing the schedule
is not (the shared demo write guard, `403 demo_account_restricted`).
"""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from conftest import drop_database_force
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from alembic import command
from app.core.config import get_settings

API = "/api/v1"
ZIP_URL = f"{API}/export/zip"
OFX_URL = f"{API}/export/ofx"
SCHEDULE_URL = f"{API}/export/schedule"

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _demo_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest disables lifespan seeding globally; this suite opts back in."""
    monkeypatch.setattr(get_settings(), "seed_demo_account", True)


@pytest.fixture(scope="module", autouse=True)
def _demo_database() -> Iterator[None]:
    """Hermetic `<suite-db>_demo` DB (named from the SUITE database so two
    agents can never drop each other's database mid-run)."""
    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack_test",
    )
    suite_db = base.rsplit("/", 1)[-1]
    demo_db = f"{suite_db}_demo"
    demo_url = base.rsplit("/", 1)[0] + f"/{demo_db}"
    admin = create_engine(base.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        drop_database_force(conn, demo_db)
        conn.execute(text(f'CREATE DATABASE "{demo_db}"'))
    admin.dispose()

    import app.api.deps as deps

    # From the swap onward everything runs under try/finally: a generator
    # fixture that raises BEFORE its `yield` never executes the code after
    # it, so the restore used to be skipped and every later test in the
    # session kept running against `<suite>_demo` (t_435c2d14).
    os.environ["DATABASE_URL"] = demo_url
    try:
        get_settings().seed_demo_account = True
        get_settings.cache_clear()
        deps._engine = None
        cfg = Config(str(_BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", demo_url)
        command.upgrade(cfg, "head")
        yield
    finally:
        os.environ["DATABASE_URL"] = base
        get_settings.cache_clear()
        deps._engine = None


def _demo_login(client: TestClient) -> dict[str, str]:
    login = client.post(f"{API}/auth/demo")
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_demo_zip_export_is_200_with_real_rows(client: TestClient) -> None:
    headers = _demo_login(client)

    response = client.get(ZIP_URL, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        transactions = list(
            csv.reader(io.StringIO(archive.read("transactions.csv").decode("utf-8")))
        )

    assert "manifest.json" in names and "transactions.csv" in names
    assert manifest["kind"] == "fintrack-export"
    assert manifest["visibility"] == "committed"
    assert manifest["user"]["email"] == "demo@fintracker.local"
    listed = {entry["name"]: entry["rows"] for entry in manifest["files"]}
    assert listed["transactions.csv"] == len(transactions) - 1

    # The demo is a real seeded household, and the ZIP agrees with the machine
    # snapshot row for row (a demo has nothing staged, so the populations are
    # identical by construction — the check is that neither is an empty box).
    snapshot = client.get(f"{API}/export/full", headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    committed_rows = len(snapshot.json()["transactions"])
    assert committed_rows > 0, "the demo export is empty"
    assert len(transactions) - 1 == committed_rows


def test_demo_ofx_export_is_200(client: TestClient) -> None:
    headers = _demo_login(client)
    accounts = client.get(f"{API}/accounts", headers=headers)
    assert accounts.status_code == 200, accounts.text
    items = accounts.json()["items"]
    assert items, "the demo seed has no accounts"
    account_id = items[0]["id"]

    response = client.get(OFX_URL, headers=headers, params={"account_id": account_id})
    assert response.status_code == 200, response.text
    body = response.content.decode("utf-8")
    assert body.startswith("OFXHEADER:100")
    assert "<STMTTRN>" in body, "the demo account has transactions to export"


def test_demo_can_read_but_not_write_the_export_schedule(client: TestClient) -> None:
    headers = _demo_login(client)

    read = client.get(SCHEDULE_URL, headers=headers)
    assert read.status_code == 200, read.text
    assert read.json() == {
        "frequency": "off",
        "hour": 3,
        "weekday": 0,
        "day_of_month": 1,
        "keep_last_n": 5,
    }

    write = client.put(SCHEDULE_URL, headers=headers, json=dict(read.json(), frequency="weekly"))
    assert write.status_code == 403, write.text
    assert write.json()["code"] == "demo_account_restricted"
