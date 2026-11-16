"""Provenance + freshness block shared by every dividend view (docs/07 §M184).

A dividend figure is only trustworthy when the reader can see WHERE it came
from, WHEN that source last answered, and whether it is stale or missing — the
three answers `GET /fx/status` already gives FX and the valuation strip already
gives prices (`price_source` / `price_age_days` / `price_stale`). The dividend
views had no such seam: the suggestions route hid its provider behind counts and
the two report panels stated no source at all.

One wire shape serves all of them (additive on every route that carries it)::

    {
      "status": "ok" | "stale" | "missing",   # worst status across sources
      "as_of": date-string | null,            # OLDEST as_of across sources
      "sources": [
        {"key": "income_trades", "kind": "ledger", "label": "…", "provider": null,
         "as_of": "2026-09-30" | null, "age_days": 0 | null,
         "status": "ok" | "stale" | "missing", "detail": "…"}
      ]
    }

Rules that keep the vocabulary honest:

- **Only a market-data source can be `stale`.** A ledger source is a recorded
  fact — it does not age; its failure mode is `missing` (nothing recorded). A
  calendar built from your own payments therefore answers ok/missing, never
  "stale".
- `age_days` is measured against the caller's own `today` and is `None` whenever
  `as_of` is `None`.
- Severity runs `missing > stale > ok`, and the top-level `as_of` is the OLDEST
  source timestamp — a view is only as fresh as its least fresh evidence, which
  is exactly the "oldest successful refresh" rule the dividend-suggestions
  header already documents for its `as_of` (docs/07 §M184).
"""

from __future__ import annotations

import datetime as dt

# The one yfinance seam for dividend events (`services/market_data.
# fetch_dividend_history`); `dividend_history.source` is CHECK-constrained to
# this value (docs/02), so the provider reported for this source is a fact of
# the schema, not a per-row read.
DIVIDEND_EVENT_SOURCE = "yfinance"

# docs/07: a cached FX row older than the carry-forward window stops resolving
# new dates at all — the same number `GET /fx/status` calls stale.
FX_STALE_DAYS = 7

_STATUS_SEVERITY = {"ok": 0, "stale": 1, "missing": 2}
STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_MISSING = "missing"


def source_status(
    *,
    key: str,
    kind: str,
    label: str,
    provider: str | None,
    as_of: dt.date | None,
    status: str,
    detail: str,
    age_days: int | None = None,
) -> dict[str, object]:
    """One source entry. `kind` is `market_data` or `ledger`."""
    return {
        "key": key,
        "kind": kind,
        "label": label,
        "provider": provider,
        "as_of": as_of,
        "age_days": age_days,
        "status": status,
        "detail": detail,
    }


def build(sources: list[dict[str, object]]) -> dict[str, object]:
    """Fold source entries into the block every dividend view returns."""
    if not sources:
        return {"status": STATUS_MISSING, "as_of": None, "sources": []}
    status = max(
        (str(source["status"]) for source in sources),
        key=lambda value: _STATUS_SEVERITY[value],
    )
    dated = [stamp for source in sources if isinstance(stamp := source["as_of"], dt.date)]
    return {"status": status, "as_of": min(dated) if dated else None, "sources": sources}


def age_days(as_of: dt.date, today: dt.date) -> int:
    """Whole days between the source's timestamp and the view's clock; never
    negative — a future-dated row is 0d old, not -1d."""
    return max(0, (today - as_of).days)
