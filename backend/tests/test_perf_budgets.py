"""M60 — API performance budgets and N+1 guards (docs/34).

Query-count budgets are enforced in every CI run. p95 latency budgets are
measured and reported on every run but asserted only when ``PERF_LATENCY_GATE=1``
— wall-clock on a shared runner is not a stable baseline (docs/34 Decisions 1).

The measurement dataset, method, budget table and slow-query threshold are
pinned in ``docs/34-api-performance-budgets.md``; ``BUDGETS`` below is the
enforced copy of that table and ``test_budget_table_matches_spec`` fails when
the two drift apart.

**One clock, read at fixture time (t_435c2d14).** Everything date-derived in
this module — the report window, the ``year=`` budget, the trailing-400-day fx
seed — is computed from a single ``date.today()`` read inside
``_perf_database_gen`` when the fixture runs, and never at import time.
``ensure_demo_account`` reads the clock itself, so the fixture seeds fx through
``max(today, date.today())`` to cover whichever of the two reads lands later.
The import-time ``BUDGETS`` below exists only so pytest can collect the
parametrized names (which are date-free) before any fixture runs; the fixture
replaces it via ``_rebind_dataset``.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections import Counter
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from conftest import drop_database_force
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session

import app.api.deps as deps
from alembic import command
from app.api.deps import get_engine
from app.core.config import get_settings
from app.core.security import create_access_token
from app.main import app
from app.models.fx_rates import FxRate, FxSource
from app.services.demo_seed import ensure_demo_account
from app.tax.acb import rebuild_user_pools

BACKEND_DIR = Path(__file__).resolve().parents[1]
SPEC_PATH = BACKEND_DIR.parent / "docs" / "34-api-performance-budgets.md"

SAMPLES = int(os.environ.get("PERF_SAMPLES", "40"))
SCALE_SAMPLES = min(5, SAMPLES)
LATENCY_GATE = os.environ.get("PERF_LATENCY_GATE") == "1"
SLOW_QUERY_MS = float(os.environ.get("PERF_SLOW_QUERY_MS", "20"))
# docs/34: no single normalized statement may repeat more than this many times
# inside one request — that is exactly what an N+1 looks like from outside.
MAX_SAME_STATEMENT = 3
WARMUP = 3

# Every date this module measures from is derived in ONE place, at fixture time
# (`_perf_database_gen` below) — never at import. Import-time capture is what
# broke suite runs crossing local midnight: `ensure_demo_account` reads the
# clock when the fixture runs, so the two disagreed by a day and the freshly
# seeded trade landed one day past the fx cache (t_435c2d14).
_demo_user_id: int | None = None


# --- measurement -----------------------------------------------------------------


class _StatementCounter:
    """Counts SQL statements and times them via the engine's execute events."""

    def __init__(self) -> None:
        self.count = 0
        self.breakdown: Counter[str] = Counter()
        self.slow: list[tuple[float, str]] = []
        self._open: dict[int, tuple[float, str]] = {}

    def before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.count += 1
        normalized = " ".join(statement.split())
        self.breakdown[normalized] += 1
        self._open[id(cursor)] = (time.perf_counter(), normalized)

    def after(self, conn, cursor, statement, parameters, context, executemany) -> None:
        started = self._open.pop(id(cursor), None)
        if started is None:
            return
        elapsed_ms = (time.perf_counter() - started[0]) * 1000.0
        if elapsed_ms >= SLOW_QUERY_MS:
            self.slow.append((elapsed_ms, started[1]))


@dataclass
class Measurement:
    name: str
    path: str
    counts: list[int]
    latencies_ms: list[float]
    statements_per_req: dict[str, float]
    max_repeat: float
    slow: list[tuple[float, str]] = field(default_factory=list)

    @property
    def queries(self) -> int:
        return self.counts[0]

    @property
    def stable(self) -> bool:
        return len(set(self.counts)) == 1

    @property
    def p50_ms(self) -> float:
        return statistics.median(self.latencies_ms)

    @property
    def p95_ms(self) -> float:
        ordered = sorted(self.latencies_ms)
        return ordered[round(0.95 * (len(ordered) - 1))]


