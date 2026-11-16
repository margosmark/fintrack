"""Price-history series for the chart family (M126, docs/12 §Range → granularity).

Cache-only: this module only reads `prices` rows (AGENTS §2 — the network seam
is `app/services/market_data.py`, never an aggregation read).

Two documented decisions live here, both pinned in docs/12:

* **auto granularity** — `pick_granularity(span_days)` returns the FINEST of
  day / week / month / year whose estimated bucket count fits
  ``MAX_SERIES_POINTS``. One formula, so 1M -> day, 6M -> week, 5Y -> month
  fall out of it instead of being a lookup table nobody can check.
* **point roofline** — a caller who asks for an explicit granularity gets it,
  coarsened only while the SERIES would exceed ``POINT_ROOFLINE`` points
  (the same guard `services/account_value.py` applies to M118), and the
  response echoes both `granularity` and `requested_granularity`.

Bucketing is `services/reports.py`'s — the same `bucket_key`/`bucket_end` the
report series use — and every point is valued at its bucket END, so a week
bucket carries the last close inside that week (M118's convention).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Literal

from sqlmodel import Session, select

from app.models.prices import Price
from app.services.reports import Granularity, bucket_end, bucket_key

#: What the caller may ask for. `auto` applies the documented formula.
SeriesGranularity = Literal["auto", "day", "week", "month", "year"]

#: Estimated-bucket budget the AUTO pick fits inside (docs/12).
MAX_SERIES_POINTS = 150
#: Hard roofline for an explicit granularity — beyond it the series is
#: coarsened instead of shipping a payload nobody can read (M118's guard).
POINT_ROOFLINE = 400

#: Average days per bucket — month/year are averages on purpose: the pick is a
#: budget decision, not a calendar, and a fixed 30/365 keeps the table checkable.
_UNIT_DAYS: dict[Granularity, float] = {
    "day": 1.0,
    "week": 7.0,
    "month": 30.44,
    "year": 365.25,
}
_COARSER: dict[Granularity, Granularity] = {"day": "week", "week": "month", "month": "year"}


def pick_granularity(span_days: int) -> Granularity:
    """The FINEST granularity whose estimated bucket count fits the budget.

    ``ceil(span_days / unit_days) <= MAX_SERIES_POINTS`` evaluated day-first.
    1M (31d) -> day, 6M (182d) -> week, 5Y (1826d) -> month, 10Y -> month.
    """
    span = max(0, span_days)
    for granularity in ("day", "week", "month", "year"):
        if math.ceil(span / _UNIT_DAYS[granularity]) <= MAX_SERIES_POINTS:  # type: ignore[index]
            return granularity  # type: ignore[return-value]
    return "year"


def span_days(first: dt.date | None, last: dt.date) -> int:
    """Days between the window floor and its end; a missing floor = today."""
    start = first if first is not None else last
    return max(0, (last - start).days)


def load_closes(
    session: Session,
    *,
    user_id: int,
    security_id: int,
    from_: dt.date | None,
    to: dt.date,
) -> list[tuple[dt.date, Decimal]]:
    """Every cached close in the window, ascending. No fetch, cache only."""
    filters = [
        Price.user_id == user_id,
        Price.security_id == security_id,
        Price.date <= to,
    ]
    if from_ is not None:
        filters.append(Price.date >= from_)
    rows = session.exec(
        select(Price.date, Price.price).where(*filters).order_by(Price.date.asc())
    ).all()
    return [(row_date, row_price) for row_date, row_price in rows]


def bucket_series(
    rows: Sequence[tuple[dt.date, Decimal]],
    granularity: Granularity,
) -> list[tuple[dt.date, Decimal]]:
    """One point per bucket: the bucket END date and the LAST close inside it.

    Ascending input; `day` is the identity. An empty series stays empty — the
    caller renders the documented "no data" state, never a fabricated line.
    """
    if granularity == "day":
        return list(rows)
    out: dict[dt.date, Decimal] = {}
    for row_date, price in rows:
        out[bucket_end(bucket_key(row_date, granularity), granularity)] = price
    return sorted(out.items())


def coarsened(
    rows: Sequence[tuple[dt.date, Decimal]],
    granularity: Granularity,
) -> tuple[Granularity, list[tuple[dt.date, Decimal]]]:
    """Apply the point roofline to an EXPLICIT granularity ask (docs/12).

    Auto picks already fit `MAX_SERIES_POINTS`, so they pass through untouched.
    """
    series = bucket_series(rows, granularity)
    while len(series) > POINT_ROOFLINE and granularity in _COARSER:
        granularity = _COARSER[granularity]
        series = bucket_series(rows, granularity)
    return granularity, series


def resolve_granularity(
    rows: Iterable[tuple[dt.date, Decimal]],
    *,
    requested: SeriesGranularity,
    first: dt.date | None,
    to: dt.date,
) -> tuple[Granularity, SeriesGranularity, list[tuple[dt.date, Decimal]]]:
    """`(granularity, requested_granularity, series)` for a price-history call.

    `requested_granularity` echoes the caller's own ask (`auto` included) so
    the UI can say honestly when the server picked or coarsened the buckets.
    """
    materialised = list(rows)
    if requested == "auto":
        picked = pick_granularity(span_days(first, to))
        resolved: Granularity = picked
        requested_echo: SeriesGranularity = "auto"
        series = bucket_series(materialised, picked)
    else:
        resolved, series = coarsened(materialised, requested)
        requested_echo = requested
    # The last bucket's END can fall AFTER the window — the week containing
    # today ends on Sunday — and a close is never dated in the future. Only
    # the last point can exceed `to`, so clamp it rather than invent a gap.
    return resolved, requested_echo, [(min(day, to), price) for day, price in series]
