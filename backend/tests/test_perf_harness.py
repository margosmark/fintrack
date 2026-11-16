"""Regression tests for the perf harness itself (t_435c2d14).

A full ``uv run pytest -q`` that crossed local midnight used to end in
``8 failed, 19 errors`` spread across files that had nothing to do with the
break. Two bugs stacked:

1. ``test_perf_budgets`` captured its clock at *import* but seeded the demo
   tenant at *fixture* time, so after 00:00 the trade was dated one day past
   the fx cache and ``rebuild_user_pools`` raised ``No USD/CAD fx rate …``.
2. That raise happened before the fixture's ``yield``, so its teardown — the
   code that puts ``DATABASE_URL`` back — never ran. Every test after ``p``
   then ran against ``<suite>_perf``, which already held the demo tenant, and
   reported duplicate-key errors in innocent files.

Neither bug needs a real midnight to reproduce: the first is driven with a
clock that reads one day ahead, the second with a setup that raises on
purpose. The session-wide ``_guard_database_url`` fixture in conftest.py is
the third line of defence and is pinned by ``test_guard_names_the_leaked_url``.

The two fixture-driving tests drop and recreate ``<suite>_perf``, so they must
run AFTER ``test_perf_budgets`` — which is how they sort.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import test_perf_budgets as perf
from conftest import SUITE_DATABASE_URL, _assert_suite_database_url
from sqlalchemy import func, select
from sqlmodel import Session

import app.api.deps as deps
import app.services.demo_seed as demo_seed
from app.core.config import get_settings
from app.models.fx_rates import FxRate
from app.models.trades import Trade

_TOMORROW = date.today() + timedelta(days=1)


class _RolledClock(date):
    """Reads one day past the real clock — a run whose import beat midnight.

    Every value it *constructs* is handed back as a plain ``date`` so no model
    column or DB driver is ever handed a subclass.
    """

    def __new__(cls, *args):
        return date(*args)

    @classmethod
    def today(cls) -> date:
        return _TOMORROW

    @classmethod
    def fromordinal(cls, ordinal: int) -> date:
        return date.fromordinal(ordinal)

    @classmethod
    def fromisoformat(cls, value: str) -> date:
        return date.fromisoformat(value)


class _RecordingSession:
    """Stands in for a real Session: collects what would have been inserted."""

    def __init__(self) -> None:
        self.rows: list[FxRate] = []

    def add(self, row) -> None:
        self.rows.append(row)

    def flush(self) -> None:
        return None


def _snapshot_dataset() -> tuple[dict, dict]:
    return perf.BUDGETS, perf.SCALE_PROBES


# --- bug 1: two clock sources --------------------------------------------------


def test_fx_seed_ends_on_the_date_it_is_given() -> None:
    """The fx window is a PARAMETER of the caller's clock, not a module global.

    That is the whole of fix 1: ``_seed_fx_cache`` used to read a
    ``date.today()`` captured at import, so a run crossing midnight seeded
    through yesterday while ``ensure_demo_account`` wrote trades dated today.
    """
    recorder = _RecordingSession()
    end = date(2027, 3, 9)
    added = perf._seed_fx_cache(recorder, end)

    dates = [row.date for row in recorder.rows]
    assert added == len(dates) == 401  # 400 trailing days + the end day itself
    assert max(dates) == end, "fx cache must reach the date the caller passed in"
    assert min(dates) == end - timedelta(days=400)


def test_budget_paths_come_from_the_date_they_are_built_from() -> None:
    """Every date-bearing URL is derived from one ``today``, at build time."""
    today = date(2027, 3, 9)
    window_start, window_end = perf._window(today)

    budgets = perf._build_budgets(today)
    assert set(budgets) == set(perf.BUDGETS), "parametrize keys must stay date-free"
    assert f"from={window_start}&to={window_end}" in budgets["dashboard/net-worth"].path
    assert window_end == today
    assert f"year={today.year}" in budgets["reports/capital-gains"].path
    assert budgets["list/transactions-limit10"].path == (
        "/api/v1/transactions?limit=10&offset=0"  # date-free budgets stay untouched
    )

    probes = perf._build_scale_probes(today)
    assert set(probes) == set(perf.SCALE_PROBES)
    assert f"to={window_end}" in probes["reports/net-worth-day"]


def test_perf_setup_survives_a_local_midnight_rollover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the crossing: import happened before 00:00, setup after it.

    ``test_perf_budgets`` already imported and captured its clock; the fixture
    and the demo seed it calls now see one day later. Before the fix this
    raised ``APIError: No USD/CAD fx rate for VTI on <tomorrow>`` at ``next``.
    """
    saved = _snapshot_dataset()
    monkeypatch.setattr(perf, "date", _RolledClock)
    monkeypatch.setattr(demo_seed, "date", _RolledClock)

    gen = perf._perf_database_gen()
    try:
        next(gen)  # ← the original failure point

        assert perf._demo_user_id is not None, "demo seed did not run"
        with Session(deps.get_engine()) as session:
            fx_last = session.execute(select(func.max(FxRate.date))).scalar_one()
            trade_last = session.execute(select(func.max(Trade.date))).scalar_one()

        # The deterministic demo seed does not guarantee a trade on the
        # current date: its latest scheduled contribution is day 28, clamped
        # to the month's last day. The rollover signal is instead that setup
        # completed with the simulated clock and its fx/window bounds cover
        # every seeded trade.
        assert trade_last <= _TOMORROW, (
            f"demo seed produced a trade after its rolled clock: {trade_last} > {_TOMORROW}"
        )
        assert fx_last >= _TOMORROW, (
            f"fx cache ends {fx_last}, before the rolled fixture date {_TOMORROW}"
        )
        assert f"to={_TOMORROW}" in perf.BUDGETS["dashboard/net-worth"].path, (
            "the measured report window must end on the fixture's rolled date"
        )
    finally:
        gen.close()
        perf.BUDGETS, perf.SCALE_PROBES = saved
        assert os.environ.get("DATABASE_URL") == SUITE_DATABASE_URL, (
            "perf fixture teardown leaked DATABASE_URL"
        )


