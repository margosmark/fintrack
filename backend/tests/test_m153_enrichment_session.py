"""M153 — the enrichment background task must never hold a session across a provider wait.

The app-side half of the M152 class (`tests/test_m152_seed_session.py`, which
fixed `scripts/backfill_etf_holdings.py`). M152's sweep caught an OPERATOR
session parked `idle in transaction`; this card is about the session the API
itself parks, on the serving path.

`fund_enrichment._run_enrichment` is queued as a FastAPI `BackgroundTask`
from BOTH commit routes (`api/imports.py`, `api/trades.py`). It opens its own
session out of `deps.get_engine()` — the API's pool — and then:

* `session.get(Security, id)` autobegins a transaction, and
* the very next thing `market_data.import_fund_composition` does is
  `fetch_fund_composition(fund.symbol)` — a network call.

`ensure_sub_fund_composition` is the worse half: it autobegins on the
`select(Security).where(user_id...)` map build and then walks up to
`SUB_FUND_ENRICHMENT_CAP = 12` sub-funds, each doing `lookup_symbol`,
`import_fund_composition`, `ensure_fundamentals` and `backfill_range` — four
more provider waits — inside that one transaction. `ensure_fundamentals`
carries the same shape on its own (SELECT, then `fetch_fundamentals`).

The pin is the MECHANISM, asked from the moment of every wait:

1. no session the task opened is in a transaction, and
2. no backend on this database is `idle in transaction` — the exact row
   M148 cost a 2-hour outage for, and the row M152's sweep caught on a
   healthy operator run.

Every provider seam on the enrichment path is stubbed with a probe that asks
both questions BEFORE answering, so the run exercises the real write path
(`import_fund_composition` really deletes+inserts+commits, the sub-fund is
really created, the holding backlink really runs) while every network hop
records whether the pool connection was parked at that instant.

`_run_enrichment` reads its fund through its OWN session/engine, which can
only see COMMITTED rows — the suite fixture's outer transaction is invisible
to it (the house pattern, docs/13). So the tenant is committed for real
through a dedicated engine and deleted again in teardown, exactly like
`test_m148_pool_deadlock.py::forecast_tenant`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session as SQLModelSession

import app.services.market_data as market_data
from app.api.deps import get_engine
from app.core.config import get_settings
from app.models.etf_holdings import EtfHoldingSource
from app.models.securities import Security, SecurityKind
from app.models.users import User
from app.services import fund_enrichment

_FUND_SYMBOL = "M153FUND"
#: Answers `lookup_symbol` with None — the "unknown ticker, stays unlinked"
#: branch, which is still a provider wait and must be released like the rest.
_GHOST_SYMBOL = "M153GHOST"
#: TWO real sub-funds, so the loop's second iteration proves the first one
#: left nothing parked behind it (the same two-candidate shape M152 used).
_SUB_SYMBOLS = ("M153SUB", "M153SUB2")

#: The exact provider-call order a healthy run produces. Asserting it is what
#: proves every probe below really sat on the path it claims to guard — a
#: silent early `return` would otherwise leave a vacuous "nothing was busy".
_EXPECTED_CALLS = (
    ("fetch_fund_composition", _FUND_SYMBOL),  # the fund's own import
    ("lookup_symbol", _GHOST_SYMBOL),  # deepening: unknown ticker, skipped
    *(
        call
        for sub in _SUB_SYMBOLS
        for call in (
            ("lookup_symbol", sub),  # deepening: resolve (creates the row)
            ("fetch_fund_composition", sub),  # deepening: the sub-fund's import
            ("fetch_fundamentals", sub),  # deepening: its fundamentals
            ("fetch_history", sub),  # deepening: its price backfill
        )
    ),
    ("fetch_fundamentals", _FUND_SYMBOL),  # _run_enrichment's own fundamentals
)

#: Every backend attached to THIS database and parked in a transaction. No
#: statement filter on purpose: the question is the pool's, not one query's —
#: a client backend parked on ANY statement is a connection the pool cannot
#: hand out. Other pytest sessions are on their own database, and autovacuum
#: is not a client backend.
_IDLE_IN_TRANSACTION = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND backend_type = 'client backend' "
    "AND state = 'idle in transaction' "
    "AND pid <> pg_backend_pid()"
)

#: ``(label, symbol)`` of every provider call, and whether the pool was parked
#: at the moment it was made.
_PROBES: list[tuple[str, str]] = []
_BUSY: list[bool] = []


@pytest.fixture(name="opened_sessions", autouse=True)
def opened_sessions_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[SQLModelSession]]:
    """Every session `_run_enrichment` opens, in order — the objects whose
    state the pin reads. Only the factory is replaced; the engine behind it
    (and therefore the connection, and therefore `pg_stat_activity`) is the
    real one, exactly as in prod.

    Autouse because EVERY test in this module asks the same question, and the
    provider probes below read the list at the moment of each wait.
    """
    _PROBES.clear()
    _BUSY.clear()
    opened: list[SQLModelSession] = []
    real_factory = fund_enrichment.SQLModelSession

    def _spy(*args: object, **kwargs: object) -> SQLModelSession:
        session = real_factory(*args, **kwargs)
        opened.append(session)
        return session

    def _at_the_wait(label: str, symbol: str) -> None:
        """The pin, asked at the moment the provider would be awaited."""
        _PROBES.append((label, symbol))
        # The same engine the task itself draws its connection from — so a
        # parked task backend is visible here by construction.
        with get_engine().connect() as conn:
            parked = int(conn.execute(text(_IDLE_IN_TRANSACTION)).scalar() or 0)
        _BUSY.append(parked > 0 or any(session.in_transaction() for session in opened))

    def _probe_composition(symbol: str) -> Any:
        _at_the_wait("fetch_fund_composition", symbol)
        if symbol in _SUB_SYMBOLS:
            rows: list[dict[str, object]] = [
                {
                    "holding_symbol": "MSFT",
                    "holding_name": "Microsoft",
                    "weight_pct": Decimal("100"),
                }
            ]
        else:
            rows = [
                {
                    "holding_symbol": _GHOST_SYMBOL,
                    "holding_name": "Ghost Holdings",
                    "weight_pct": Decimal("20"),
                },
                *(
                    {
                        "holding_symbol": sub,
                        "holding_name": f"{sub} long name",
                        "weight_pct": Decimal("40"),
                    }
                    for sub in _SUB_SYMBOLS
                ),
            ]
        return (EtfHoldingSource.yfinance, rows)

    def _probe_lookup(symbol: str) -> dict[str, object] | None:
        _at_the_wait("lookup_symbol", symbol)
        if symbol not in _SUB_SYMBOLS:
            return None
        return {
            "symbol": symbol,
            "name": f"{symbol} long name",
            "kind": SecurityKind.etf,
            "listing_currency": "CAD",
            "exchange": "TSX",
        }

    def _probe_fundamentals(symbol: str) -> dict[str, object] | None:
        _at_the_wait("fetch_fundamentals", symbol)
        return {"sector": "Test"}

    def _probe_history(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
        _at_the_wait("fetch_history", symbol)
        return {}

    monkeypatch.setattr(fund_enrichment, "SQLModelSession", _spy)
    monkeypatch.setattr(market_data, "fetch_fund_composition", _probe_composition)
    monkeypatch.setattr(market_data, "lookup_symbol", _probe_lookup)
    monkeypatch.setattr(market_data, "fetch_fundamentals", _probe_fundamentals)
    monkeypatch.setattr(market_data, "fetch_history", _probe_history)
    yield opened


@pytest.fixture(name="committed_fund")
def committed_fund_fixture() -> Iterator[int]:
    """A really-committed tenant + fund with ZERO composition rows — the
    state a just-imported batch leaves behind — plus the engine that made
    them visible to the task's own session. Teardown deletes everything the
    run created (the sub-fund security and its rows too) by user_id."""
    engine = create_engine(get_settings().database_url)
    with SQLModelSession(engine) as setup:
        user = User(email="m153-enrichment@example.com", hashed_password="x")
        setup.add(user)
        setup.commit()
        setup.refresh(user)
        assert user.id is not None
        user_id = user.id
        fund = Security(
            user_id=user_id,
            symbol=_FUND_SYMBOL,
            name="M153 Fixture Fund",
            kind=SecurityKind.etf,
            listing_currency="CAD",
            exchange="TSX",
        )
        setup.add(fund)
        setup.commit()
        setup.refresh(fund)
        fund_id = fund.id
        assert fund_id is not None
    try:
        yield fund_id
    finally:
        with SQLModelSession(engine) as teardown:
            for table in ("etf_holdings", "fundamentals", "prices", "securities"):
                teardown.execute(
                    text(f"DELETE FROM {table} WHERE user_id = :uid"),  # noqa: S608 -- table from fixed tuple in this test
                    {"uid": user_id},
                )
            teardown.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
            teardown.commit()
        engine.dispose()


def test_enrichment_task_releases_its_session_before_every_provider_wait(
    committed_fund: int,
    opened_sessions: list[SQLModelSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole background task, askable at every wait: the fund's own
    import, the deepening loop's lookup/import/fundamentals/backfill, and
    `_run_enrichment`'s own fundamentals refresh."""
    fund_enrichment._run_enrichment(committed_fund)
    capsys.readouterr()

    assert _PROBES == list(_EXPECTED_CALLS), (
        f"the enrichment path did not exercise every provider seam — the pin only saw {_PROBES!r}"
    )
    assert opened_sessions, "no session was opened — the pin never ran"
    assert _BUSY == [False] * len(_EXPECTED_CALLS), (
        "the enrichment task held a session in a transaction while it waited "
        "on a provider — that is the `idle in transaction` row M152 filed "
        "and M153 moves onto the serving path"
    )


