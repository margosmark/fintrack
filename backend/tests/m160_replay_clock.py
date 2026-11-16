"""Freezing the one clock an M160 replay target reads (docs/33 §Server-first rule 5).

`m160_replay_common.py` covers everything a golden needs EXCEPT the wall
clock: three of the ten vector fixtures were captured on a specific day, and
the routes behind them read ``dt.date.today()`` directly — not ``today_et()``,
so ``tests/conftest.py::frozen_today`` cannot reach them. The replay has to
inject the capture date exactly the way the deleted client tests did:
``s2bReports.test.ts`` passed ``captured_at`` as its ``today``, and
``s2cDividends.test.ts`` did the same to freeze the TTM window.

``monkeypatch`` cannot reach ``datetime.date.today()`` (an immutable C type),
so the pin replaces the MODULE's own ``dt`` binding with a stand-in whose
``date`` is a subclass with a pinned ``today()``. Two invariants keep that
honest:

* ``date(...)`` still builds a PLAIN ``datetime.date`` — ``__new__`` hands
  back ``date(...)`` — so no shim-built value can leak into a response the
  golden then has to compare, and no isinstance/serialization surprise ever
  reaches pydantic's ``dt.date`` fields;
* every other ``dt.*`` name is forwarded from the real module, so a NEW
  clock read fails loudly here instead of silently drifting with the
  calendar.

Run: cd backend && uv run pytest tests/test_m160_replay_s2b.py -q
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest


def freeze_module_clock(monkeypatch: pytest.MonkeyPatch, module: Any, today: dt.date) -> None:
    """Pin ``module.dt.date.today()`` to ``today`` for the duration of a test."""

    class PinnedDate(dt.date):
        def __new__(cls, *args: Any, **kwargs: Any) -> dt.date:
            return dt.date(*args, **kwargs)

        @classmethod
        def today(cls) -> dt.date:
            return today

    monkeypatch.setattr(
        module,
        "dt",
        SimpleNamespace(
            date=PinnedDate,
            timedelta=dt.timedelta,
            datetime=dt.datetime,
            time=dt.time,
            timezone=dt.timezone,
            UTC=dt.UTC,
        ),
    )