# --- bug 2: teardown skipped by a setup error ----------------------------------


def test_database_url_swap_is_undone_when_setup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A setup error after the swap must still put the world back.

    Before the fix the restore sat after a ``yield`` that was never reached,
    so one failing setup poisoned every later test in the session — the
    duplicate-key cascade the card describes.
    """
    saved = _snapshot_dataset()
    original_url = os.environ.get("DATABASE_URL")
    assert original_url == SUITE_DATABASE_URL, "test precondition"

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated alembic failure")

    monkeypatch.setattr(perf, "command", SimpleNamespace(upgrade=_boom))

    gen = perf._perf_database_gen()
    try:
        with pytest.raises(RuntimeError, match="simulated alembic failure"):
            next(gen)

        assert os.environ.get("DATABASE_URL") == original_url, (
            "DATABASE_URL was left pointing at the perf database"
        )
        assert deps._engine is None, "the perf engine was left cached"
        assert get_settings().seed_demo_account is False, (
            "settings cache not cleared — seed_demo_account would stay True"
        )
    finally:
        perf.BUDGETS, perf.SCALE_PROBES = saved


# --- line of defence 3: the session-wide guard ---------------------------------


def test_guard_names_the_leaked_url() -> None:
    """The session guard must fail loudly and name the database it found."""
    _assert_suite_database_url("the clean case")  # must not raise

    leaked = f"{SUITE_DATABASE_URL}_perf"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = leaked
    try:
        with pytest.raises(AssertionError) as excinfo:
            _assert_suite_database_url("after the test")
        message = str(excinfo.value)
        assert leaked in message, "the guard must name the database it found"
        assert "_perf_database_gen" in message, "the guard must point at the fix"
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