def test_the_task_sessions_are_closed_not_left_open(
    committed_fund: int, opened_sessions: list[SQLModelSession]
) -> None:
    """The incident's other half: two backends died together at 11:09:05 with
    `unexpected EOF on client connection with an open transaction`. A task
    that closes as it goes has nothing to lose."""
    fund_enrichment._run_enrichment(committed_fund)

    assert opened_sessions, "no session was opened — the pin never ran"
    assert [session.in_transaction() for session in opened_sessions] == [False] * len(
        opened_sessions
    )


def test_the_deepening_loop_releases_between_sub_funds(
    committed_fund: int, opened_sessions: list[SQLModelSession]
) -> None:
    """`ensure_sub_fund_composition` is the half that used to park ONE
    transaction across the whole loop. Two sub-funds are enriched here, so
    the SECOND iteration's waits prove the FIRST one left nothing behind —
    the same two-candidate shape M152's pin used."""
    fund_enrichment._run_enrichment(committed_fund)

    sub_waits = [i for i, (_label, symbol) in enumerate(_PROBES) if symbol in _SUB_SYMBOLS]
    assert {symbol for _label, symbol in _PROBES if symbol in _SUB_SYMBOLS} == set(_SUB_SYMBOLS), (
        "the deepening loop never enriched BOTH sub-funds — the second "
        "iteration never got to prove the first left nothing behind"
    )
    assert all(not _BUSY[i] for i in sub_waits), (
        "the deepening loop parked the pool connection between sub-funds"
    )
    assert [session.in_transaction() for session in opened_sessions] == [False] * len(
        opened_sessions
    )


