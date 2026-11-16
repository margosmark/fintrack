"""M82 — per-institution brokerage CSV profiles (docs/41).

Every profile is exercised the same way, end to end against the running
application stack's own routes:

    dry run (zero writes) → stage → commit → recompute positions and cash
    effect FROM THE DATABASE → compare with the fixture's own stated totals
    (its ``.expected.json`` sidecar) → re-import and prove net-new is 0.

The sidecar is the authority: it is the fixture's *stated* result, so a parser
change that silently alters a trade, a sign, a commission or a currency shows
up here as a number mismatch rather than as a plausible-looking pass.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.importers.brokerage_profiles import (
    BROKERAGE_PROFILES,
    PROFILE_SLUGS,
    detect_profile,
)
from app.importers.investment_profile import parse_investment_csv
from app.models.accounts import Account
from app.models.fx_rates import FxRate, FxSource
from app.models.import_batches import ImportBatch
from app.models.securities import Security
from app.models.trades import Trade
from app.services import market_data
from app.services.total_return import _trade_cash_effect

API = "/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "brokerage"
APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: One fixture per profile (plus the French NBDB twin) — the 9 files that carry
#: a sidecar. The 4 without one are refusal fixtures (bad date, bad number and
#: the two corporate-action files) and are asserted as 422s below.
SMOKE_FILES: tuple[str, ...] = (
    "questrade_activity.csv",
    "wealthsimple_activity.csv",
    "ibkr_flex.csv",
    "ibkr_statement.csv",
    "fidelity_activity.csv",
    "td_activity.csv",
    "rbc_activity.csv",
    "nbdb_activity.csv",
    "nbdb_activity_fr.csv",
)

#: Files whose corporate-action row MUST refuse the whole file (docs/41 §6).
REFUSAL_FILES: tuple[str, ...] = ("ibkr_corporate_actions.csv", "td_corporate_action.csv")

#: Files with a row the parser cannot turn into a number/date — staging answers
#: 422 rather than dropping or guessing it (docs/41 "Unmapped rows").
STUCK_FILES: tuple[str, ...] = ("questrade_bad_date.csv", "questrade_bad_number.csv")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sidecar(name: str) -> dict:
    return json.loads((FIXTURES / name).with_suffix(".expected.json").read_text())


def _records(path: Path) -> list[list[str]]:
    """Non-empty logical records of a fixture (csv handles quoted newlines)."""
    text = path.read_text(encoding="utf-8")
    first = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if first.count(";") > first.count(",") else ","
    return [
        row
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]


def _eligible_data_rows(path: Path) -> int:
    """Records this profile is ALLOWED to turn into a trade or an unmapped row.

    Anything subtracted here is either file furniture (the header record,
    IBKR's per-section ``Header`` records, its ``Statement`` / ``Account
    Information`` title rows) or a section IBKR excludes **with a per-file
    notice** (``Withholding Tax``) — a notice, not a dropped row. Whatever is
    left must come back as `summary.total + summary.unmapped`: that identity is
    "no silent drops" made arithmetic.
    """
    records = _records(path)
    if path.name != "ibkr_statement.csv":
        return len(records) - 1  # minus the one header record
    counted = 0
    for row in records[1:]:
        if len(row) > 1 and row[1].strip() == "Header":
            continue  # section column header
        if row[0].strip() in ("Statement", "Account Information"):
            continue  # file furniture (docs/41 notices table)
        if row[0].strip() == "Withholding Tax":
            continue  # excluded WITH a notice, never silently
        counted += 1
    return counted


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m82-{datetime.now().timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(client: TestClient, headers: dict[str, str], currency: str = "CAD") -> dict:
    body = client.post(
        f"{API}/accounts",
        json={
            "name": f"M82 {currency}",
            "type": "brokerage_non_registered",
            "currency": currency,
        },
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _upload(
    client: TestClient,
    headers: dict[str, str],
    account_id: int,
    name: str,
    slug: str | None,
    *,
    preview: bool,
):
    url = f"{API}/trades/import/preview" if preview else f"{API}/trades/import"
    with open(FIXTURES / name, "rb") as handle:
        return client.post(
            url,
            files={"file": (name, handle, "text/csv")},
            data={"account_id": str(account_id)},
            params={"profile": slug} if slug else {},
            headers=headers,
        )


def _dry_run(client, headers, account_id, name, slug):
    return _upload(client, headers, account_id, name, slug, preview=True)


def _stage(client, headers, account_id, name, slug):
    return _upload(client, headers, account_id, name, slug, preview=False)


def _counts(session: Session, user_id: int) -> dict[str, int]:
    """Rows a dry run must not move: securities, trades, import_batches."""
    return {
        "securities": len(session.exec(select(Security).where(Security.user_id == user_id)).all()),
        "trades": len(session.exec(select(Trade).where(Trade.user_id == user_id)).all()),
        "batches": len(
            session.exec(select(ImportBatch).where(ImportBatch.user_id == user_id)).all()
        ),
    }


def _balances(session: Session, account_id: int) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """`positions` and `cash_effect` recomputed from committed rows only.

    Positions are signed quantities summed per symbol; cash effect reuses
    `services/total_return._trade_cash_effect`, the single source of the
    docs/05 cash-effect table, grouped by `securities.listing_currency` so the
    invariant can never drift from the report engine.
    """
    trades = session.exec(
        select(Trade, Security.symbol, Security.listing_currency)
        .join(Security, Trade.security_id == Security.id)
        .where(Trade.account_id == account_id)
    ).all()
    positions: dict[str, Decimal] = {}
    cash: dict[str, Decimal] = {}
    for trade, symbol, listing in trades:
        positions[symbol] = positions.get(symbol, Decimal("0")) + trade.quantity
        cash[listing] = cash.get(listing, Decimal("0")) + _trade_cash_effect(trade)
    return (
        {symbol: total for symbol, total in positions.items() if total != 0},
        {currency: total for currency, total in cash.items() if total != 0},
    )


@pytest.fixture(name="fx_cache")
def fx_cache_fixture(session: Session) -> None:
    """Seed the shared `fx_rates` cache across every fixture trade date.

    `fx_rate_cad` is exact-date and cache-only (docs/06/docs/07): a taxable
    account's ACB rebuild answers `422 fx_rate_missing` rather than guess a
    rate. That refusal is the documented behaviour, so the suite supplies the
    rates a real install gets from BoC instead of disabling the rule. The
    suite runs on its own isolated database (conftest derives `<db>_test`),
    which is the requirement AGENTS §5 places on any suite that seeds fx.
    """
    day = date(2025, 12, 1)
    while day <= date(2026, 4, 30):
        session.add(
            FxRate(
                base="USD",
                quote="CAD",
                rate=Decimal("1.35000000"),
                date=day,
                source=FxSource.boc_valet,
            )
        )
        day += timedelta(days=1)
    session.flush()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch):
    """Pin security resolution to the row's own currency — no yfinance.

    `_resolve_security` falls back to `RawTrade.currency` when both seams
    answer None, which is exactly the docs/41 currency contract under test:
    a Fidelity USD row must stay USD even in a CAD account.
    """
    monkeypatch.setattr(market_data, "lookup_symbol", lambda symbol, **kw: None)
    monkeypatch.setattr(market_data, "fetch_latest_close", lambda symbol, today=None: None)


# --------------------------------------------------------------------------- #
# 1. Per-institution smoke: dry run → stage → commit → DB invariant → re-import
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SMOKE_FILES)
def test_per_institution_smoke(
    client: TestClient, session: Session, fx_cache: None, name: str
) -> None:
    expected = _sidecar(name)
    slug = expected["profile"]
    label = BROKERAGE_PROFILES[slug].label
    headers = _auth(client)
    account = _account(client, headers, expected.get("account_currency", "CAD"))

    # --- dry run: zero writes, stated totals -------------------------------
    preview = _dry_run(client, headers, account["id"], name, slug)
    assert preview.status_code == 201, preview.text
    dry = preview.json()
    assert dry["profile"] == slug
    assert len(dry["notices"]) == expected["notices"], dry["notices"]
    for notice in dry["notices"]:
        assert notice.startswith(f"{label}: "), notice
    assert dry["summary"]["unmapped"] == expected["unmapped"]
    assert dry["summary"]["total"] == expected["rows"]
    assert len(dry["unmapped"]) == expected["unmapped"]
    # No silent drops: parsed + unmapped == eligible data rows (docs/41).
    assert dry["summary"]["total"] + dry["summary"]["unmapped"] == _eligible_data_rows(
        FIXTURES / name
    )

    # --- stage -------------------------------------------------------------
    staged = _stage(client, headers, account["id"], name, slug)
    assert staged.status_code == 201, staged.text
    body = staged.json()
    assert body["profile"] == slug
    assert body["institution"] == label
    assert body["imported"] == expected["imported"]
    assert body["duplicates_skipped"] == expected["duplicates_skipped"]
    assert len(body["unmapped"]) == expected["unmapped"]
    assert len(body["notices"]) == expected["notices"]
    assert len(body["preview"]) == expected["rows"]

    batch = session.exec(select(ImportBatch).where(ImportBatch.id == body["batch_id"])).one()
    assert batch.institution == label

    # --- commit ------------------------------------------------------------
    committed = client.post(f"{API}/trades/import/{body['batch_id']}/commit", headers=headers)
    assert committed.status_code == 200, committed.text
    assert committed.json()["imported"] == expected["imported"]

    # --- balance invariant, recomputed from the database -------------------
    positions, cash = _balances(session, account["id"])
    assert positions == {
        symbol: Decimal(quantity) for symbol, quantity in expected["positions"].items()
    }
    assert cash == {currency: Decimal(total) for currency, total in expected["cash_effect"].items()}

    # --- re-import: net-new must be 0 --------------------------------------
    again = _stage(client, headers, account["id"], name, slug)
    assert again.status_code == 201, again.text
    assert again.json()["imported"] == 0
    assert again.json()["duplicates_skipped"] == expected["rows"]


# --------------------------------------------------------------------------- #
# 2. Dry-run / staging verdict parity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SMOKE_FILES)
def test_dry_run_matches_staged_verdicts(client: TestClient, session: Session, name: str) -> None:
    """A row the dry run calls `new` stages as imported; `duplicate` stages
    with `is_duplicate=true` — one decision function, two surfaces (docs/41)."""
    slug = _sidecar(name)["profile"]
    headers = _auth(client)
    account = _account(client, headers)

    def verdicts() -> list[str]:
        resp = _dry_run(client, headers, account["id"], name, slug)
        assert resp.status_code == 201, resp.text
        return [row["verdict"] for row in resp.json()["rows"]]

    def staged_duplicates() -> list[bool]:
        resp = _stage(client, headers, account["id"], name, slug)
        assert resp.status_code == 201, resp.text
        return [row["duplicate"] for row in resp.json()["preview"]]

    # First pass: nothing imported yet, every row is new on both surfaces.
    first = verdicts()
    assert first, "fixture produced no rows"
    assert set(first) == {"new"}
    assert staged_duplicates() == [False] * len(first)

    # Second pass: everything is already staged/committed, both say duplicate.
    second = verdicts()
    assert second == ["duplicate"] * len(first)
    assert staged_duplicates() == [True] * len(first)


# --------------------------------------------------------------------------- #
# 3. Zero-write guarantee
# --------------------------------------------------------------------------- #


def test_dry_run_writes_nothing(client: TestClient, session: Session) -> None:
    """`securities`, `trades` and `import_batches` counts are byte-identical
    across a dry run of every fixture — including the refusals."""
    headers = _auth(client)
    account = _account(client, headers)
    user_id = _user_id(session, account["id"])

    before = _counts(session, user_id)
    for name in (*SMOKE_FILES, *REFUSAL_FILES):
        slug = _sidecar(name)["profile"] if name in SMOKE_FILES else None
        resp = _dry_run(client, headers, account["id"], name, slug)
        # Refusal fixtures answer 422 from the parser; either way nothing is
        # written, which is the actual claim.
        assert resp.status_code in (201, 422), resp.text
    assert _counts(session, user_id) == before

    # The stuck files are NOT refused on the dry run — they are shown, tagged.
    for name in STUCK_FILES:
        resp = _dry_run(client, headers, account["id"], name, "questrade")
        assert resp.status_code == 201, resp.text
        unmapped = resp.json()["unmapped"]
        assert len(unmapped) == 1
        assert unmapped[0]["code"] == "stuck"
        assert unmapped[0]["institution"] == "Questrade"
    assert _counts(session, user_id) == before


def _user_id(session: Session, account_id: int) -> int:
    account = session.get(Account, account_id)
    assert account is not None
    return account.user_id


# --------------------------------------------------------------------------- #
# 4. Unmapped rows are per-institution distinct — never silent
# --------------------------------------------------------------------------- #


def test_unmapped_rows_carry_the_institution(client: TestClient) -> None:
    """Every unmapped row names the institution that produced it, so two
    profiles' refusals are distinguishable on sight (docs/41)."""
    headers = _auth(client)
    account = _account(client, headers)
    seen: dict[str, str] = {}
    for name in SMOKE_FILES:
        slug = _sidecar(name)["profile"]
        label = BROKERAGE_PROFILES[slug].label
        resp = _dry_run(client, headers, account["id"], name, slug)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        for row in body["unmapped"]:
            assert row["institution"] == label, row
            assert row["reason"].startswith(f"{label}: "), row["reason"]
            seen.setdefault(row["activity_type"], row["reason"])
        assert body["summary"]["total"] + body["summary"]["unmapped"] == (
            _eligible_data_rows(FIXTURES / name)
        )
    # The same unmappable word is reported with its own institution's prefix.
    assert "Questrade: Unknown activity" in seen["Reorganization"]
    assert "Wealthsimple: Unknown activity" in seen["Foo"]