def _measure(
    client: TestClient, engine: Engine, name: str, path: str, headers: dict[str, str], samples: int
) -> Measurement:
    counter = _StatementCounter()
    event.listen(engine, "before_cursor_execute", counter.before)
    event.listen(engine, "after_cursor_execute", counter.after)
    counts: list[int] = []
    latencies: list[float] = []
    try:
        for _ in range(WARMUP):
            resp = client.get(path, headers=headers)
            assert resp.status_code == 200, (
                f"{path} warm-up -> {resp.status_code}: {resp.text[:300]}"
            )
        counter.breakdown.clear()
        counter.slow.clear()
        for _ in range(samples):
            counter.count = 0
            started = time.perf_counter()
            resp = client.get(path, headers=headers)
            latencies.append((time.perf_counter() - started) * 1000.0)
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text[:300]}"
            counts.append(counter.count)
    finally:
        event.remove(engine, "before_cursor_execute", counter.before)
        event.remove(engine, "after_cursor_execute", counter.after)
    return Measurement(
        name=name,
        path=path,
        counts=counts,
        latencies_ms=latencies,
        statements_per_req={
            stmt: round(total / samples, 3)
            for stmt, total in sorted(counter.breakdown.items(), key=lambda kv: -kv[1])
        },
        max_repeat=max(counter.breakdown.values(), default=0) / samples,
        slow=sorted(counter.slow, reverse=True),
    )


def _row(name: str, m: Measurement) -> str:
    return (
        f"| `{name}` | {m.queries} | {m.p50_ms:.1f} | {m.p95_ms:.1f} | {m.max_repeat:.0f} | "
        f"{'yes' if m.stable else 'NO'} |"
    )


# --- the dataset (docs/34 "Measurement dataset") ----------------------------------


def _seed_fx_cache(session: Session, today: date) -> int:
    """One deterministic USD→CAD row per calendar day over the trailing 400 days.

    fx_rates is never seeded (docs/30 H7); without this the fx-dependent
    endpoints take their 422 `fx_rate_missing` short-circuit and the budget
    would be measured against the error path.

    ``today`` is PASSED IN, not read here: the caller's clock is the one the
    demo seed uses, and a ``date.today()`` captured at module import is a day
    behind by the time a long suite run reaches this fixture.
    """
    day = today.toordinal() - 400
    added = 0
    while day <= today.toordinal():
        d = date.fromordinal(day)
        rate = Decimal(str(round(1.30 + 0.02 * ((d.toordinal() % 60) / 60.0), 6)))
        session.add(FxRate(base="USD", quote="CAD", date=d, rate=rate, source=FxSource.manual))
        added += 1
        day += 1
    session.flush()
    return added


