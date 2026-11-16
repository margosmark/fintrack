"""M152 — the seed script must never hold a session across the provider loop.

Incident (2026-10-11 11:00-11:09 UTC, prod `fintrack`): a `pg_stat_activity`
sweep taken during M149's deploy verification found sessions parked
`idle in transaction` (`wait_event = ClientRead`) for 8+ minutes, one after
another, with this as their last statement::

    SELECT securities.created_at, ..., securities.dead_symbol_reason
    FROM securities
    WHERE securities.kind IN ($1, $2) AND securities.user_id = $3::INTEGER
    ORDER BY securities.user_id, securities.id

That statement has exactly one source in the tree:
`scripts/backfill_etf_holdings.py::_candidates` in its `--refresh` +
`--user-id` shape (the non-refresh branch carries two EXISTS clauses and an
extra ORDER BY term; `services/fund_enrichment.py::funds_needing_enrichment`
builds its WHERE the other way round). It was an OPERATOR run, not a request
and not a background task: `docker exec fin_tracker-api-1 python -u
scripts/backfill_etf_holdings.py --user-id 343 --apply --refresh`, started
2026-10-11 10:59:59 UTC — the window the sweep was taken in. The script
opened ONE session for the candidate SELECT and then held it, across every
provider fetch and every `--sleep`, for the whole 8-minute run; its second
connection was `_run_enrichment`'s. Both logged `unexpected EOF on client
connection with an open transaction` at 11:09:05 when the exec was torn
down. `/health` answered 200 throughout — nothing user-visible broke.

The pin is the MECHANISM, at the moment the provider is awaited:

1. every session the script opened is out of its transaction, and
2. no backend is `idle in transaction` on THAT statement — the exact row
   the sweep reported.

Both fail without the fix (the candidate session is still open, and
`pg_stat_activity` shows it parked on the very query) and pass with it.

`_candidates` is called for real — it is the statement under test — but its
RETURN is replaced with fixture rows: the script's own engine can only see
COMMITTED data, and the suite fixture's transaction is never committed
(conftest's outer-transaction rollback IS the test's cleanup). What is
asserted here is when the session ends, not which rows come back.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlmodel import Session as SQLModelSession

import app.services.market_data as market_data
from app.core.config import get_settings
from app.models.securities import Security, SecurityKind
from scripts import backfill_etf_holdings as seed

# The observed statement, to the column the sweep identified it by: the
# `--refresh` branch's ORDER BY, which the other branch does not produce.
_PARKED_QUERY = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND state = 'idle in transaction' "
    "AND pid <> pg_backend_pid() "
    "AND query LIKE 'SELECT securities.created_at%' "
    "AND query LIKE '%ORDER BY securities.user_id, securities.id'"
)

#: Two candidates, so the loop runs twice and the SECOND iteration proves the
#: first one left nothing behind either.
_FIXTURE_CANDIDATES = (
    Security(
        id=41001,
        user_id=41001,
        symbol="M152A",
        name="M152 Fixture Fund A",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    ),
    Security(
        id=41002,
        user_id=41001,
        symbol="M152B",
        name="M152 Fixture Fund B",
        kind=SecurityKind.mutual_fund,
        listing_currency="CAD",
        exchange="TSX",
    ),
)


@pytest.fixture(name="opened_sessions")
def opened_sessions_fixture(monkeypatch: pytest.MonkeyPatch) -> list[SQLModelSession]:
    """Every session the script opens, in order — the objects whose state
    the pin reads. Only the factory is replaced; the engine behind it (and
    therefore the connection, and therefore `pg_stat_activity`) is the real
    one, exactly as on a prod run."""
    opened: list[SQLModelSession] = []
    real_session_factory = seed.Session

    def _spy(*args: object, **kwargs: object) -> SQLModelSession:
        session = real_session_factory(*args, **kwargs)
        opened.append(session)
        return session

    monkeypatch.setattr(seed, "Session", _spy)
    return opened


@pytest.fixture(name="fixture_candidates")
def fixture_candidates_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the REAL candidate query, then hand the loop rows it can see.

    The query is the whole point — it is the statement the sweep caught
    parked — but its result set is irrelevant to this pin and the suite's
    rows are not committed, so the returned list is replaced.
    """
    real_candidates = seed._candidates

    def _with_fixture_rows(
        session: object, user_id: int | None, *, refresh: bool
    ) -> list[Security]:
        real_candidates(session, user_id, refresh=refresh)  # type: ignore[arg-type]
        return list(_FIXTURE_CANDIDATES)

    monkeypatch.setattr(seed, "_candidates", _with_fixture_rows)


def _parked_candidates(engine: Engine) -> int:
    """Backends `idle in transaction` on the observed statement."""
    with engine.connect() as conn:
        return int(conn.execute(text(_PARKED_QUERY)).scalar() or 0)


def test_apply_run_releases_every_session_before_the_provider_wait(
    opened_sessions: list[SQLModelSession],
    fixture_candidates: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--apply` (the invocation prod was running): nothing is in a
    transaction when `_run_enrichment` starts, and no backend is parked."""
    seen_busy: list[bool] = []
    enriched: list[int] = []

    def probe(security_id: int) -> None:
        enriched.append(security_id)
        engine = create_engine(get_settings().database_url)
        try:
            parked = _parked_candidates(engine)
        finally:
            engine.dispose()
        seen_busy.append(parked > 0 or any(s.in_transaction() for s in opened_sessions))

    monkeypatch.setattr(seed, "_run_enrichment", probe)

    assert seed.main(["--user-id", "0", "--apply", "--refresh", "--sleep", "0"]) == 0
    capsys.readouterr()

    assert enriched == [41001, 41002], "the candidate query returned nothing — the pin never ran"
    assert len(opened_sessions) >= 1
    assert seen_busy == [False, False], (
        "the seed held a session in a transaction while it waited on the "
        "provider — that is the `idle in transaction` row M152 filed"
    )


def test_dry_run_releases_the_candidate_session_before_the_provider_wait(
    opened_sessions: list[SQLModelSession],
    fixture_candidates: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default dry run parks the SAME statement — it must not either."""
    seen_busy: list[bool] = []
    probed: list[str] = []

    def probe(symbol: str) -> None:
        probed.append(symbol)
        engine = create_engine(get_settings().database_url)
        try:
            parked = _parked_candidates(engine)
        finally:
            engine.dispose()
        seen_busy.append(parked > 0 or any(s.in_transaction() for s in opened_sessions))
        return None

    monkeypatch.setattr(market_data, "fetch_fund_composition", probe)

    assert seed.main(["--user-id", "0", "--refresh", "--sleep", "0"]) == 0
    capsys.readouterr()

    assert probed == ["M152A", "M152B"], "the candidate query returned nothing — the pin never ran"
    assert seen_busy == [False, False]


def test_the_candidate_sessions_are_closed_not_left_open(
    opened_sessions: list[SQLModelSession],
    fixture_candidates: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every session `main()` opened is closed by the time it returns.

    The incident's second symptom: two backends died together at
    11:09:05 with `unexpected EOF on client connection with an open
    transaction`. A script that closes as it goes has nothing to lose.
    """
    monkeypatch.setattr(seed, "_run_enrichment", lambda _security_id: None)

    assert seed.main(["--user-id", "0", "--apply", "--refresh", "--sleep", "0"]) == 0
    capsys.readouterr()

    assert opened_sessions, "no session was opened — the pin never ran"
    assert [s.in_transaction() for s in opened_sessions] == [False] * len(opened_sessions)