def test_unparseable_rows_are_shown_then_refused(client: TestClient) -> None:
    """A guessed date or number is exactly the silent-wrong failure docs/41
    refuses: the dry run SHOWS the row, staging answers 422 (docs/41)."""
    headers = _auth(client)
    account = _account(client, headers)

    for name in STUCK_FILES:
        preview = _dry_run(client, headers, account["id"], name, "questrade")
        assert preview.status_code == 201, preview.text
        body = preview.json()
        assert body["summary"]["unmapped"] == 1
        assert body["unmapped"][0]["code"] == "stuck"
        assert body["summary"]["total"] == 1  # the good row is still previewed

        staged = _stage(client, headers, account["id"], name, "questrade")
        assert staged.status_code == 422, staged.text
        assert staged.json()["code"] == "parse_error"
        assert "line 3" in staged.json()["detail"]


# --------------------------------------------------------------------------- #
# 5. Multi-currency rules (docs/41 § multi-currency, docs/07)
# --------------------------------------------------------------------------- #


def test_fidelity_usd_survives_a_cad_account(
    client: TestClient, session: Session, fx_cache: None
) -> None:
    """`file_constant` beats `accounts.currency`: the account never leaks into
    the row, and the CAD conversion goes through `fx_rates` (docs/07)."""
    headers = _auth(client)
    account = _account(client, headers, currency="CAD")
    assert account["currency"] == "CAD"

    resp = _stage(client, headers, account["id"], "fidelity_activity.csv", "fidelity")
    assert resp.status_code == 201, resp.text
    assert resp.json()["preview"][0]["currency"] == "USD"
    client.post(f"{API}/trades/import/{resp.json()['batch_id']}/commit", headers=headers)

    aapl = session.exec(
        select(Security).where(
            Security.user_id == _user_id(session, account["id"]), Security.symbol == "AAPL"
        )
    ).one()
    assert aapl.listing_currency == "USD"

    _, cash = _balances(session, account["id"])
    assert cash == {"USD": Decimal("-845.10")}


