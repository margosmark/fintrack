"""Test-infra guards for the suite's own machinery.

BUG-3 (S2b review wave): the rollback-per-test fixture keeps an outer
transaction open while TestClient runs BackgroundTasks, whose bodies open
their OWN session and commit. A commit whose FK points at fixture-inserted
(uncommitted) rows waits for a lock that only releases at teardown — which
the caller never reaches: the suite deadlocks inside the request, and only
during market hours (a successful yfinance fetch is what reaches the
commit). conftest sets a database-level lock_timeout so that wait fails
fast into the wrappers' designed swallow. Removing the conftest ALTER
re-opens the wedge — this guard fails first, without hanging.

Second, `latency_budget_blocker`: the one sanctioned way a wall-clock budget
may skip (docs/34 "what is NOT enforced", AGENTS §4). Its truth table is
pinned here, because getting it backwards would either red every clock pin
on the loaded box that runs suites concurrently, or silently enforce none.
"""

import os

import pytest
from conftest import latency_budget_blocker

from app.api.deps import get_engine


def test_test_db_carries_a_lock_timeout() -> None:
    """The test database must ship a non-zero lock_timeout (BUG-3 guard)."""
    with get_engine().connect() as conn:
        value = conn.exec_driver_sql("SHOW lock_timeout").scalar()
    assert value is not None and value not in ("0", "0s", "0ms"), (
        f"lock_timeout not applied on the test database (got {value!r}); "
        "background-task commits can deadlock the suite again (BUG-3)"
    )


def test_latency_budget_blocker_answers_the_clock_question_the_same_way_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall-clock pin SKIPS with a reason on a busy box, and asserts on a quiet one.

    The skip reason must carry the load and the core count — the caller
    appends its own measured milliseconds to it (docs/34: latency is REPORTED,
    never enforced, on a shared machine), so the evidence survives the skip.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (11.0, 10.0, 9.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    reason = latency_budget_blocker()
    assert reason is not None, "load 11 on 4 cores cannot answer a wall-clock budget"
    assert "11.0" in reason and "4 cores" in reason, reason

    # Exactly saturated is already too busy: the boundary is `>=` the core count.
    monkeypatch.setattr(os, "getloadavg", lambda: (4.0, 3.5, 3.0))
    assert latency_budget_blocker() is not None

    # A quiet box must let the budget assertions run — otherwise the pin is
    # never enforced anywhere and the doc's claim is false.
    monkeypatch.setattr(os, "getloadavg", lambda: (0.7, 0.6, 0.5))
    assert latency_budget_blocker() is None, "a quiet box must let the clock assert"