def _perf_database_gen() -> Generator[None, None, None]:
    """Hermetic `<suite>_perf` DB: M10 demo seed + synthetic fx cache (docs/34).

    Two properties this generator must hold — both were broken once (t_435c2d14):

    * **One clock.** ``today`` is read HERE, while the database is built, and
      threaded into the fx seed and the report window (via ``_rebind_dataset``).
      ``ensure_demo_account`` reads the clock itself a moment later, so seeding
      fx through ``max(today, date.today())`` covers whichever read is later.
      Reading ``date.today()`` at import time instead is what made a suite run
      crossing local midnight seed a trade one day past the fx cache.

    * **Exception-safe teardown.** Everything from the ``DATABASE_URL`` swap
      onward sits in ``try/finally``. A generator fixture that raises BEFORE
      its ``yield`` never runs the code after it, so the swap used to leak:
      every test for the rest of the *session* then ran against the perf DB —
      which already holds the demo tenant — and reported duplicate-key failures
      in files that had nothing to do with the break. The separate
      ``_guard_database_url`` fixture in conftest.py now fails loudly if a leak
      ever gets past this.
    """
    global _demo_user_id
    # One clock read for the whole dataset — see the docstring above.
    today = date.today()
    _rebind_dataset(today)  # defined with BUDGETS, further down this module

    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack",
    )
    root, _, suite_db = base.rpartition("/")
    perf_db = f"{suite_db}_perf"
    perf_url = f"{root}/{perf_db}"
    admin = create_engine(f"{root}/postgres", isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            drop_database_force(conn, perf_db)
            conn.execute(text(f'CREATE DATABASE "{perf_db}"'))
    finally:
        admin.dispose()

    os.environ["DATABASE_URL"] = perf_url
    get_settings.cache_clear()
    deps._engine = None
    try:
        settings = get_settings()
        settings.seed_demo_account = True

        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", perf_url)
        command.upgrade(cfg, "head")

        with Session(get_engine(), expire_on_commit=False) as session:
            user = ensure_demo_account(session)
            assert user is not None, "M10 demo seed returned no user"
            # ensure_demo_account read the clock itself just now; cover both
            # reads so a midnight flip inside the seed window still has a rate.
            _seed_fx_cache(session, max(today, date.today()))
            rebuild_user_pools(session, user.id)
            session.commit()
            _demo_user_id = user.id
        settings.seed_demo_account = False

        yield
    finally:
        _demo_user_id = None
        os.environ["DATABASE_URL"] = base
        get_settings.cache_clear()
        deps._engine = None


@pytest.fixture(scope="module", autouse=True)
def _perf_database() -> Iterator[None]:
    yield from _perf_database_gen()


@dataclass
class _PerfEnv:
    """Lazily-measured endpoint cache shared by every test in this module."""

    client: TestClient
    headers: dict[str, str]
    engine: Engine
    results: dict[str, Measurement] = field(default_factory=dict)

    def measure(self, name: str, path: str, samples: int = SAMPLES) -> Measurement:
        if name not in self.results:
            self.results[name] = _measure(
                self.client, self.engine, name, path, self.headers, samples
            )
        return self.results[name]


@pytest.fixture(scope="module")
def perf_env(_perf_database: None) -> Iterator[_PerfEnv]:
    assert _demo_user_id is not None, "perf database fixture did not seed"
    # No get_db override: requests must open real sessions against the perf
    # engine, exactly as production does (docs/34 "Measurement dataset").
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        env = _PerfEnv(
            client=client,
            headers={"Authorization": f"Bearer {create_access_token(_demo_user_id)}"},
            engine=get_engine(),
        )
        yield env
    _write_report(env)
    app.dependency_overrides.clear()


def _write_report(env: _PerfEnv) -> None:
    rows = [
        {
            "endpoint": name,
            "path": m.path,
            "queries": m.queries,
            "queries_stable": m.stable,
            "max_statement_repeat": m.max_repeat,
            "p50_ms": round(m.p50_ms, 2),
            "p95_ms": round(m.p95_ms, 2),
            "max_ms": round(max(m.latencies_ms), 2),
            "samples": len(m.counts),
            "statements_per_request": m.statements_per_req,
            "slow_statements_ge_ms": SLOW_QUERY_MS,
        }
        for name, m in sorted(env.results.items())
    ]
    print("\n| endpoint | queries | p50 ms | p95 ms | max repeat | stable |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, m in sorted(env.results.items()):
        print(_row(name, m))
    target = os.environ.get("PERF_REPORT")
    if target:
        Path(target).write_text(json.dumps(rows, indent=2))
        print(f"perf report written to {target}")


# --- the enforced copy of the docs/34 budget table ---------------------------------


@dataclass(frozen=True)
class Budget:
    path: str
    q: int
    p95_ms: float
    spec_path: str
    samples: int = SAMPLES


def _window(today: date) -> tuple[date, date]:
    """First day of the month 12 months back .. ``today``.

    13 monthly buckets (393 daily buckets on 2026-09-28), matching the reports
    tab default and docs/12's history span.
    """
    year, month = divmod((today.year * 12 + today.month - 1) - 12, 12)
    return date(year, month + 1, 1), today


def _build_budgets(today: date) -> dict[str, Budget]:
    from_, to = _window(today)
    return {
        "dashboard/spending-categories": Budget(
            path=f"/api/v1/reports/spending/categories?from={from_}&to={to}",
            q=3,
            p95_ms=90,
            spec_path="GET /api/v1/reports/spending/categories?from=YYYY-MM-DD&to=YYYY-MM-DD",
        ),
        "dashboard/net-worth": Budget(
            path=f"/api/v1/reports/net-worth?from={from_}&to={to}&granularity=month",
            q=7,
            p95_ms=250,
            spec_path=(
                "GET /api/v1/reports/net-worth?from=YYYY-MM-DD&to=YYYY-MM-DD&granularity=month"
            ),
        ),
        "dashboard/cashflow": Budget(
            path=f"/api/v1/reports/cashflow?from={from_}&to={to}&granularity=month",
            q=4,
            p95_ms=120,
            spec_path=(
                "GET /api/v1/reports/cashflow?from=YYYY-MM-DD&to=YYYY-MM-DD&granularity=month"
            ),
        ),
        # M108 P4: 4 -> 5 statements. The receipt-image flag is ONE batched
        # `SELECT transaction_id … WHERE transaction_id IN (page)` per request —
        # flat across page sizes, so invariant 1 still holds; the ceiling moves
        # because the constant term did, not because a per-row query appeared.
        "list/transactions-limit50": Budget(
            path="/api/v1/transactions?limit=50&offset=0",
            q=5,
            p95_ms=250,
            spec_path="GET /api/v1/transactions?limit=50&offset=0",
        ),
        "list/transactions-limit10": Budget(
            path="/api/v1/transactions?limit=10&offset=0",
            q=5,
            p95_ms=120,
            spec_path="GET /api/v1/transactions?limit=10&offset=0",
        ),
        "reports/investment-performance": Budget(
            path=f"/api/v1/reports/investment-performance?from={from_}&to={to}",
            q=7,
            p95_ms=250,
            spec_path=("GET /api/v1/reports/investment-performance?from=YYYY-MM-DD&to=YYYY-MM-DD"),
        ),
        "reports/capital-gains": Budget(
            path=f"/api/v1/reports/capital-gains?year={today.year}&format=json",
            q=3,
            p95_ms=150,
            spec_path="GET /api/v1/reports/capital-gains?year=YYYY&format=json",
        ),
        # M137 P0-1: ⌘K's transaction group gained its pg_trgm half (it used to
        # be answered by the blind-index token alone) — one more statement, the
        # same six the request has always been: auth, then the five groups.
        "search/global": Budget(
            path="/api/v1/search?q=grocery&limit=5",
            q=6,
            p95_ms=120,
            spec_path="GET /api/v1/search?q=grocery&limit=5",
        ),
    }


# Measured only by the scale-independence guards (docs/34), never budgeted.
def _build_scale_probes(today: date) -> dict[str, str]:
    from_, to = _window(today)
    return {
        "list/transactions-limit1": "/api/v1/transactions?limit=1&offset=0",
        "list/transactions-limit200": "/api/v1/transactions?limit=200&offset=0",
        "reports/net-worth-day": f"/api/v1/reports/net-worth?from={from_}&to={to}&granularity=day",
    }


# Collection-time values: pytest reads BUDGETS' DATE-FREE keys while importing
# this module, before any fixture exists. `_perf_database_gen` REPLACES both
# dicts with these same builders called on ITS clock, so every measured URL,
# the report window and the fx seed end up on one date read per run (t_435c2d14).
BUDGETS: dict[str, Budget] = _build_budgets(date.today())
SCALE_PROBES: dict[str, str] = _build_scale_probes(date.today())


def _rebind_dataset(today: date) -> None:
    """Re-derive every date-bearing URL in this module from ``today``.

    Called by ``_perf_database_gen`` with the date the perf DB is built on, so
    the measured paths can never embed a date that predates the data they query.
    """
    global BUDGETS, SCALE_PROBES
    BUDGETS = _build_budgets(today)
    SCALE_PROBES = _build_scale_probes(today)


# --- guards ------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_query_budget(name: str, perf_env: _PerfEnv) -> None:
    budget = BUDGETS[name]
    m = perf_env.measure(name, budget.path, budget.samples)
    assert m.stable, f"{name}: query count varied across samples {sorted(set(m.counts))}"
    assert m.queries <= budget.q, (
        f"{name} issued {m.queries} SQL statements, budget is {budget.q} (docs/34 budget table)\n"
        + "\n".join(f"  {n:6.2f}  {s}" for s, n in m.statements_per_req.items())
    )
    if LATENCY_GATE:
        assert m.p95_ms <= budget.p95_ms, (
            f"{name} p95 {m.p95_ms:.1f} ms exceeds the {budget.p95_ms:.0f} ms budget (docs/34)"
        )
    print(_row(name, m))


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_no_statement_repeats_beyond_cap(name: str, perf_env: _PerfEnv) -> None:
    """An N+1 repeats one statement; a cap catches it even if a budget is raised."""
    budget = BUDGETS[name]
    m = perf_env.measure(name, budget.path, budget.samples)
    assert m.max_repeat <= MAX_SAME_STATEMENT, (
        f"{name}: one statement ran {m.max_repeat:.1f}x per request "
        f"(cap {MAX_SAME_STATEMENT}) — this is what an N+1 looks like\n"
        + "\n".join(f"  {n:6.2f}  {s}" for s, n in m.statements_per_req.items())
    )


def test_transactions_query_count_is_page_size_independent(perf_env: _PerfEnv) -> None:
    """docs/34 invariant 1: 4 / 53 / 203 before M60, flat after.

    Flat at 5 since M108 P4 — the receipt-flag statement is constant per
    request, which is exactly what this invariant exists to prove it is not.
    """
    counts = {}
    for name, path, samples in (
        ("limit1", SCALE_PROBES["list/transactions-limit1"], SCALE_SAMPLES),
        (
            "limit50",
            BUDGETS["list/transactions-limit50"].path,
            BUDGETS["list/transactions-limit50"].samples,
        ),
        ("limit200", SCALE_PROBES["list/transactions-limit200"], SCALE_SAMPLES),
    ):
        m = perf_env.measure(f"scale/transactions-{name}", path, samples)
        counts[name] = m.queries
    assert len(set(counts.values())) == 1, (
        f"GET /transactions query count scales with page size: {counts} (docs/34 invariant 1)"
    )


def test_net_worth_query_count_is_bucket_count_independent(perf_env: _PerfEnv) -> None:
    """docs/34 invariant 2: 368 (day) vs 18 (month) before M60, flat after."""
    day = perf_env.measure(
        "scale/net-worth-day", SCALE_PROBES["reports/net-worth-day"], SCALE_SAMPLES
    ).queries
    month = perf_env.measure(
        "scale/net-worth-month",
        BUDGETS["dashboard/net-worth"].path,
        BUDGETS["dashboard/net-worth"].samples,
    ).queries
    assert day == month, (
        f"GET /reports/net-worth query count scales with bucket count: "
        f"granularity=day {day} vs granularity=month {month} (docs/34 invariant 2)"
    )


def test_budget_table_matches_spec() -> None:
    """docs/34 is the human source of truth; BUDGETS is the enforced copy."""
    lines = SPEC_PATH.read_text().splitlines()
    for name, budget in sorted(BUDGETS.items()):
        needle = f"`{budget.spec_path}`"
        q_cell = f"| **{budget.q}** |"
        p95_cell = f"| **{budget.p95_ms:g}** |"
        assert any(needle in line and q_cell in line and p95_cell in line for line in lines), (
            f"docs/34 has no budget row for {name}: expected a line containing "
            f"{needle} {q_cell} {p95_cell}"
        )