def test_row_column_blank_currency_falls_back_to_account(
    client: TestClient,
) -> None:
    """`require_currency=false` (NBDB/RBC): a blank cell keeps the row and the
    target account decides — the deposit row must not become an unmapped row."""
    headers = _auth(client)
    account = _account(client, headers, currency="CAD")
    resp = _dry_run(client, headers, account["id"], "nbdb_activity.csv", "nbdb")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["summary"]["unmapped"] == 0
    deposit = next(row for row in body["rows"] if row["action"] == "contribution")
    assert deposit["currency"] == "CAD"


def test_row_column_required_currency_rejects_the_row(client: TestClient) -> None:
    """`require_currency=true` (Questrade): a blank ISO cell is an unmapped
    row naming the column, not a silently defaulted currency."""
    csv_text = (
        "Transaction Date,Settlement Date,Action,Symbol,Description,Currency,"
        "Quantity,Price,Gross Amount,Commission,Net Amount,Account\n"
        "2026-01-12,2026-01-14,BUY,SHOP,Shopify Inc,,10,150.00,1500.00,4.95,"
        "-1504.95,QCAN\n"
    )
    headers = _auth(client)
    account = _account(client, headers)
    resp = client.post(
        f"{API}/trades/import/preview",
        files={"file": ("q.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"account_id": account["id"]},
        params={"profile": "questrade"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["summary"]["total"] == 0
    assert body["summary"]["unmapped"] == 1
    assert "missing Currency" in body["unmapped"][0]["reason"]
    assert body["unmapped"][0]["institution"] == "Questrade"


# --------------------------------------------------------------------------- #
# 6. Corporate actions: parse strictly, refuse unknown forms (AGENTS §3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", REFUSAL_FILES)
def test_unknown_corporate_action_is_422_and_writes_nothing(
    client: TestClient, session: Session, name: str
) -> None:
    headers = _auth(client)
    account = _account(client, headers)
    user_id = _user_id(session, account["id"])
    before = _counts(session, user_id)

    resp = _stage(client, headers, account["id"], name, None)
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["code"] == "unknown_corporate_action"
    assert "line" in payload["detail"]
    assert "manual" in payload["detail"] or "by hand" in payload["detail"]

    # The rejection happens before any row is staged.
    assert _counts(session, user_id) == before


# --------------------------------------------------------------------------- #
# 7. Detection, header mismatch, unknown slug
# --------------------------------------------------------------------------- #


def test_detect_profile() -> None:
    """Every fixture auto-detects to its own slug; an unknown header does not."""
    for name in SMOKE_FILES:
        expected = _sidecar(name)["profile"]
        detected = detect_profile((FIXTURES / name).read_bytes())
        assert detected is not None, name
        assert detected.slug == expected, name
    for name in REFUSAL_FILES:
        detected = detect_profile((FIXTURES / name).read_bytes())
        assert detected is not None, name
        assert detected.slug == _expected_refusal_slug(name), name

    assert detect_profile(b"some,unknown,header\n1,2,3\n") is None
    assert detect_profile(b"") is None


def _expected_refusal_slug(name: str) -> str:
    return "ibkr" if name.startswith("ibkr") else "td-di"


def test_detect_profile_stays_in_registry_order() -> None:
    """The picker and docs/41 read PROFILE_SLUGS — pin the public order."""
    assert PROFILE_SLUGS == (
        "questrade",
        "wealthsimple",
        "ibkr",
        "fidelity",
        "td-di",
        "rbc-di",
        "nbdb",
    )
    assert set(BROKERAGE_PROFILES) == set(PROFILE_SLUGS)


def test_explicit_profile_header_mismatch_is_422(client: TestClient) -> None:
    headers = _auth(client)
    account = _account(client, headers)
    resp = _stage(client, headers, account["id"], "wealthsimple_activity.csv", "questrade")
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["code"] == "profile_header_mismatch"
    assert "Transaction Date" in payload["detail"] or "missing" in payload["detail"]


def test_unknown_import_profile_is_422(client: TestClient) -> None:
    headers = _auth(client)
    account = _account(client, headers)
    resp = _stage(client, headers, account["id"], "questrade_activity.csv", "not-a-broker")
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "unknown_import_profile"


def test_auto_detection_without_a_slug(client: TestClient) -> None:
    """No `profile` given → header detection; a Wealthsimple-shaped file still
    falls back to the LEGACY parser so existing imports stay byte-identical."""
    headers = _auth(client)
    account = _account(client, headers)

    resp = _stage(client, headers, account["id"], "questrade_activity.csv", None)
    assert resp.status_code == 201, resp.text
    assert resp.json()["profile"] == "questrade"
    assert resp.json()["institution"] == "Questrade"

    # Byte-for-byte the legacy parser: same rows in, same rows reported out.
    raw = (FIXTURES / "wealthsimple_activity.csv").read_bytes()
    legacy_rows, legacy_unmapped = parse_investment_csv(raw)
    legacy = _stage(client, headers, account["id"], "wealthsimple_activity.csv", None)
    assert legacy.status_code == 201, legacy.text
    assert legacy.json()["profile"] is None
    assert legacy.json()["institution"] is None
    assert legacy.json()["imported"] == len(legacy_rows)
    assert len(legacy.json()["unmapped"]) == len(legacy_unmapped)

    # The express-order label is the M82 evidence note: the legacy parser
    # reports it, the opted-in profile RESOLVES it (docs/41 § "Action
    # vocabulary"). Same file, second account, so nothing dedups away.
    express = next(
        row
        for row in legacy.json()["unmapped"]
        if row["activity_type"] == "Buy" and row["activity_sub_type"] == "Express"
    )
    assert "Unknown activity" in express["reason"]
    assert express["institution"] is None  # legacy rows carry no institution

    profiled = _stage(
        client,
        headers,
        _account(client, headers)["id"],
        "wealthsimple_activity.csv",
        "wealthsimple",
    )
    assert profiled.status_code == 201, profiled.text
    assert profiled.json()["profile"] == "wealthsimple"
    assert profiled.json()["institution"] == "Wealthsimple"
    assert profiled.json()["imported"] == len(legacy_rows) + 1
    assert len(profiled.json()["unmapped"]) == len(legacy_unmapped) - 1


# --------------------------------------------------------------------------- #
# 8. The export registry and the staging write path are untouched
# --------------------------------------------------------------------------- #


def test_export_registry_unchanged() -> None:
    """M82 touches the IMPORT path only: no export module changed shape.

    The export registry test (`tests/test_export_registry.py`) is the live
    assertion; this pins that its subject still exists and still covers
    `SQLModel.metadata`, so a future rename cannot silently orphan it.
    """
    source = (APP_DIR / "api" / "export.py").read_text(encoding="utf-8")
    assert "EXCLUDED_TABLES" in source
    registry_test = Path(__file__).parent / "test_export_registry.py"
    assert registry_test.exists()
    text = registry_test.read_text(encoding="utf-8")
    assert "SQLModel.metadata" in text


def test_importbatch_write_sites_are_pinned() -> None:
    """Exactly three `ImportBatch(` construction sites — M82 adds no second
    write path (docs/41 "Unchanged by design")."""
    sites: dict[str, int] = {}
    for path in APP_DIR.rglob("*.py"):
        hits = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if "class ImportBatch" in line:
                continue  # the model's own declaration is not a write site
            hits += len(re.findall(r"(?<![\w.])ImportBatch\(", line))
        if hits:
            sites[str(path.relative_to(APP_DIR))] = hits
    assert sites == {
        "services/import_staging.py": 1,
        "services/demo_seed.py": 1,
        "api/trades.py": 1,
    }


def test_profile_registry_is_pure_config() -> None:
    """A profile is config + fixtures: no DB, no network import inside the
    registry module beyond the models it maps to (docs/41 "What a profile is")."""
    source = (APP_DIR / "importers" / "brokerage_profiles.py").read_text(encoding="utf-8")
    for forbidden in ("requests.", "httpx.", "yfinance", "session.add", "db.commit"):
        assert forbidden not in source, forbidden