#: What `ensure_sub_fund_composition` must call, in order, for this fixture:
#: the ghost lookup (skipped), then resolve → import → fundamentals → price
#: backfill for each of the two sub-funds.
_DEEPENING_CALLS = [
    ("lookup_symbol", _GHOST_SYMBOL),
    *(
        call
        for sub in _SUB_SYMBOLS
        for call in (
            ("lookup_symbol", sub),
            ("fetch_fund_composition", sub),
            ("fetch_fundamentals", sub),
            ("fetch_history", sub),
        )
    ),
]


def test_the_release_is_safe_on_a_default_configured_session(
    committed_fund: int, opened_sessions: list[SQLModelSession]
) -> None:
    """The seam the card asked to be decided deliberately, decided: a session
    whose `expire_on_commit` is still at its True DEFAULT — the shape any
    future caller would write, and NOT the shape the two background tasks
    happen to configure.

    A plain `commit()` there would expire every ORM object the loop is about
    to read, and the next attribute access would silently re-open the
    transaction the release just gave back: the fix would then work for the
    sessions that set the flag and quietly not work for anyone else.
    `release_before_provider_wait` pins the flag off for the duration of the
    commit and restores it, so the caller's objects stay usable AND the
    connection still comes back.
    """
    engine = create_engine(get_settings().database_url)
    session = SQLModelSession(engine)  # expire_on_commit=True by default
    assert session.expire_on_commit is True, "the premise of this pin changed"
    opened_sessions.append(session)  # the probes read this list
    try:
        fund = session.get(Security, committed_fund)
        assert fund is not None, "the committed fixture fund is invisible"
        market_data.ensure_sub_fund_composition(session, fund, [_GHOST_SYMBOL, *_SUB_SYMBOLS])
    finally:
        session.close()
        engine.dispose()

    assert _PROBES == _DEEPENING_CALLS, (
        f"the deepening loop did not walk both sub-funds — the pin only saw {_PROBES!r}"
    )
    assert _BUSY == [False] * len(_BUSY), (
        "a default-configured session parked the pool connection across a "
        "provider wait — releasing it must not expire the objects the loop "
        "reads, or the next attribute access re-opens the transaction"
    )


def test_the_release_loads_the_row_it_is_about_to_let_go_of(
    committed_fund: int,
) -> None:
    """The helper's own contract, pinned directly.

    `ensure_sub_fund_composition` releases and THEN calls
    `import_fund_composition(session, sub)` and
    `backfill_range(session, sub, …)`, and both read attributes off that
    object as the first thing they do — `fetch_fund_composition(fund.symbol)`
    and `fetch_history(security.symbol, …)`. Were `sub` expired at that
    moment the read would lazy-load and reopen the transaction the release
    just handed back, leaving the wait dirty while every assertion about "did
    we commit?" still held. So the helper loads the row inside the
    transaction it is about to release.

    Asked as the unit contract rather than end-to-end: on the paths
    exercised above, some earlier read (`sub.kind`, the `has_rows` SELECT)
    has already loaded the row, so no integration route can tell the
    difference — this is the case where it can.
    """
    engine = create_engine(get_settings().database_url)
    session = SQLModelSession(engine)
    try:
        fund = session.get(Security, committed_fund)
        assert fund is not None, "the committed fixture fund is invisible"
        session.commit()  # with expire_on_commit=True this expires `fund`…
        session.expire(fund)  # …and this makes that explicit
        market_data.release_before_provider_wait(session, fund)

        # Exactly what the provider seam does next: read off the object. If
        # the release had not loaded it, this attribute read is a lazy load,
        # and a lazy load is a NEW transaction.
        assert fund.symbol == _FUND_SYMBOL
        assert not session.in_transaction(), (
            "reading the released row reopened the transaction — the release "
            "has to load what the provider call is about to read, or the "
            "wait that follows is dirty again"
        )
    finally:
        session.close()
        engine.dispose()

    assert _PROBES == [], f"no provider should run here — the pin saw {_PROBES!r}"
