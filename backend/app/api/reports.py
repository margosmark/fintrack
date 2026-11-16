"""reports — capital-gains annual report (docs/06) + aggregations (docs/12)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api._report_flags import assert_report_route_live
from app.api.deps import get_current_user, get_db
from app.core.errors import api_error
from app.models.users import User
from app.schemas.reports import (
    CapitalGainsReport,
    CashflowCalendarReport,
    CashflowReport,
    CashflowSankeyReport,
    CategoryBreakdownReport,
    CategoryTagMatrixReport,
    DividendReport,
    NetWorthReport,
    PerformanceReport,
    SpendingReport,
    TotalReturnReport,
)
from app.services import dividends as dividends_service
from app.services import reports as reports_service
from app.services import total_return as total_return_service
from app.services.cashflow_calendar import build_cashflow_calendar, month_bounds
from app.services.txn_filters import today_et
from app.tax.reports import capital_gains_report, report_csv, report_html

Granularity = Literal["day", "week", "month", "year"]
CsvFormat = Literal["json", "csv"]
# M159 (docs/12 §Owner dimension, docs/08 §Report routes): `household` widens
# ONLY who is summed — the caller's own committed rows plus the owner's
# allowlisted rows the caller's own active `sharing_links` row projects
# (docs/43 §6). An unlinked caller, and an OWNER-seat caller (the grant is
# one-directional, docs/43 §2), get their own rows under either value.
ReportScope = Literal["own", "household"]
OwnReportScope = Literal["own"]


_CSV_FILENAMES = {
    "spending": "spending",
    "spending/categories": "spending-categories",
    "category-tag-matrix": "category-tag-matrix",
    "cashflow": "cashflow",
    "net-worth": "net-worth",
    "investment-performance": "investment-performance",
    "dividends": "dividends",
}

# M124 (docs/12 §1): the item field names `format=csv` emits follow the
# dimension the caller asked for — the header is pinned to the JSON fields
# that are actually present, never to a fixed guess.
_CSV_FIELDS: dict[str, list[str]] = {
    "spending:category": ["bucket", "category_id", "category_name", "total_cad"],
    "spending:tag": ["bucket", "tag_id", "tag_name", "total_cad"],
    "spending/categories:category": ["category_id", "name", "cad_total"],
    "spending/categories:tag": ["tag_id", "name", "cad_total"],
    "category-tag-matrix": [
        "category_id",
        "category_name",
        "tag_id",
        "tag_name",
        "cad_total",
    ],
}


def _csv_fields(name: str, group_by: str) -> list[str]:
    return _CSV_FIELDS[f"{name}:{group_by}"]


# `?category_id=` accepts a real id, or the literal `uncategorized` for the
# NULL bucket — the same token the transactions list already uses, so a
# "drill into Uncategorized" link is one string either way (docs/08).
_UNCATEGORIZED_FILTER = "uncategorized"


def _parse_category_filter(raw: str | None) -> tuple[int | None, bool]:
    """`(category_id, uncategorized_only)` for the drill-down filter.

    Anything that is neither an integer nor the `uncategorized` token is a
    422, matching how the transactions list treats the same parameter.
    """
    if raw is None:
        return None, False
    if raw == _UNCATEGORIZED_FILTER:
        return None, True
    try:
        value = int(raw)
    except ValueError as exc:
        raise api_error(422, f"Invalid category filter: {raw}", "invalid_category") from exc
    if value < 1:
        raise api_error(422, f"Invalid category filter: {raw}", "invalid_category")
    return value, False


def _check_range(from_: dt.date, to: dt.date) -> None:
    if from_ > to:
        raise api_error(422, f"Invalid date range: {from_} > {to}", "invalid_date_range")


_MONTH_PATTERN = re.compile(r"(\d{4})-(\d{2})")
_MONTH_YEAR_MIN = 1900
_MONTH_YEAR_MAX = 2200


def _check_month(month: str | None, today: dt.date) -> tuple[dt.date, dt.date]:
    """(month_start, month_end) for `GET /reports/cashflow-calendar` (docs/36).

    An absent `month` means the server's current month (`today_et()`), the same
    defaulting style `GET /reports/dividends?year=` uses. Anything present that
    is not a real calendar month inside 1900-2200 is a **422 `invalid_month`**
    envelope — hand-validated so the endpoint can never answer a bare 500, and
    the year band bounds the bill-occurrence loop.
    """
    if month is None:
        return month_bounds(today.year, today.month)
    invalid = (
        f"Invalid month: '{month}' — expected YYYY-MM, year {_MONTH_YEAR_MIN}-{_MONTH_YEAR_MAX}"
    )
    match = _MONTH_PATTERN.fullmatch(month)
    if match is None:
        raise api_error(422, invalid, "invalid_month")
    year, mon = int(match.group(1)), int(match.group(2))
    if not (_MONTH_YEAR_MIN <= year <= _MONTH_YEAR_MAX and 1 <= mon <= 12):
        raise api_error(422, invalid, "invalid_month")
    return month_bounds(year, mon)


def _csv_response(name: str, from_: dt.date, to: dt.date, body: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{_CSV_FILENAMES[name]}-{from_}-{to}.csv"'
        },
    )


router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/spending", response_model=SpendingReport)
def spending(
    from_: Annotated[dt.date, Query(alias="from")],
    to: Annotated[dt.date, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    granularity: Granularity = "month",
    format: CsvFormat = "json",
    group_by: reports_service.GroupBy = "category",
    category_id: Annotated[
        str | None,
        Query(
            description=(
                "M124 R2 drill filter: a category id (that category AND its "
                "descendants) or the literal `uncategorized`. Composes with "
                "`group_by` — `group_by=tag&category_id=<id>` answers "
                "'this category, by tag'."
            )
        ),
    ] = None,
    scope: ReportScope = "own",
) -> Response | SpendingReport:
    """Spending by time bucket (docs/12 §1).

    `scope=household` (M159) widens ONLY who is summed: the caller's own
    committed rows plus the owner's allowlisted rows through the caller's own
    active `sharing_links` row (docs/43 §6). Buckets, `group_by`, the
    `category_id` drill, zero-fill and the CSV rendering are byte-identical to
    `scope=own` — one param, one population, one math. See docs/08 §Report
    routes for the scope contract and its CSV-parity obligation.
    """
    _check_range(from_, to)
    scope_id, uncategorized_only = _parse_category_filter(category_id)
    report = reports_service.spending_report(
        db,
        current_user.id,
        from_,
        to,
        granularity,
        group_by=group_by,
        category_id=scope_id,
        uncategorized_only=uncategorized_only,
        # M159: the household scope changes WHO is summed, never the math.
        household=scope == "household",
    )
    if format == "csv":
        rows = list(report["items"])
        return _csv_response(
            "spending",
            from_,
            to,
            reports_service.report_csv(rows, _csv_fields("spending", group_by)),
        )
    return SpendingReport(**report)


@router.get("/spending/categories", response_model=CategoryBreakdownReport)
def spending_categories(
    from_: Annotated[dt.date, Query(alias="from")],
    to: Annotated[dt.date, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    format: CsvFormat = "json",
    group_by: reports_service.GroupBy = "category",
    scope: ReportScope = "own",
) -> Response | CategoryBreakdownReport:
    """The donut's slice set (docs/12 §2).

    `scope=household` (M159) sums the same population `GET /reports/spending`
    does under that scope — the caller's own rows plus the owner's allowlisted
    projection rows (docs/43 §6) — with the same merged category/tag namespace,
    so the two routes can never disagree about which rows a household holds.
    """
    _check_range(from_, to)
    report = reports_service.category_breakdown_report(
        db, current_user.id, from_, to, group_by=group_by, household=scope == "household"
    )
    if format == "csv":
        rows = list(report["items"])
        return _csv_response(
            "spending/categories",
            from_,
            to,
            reports_service.report_csv(rows, _csv_fields("spending/categories", group_by)),
        )
    return CategoryBreakdownReport(**report)


@router.get("/category-tag-matrix", response_model=CategoryTagMatrixReport)
def category_tag_matrix(
    from_: Annotated[dt.date, Query(alias="from")],
    to: Annotated[dt.date, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    format: CsvFormat = "json",
    scope: ReportScope = "own",
) -> Response | CategoryTagMatrixReport:
    """M124 R3 (docs/12 §1): which tags each category's spending carries.

    A category x tag cross-tab, so it answers "how much did I spend on
    Groceries tagged vacation?" without a second query. Cells count a
    transaction once PER TAG — see the service docstring — which is why the
    report also carries the once-counted `total_cad` the caption points at.

    `scope=household` (M179) widens ONLY who is summed — the caller's own
    committed rows plus the owner's allowlisted rows the caller's own active
    `sharing_links` row projects (docs/43 §6), the same population and the same
    contract as `GET /reports/spending`'s. The matrix is the Categories tab's
    second panel over that tab's population, so the Owner lens drives it too;
    the CSV twin re-runs the same computation over the same scope.
    """
    _check_range(from_, to)
    report = reports_service.category_tag_matrix(
        db, current_user.id, from_, to, household=scope == "household"
    )
    if format == "csv":
        rows = list(report["items"])
        return _csv_response(
            "category-tag-matrix",
            from_,
            to,
            reports_service.report_csv(rows, _CSV_FIELDS["category-tag-matrix"]),
        )
    return CategoryTagMatrixReport(**report)


@router.get("/cashflow", response_model=CashflowReport)
def cashflow(
    from_: Annotated[dt.date, Query(alias="from")],
    to: Annotated[dt.date, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    granularity: Granularity = "month",
    format: CsvFormat = "json",
    scope: OwnReportScope = "own",
) -> Response | CashflowReport:
    _check_range(from_, to)
    report = reports_service.cashflow_report(db, current_user.id, from_, to, granularity)
    if format == "csv":
        rows = list(report["buckets"])
        return _csv_response(
            "cashflow",
            from_,
            to,
            reports_service.report_csv(rows, ["bucket", "income_cad", "expenses_cad", "net_cad"]),
        )
    return CashflowReport(**report)


@router.get("/cashflow-calendar", response_model=CashflowCalendarReport)
def cashflow_calendar(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    month: Annotated[str | None, Query(description="YYYY-MM (default: current month)")] = None,
    scope: OwnReportScope = "own",
) -> CashflowCalendarReport:
    """Zero-filled month grid over bills + dividends + subscriptions (docs/36).

    Composition only: the three sources come from their own canonical
    functions, every query is user-scoped, and nothing here fetches. M170
    (docs/36 §M170): the `subscription` series is CONFIRMED state only —
    `services/confirmed_subscriptions.py` (active bills + recurring groups
    the user accepted), never the M27 detector's candidates — and the payload
    carries `confirmed_subscription_count` so the client can render the honest
    empty state when that set is empty.
    """
    today = today_et()
    month_start, month_end = _check_month(month, today)
    return CashflowCalendarReport(
        **build_cashflow_calendar(
            db,
            current_user.id,
            month_start=month_start,
            month_end=month_end,
            today=today,
        )
    )


@router.get("/cashflow-sankey", response_model=CashflowSankeyReport)
def cashflow_sankey(
    from_: Annotated[dt.date, Query(alias="from")],
    to: Annotated[dt.date, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    scope: OwnReportScope = "own",
) -> CashflowSankeyReport:
    """Income sources -> money in -> destinations (M77, docs/12 §Sankey).

    JSON-only: the graph is a chart input, not a flat table, so it carries no
    `format=csv` twin (same decision as the `/projections/*` routes). This
    route IS the answer the Reports page renders: M160-R2 (t_85311c34)
    re-pointed `SankeyPanel` here, and `lib/sankeyEngine.ts` — the browser
    twin the M77 parity goldens used to diff against — is now only the
    reference implementation, replayed against this route by
    `tests/test_m160_replay_m77.py`.

    M160-R2: consumed by the UI, so it left the M137 P1-1 orphan set — never
    gated by `SERVER_REPORTS_DEPRECATED`.
    """
    assert_report_route_live("/reports/cashflow-sankey")
    _check_range(from_, to)
    return CashflowSankeyReport(**reports_service.cashflow_sankey(db, current_user.id, from_, to))


@router.get("/total-return", response_model=TotalReturnReport)
def total_return(
    from_: Annotated[dt.date, Query(alias="from")],
    to: Annotated[dt.date, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    scope: OwnReportScope = "own",
) -> TotalReturnReport:
    """Time-weighted + money-weighted period returns (M77, docs/05).

    JSON-only (no `format=csv`); this route is the ONLY implementation since
    M160 (P2-5) retired `lib/totalReturnEngine.ts`, and M160-R2 (t_85311c34)
    re-pointed `TotalReturnMetrics` here — the frozen M77 golden replays
    against this route in `tests/test_m160_replay_m77.py`, and the
    hand-computed ledger lives in `tests/test_total_return.py`.

    M160-R2: consumed by the UI, so it left the M137 P1-1 orphan set — never
    gated by `SERVER_REPORTS_DEPRECATED`.
    """
    assert_report_route_live("/reports/total-return")
    _check_range(from_, to)
    return TotalReturnReport(
        **total_return_service.total_return_report(db, current_user.id, from_, to)
    )


@router.get("/net-worth", response_model=NetWorthReport)
def net_worth(
    from_: Annotated[dt.date, Query(alias="from")],
    to: Annotated[dt.date, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    granularity: Granularity = "month",
    format: CsvFormat = "json",
    scope: OwnReportScope = "own",
) -> Response | NetWorthReport:
    _check_range(from_, to)
    report = reports_service.net_worth_report(db, current_user.id, from_, to, granularity)
    if format == "csv":
        rows = list(report["buckets"])
        return _csv_response(
            "net-worth",
            from_,
            to,
            reports_service.report_csv(
                rows, ["bucket_end", "cash_cad", "positions_cad", "net_cad"]
            ),
        )
    return NetWorthReport(**report)


@router.get("/investment-performance", response_model=PerformanceReport)
def investment_performance(
    from_: Annotated[dt.date, Query(alias="from")],
    to: Annotated[dt.date, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    format: CsvFormat = "json",
    scope: OwnReportScope = "own",
) -> Response | PerformanceReport:
    _check_range(from_, to)
    report = reports_service.investment_performance_report(db, current_user.id, from_, to)
    if format == "csv":
        rows = list(report["items"])
        fields = [
            "security_id",
            "symbol",
            "quantity",
            "realized_cad",
            "unrealized_cad",
            "acb_pool_cad",
            "market_value_cad",
        ]
        return _csv_response(
            "investment-performance", from_, to, reports_service.report_csv(rows, fields)
        )
    return PerformanceReport(**report)


@router.get("/capital-gains", response_model=None)
def capital_gains(
    year: Annotated[int, Query(ge=1900, le=2200)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    format: Annotated[Literal["json", "csv", "html"], Query()] = "json",
    scope: OwnReportScope = "own",
) -> Response | CapitalGainsReport:
    report = capital_gains_report(db, current_user.id, year)
    if format == "csv":
        return Response(
            content=report_csv(report),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="capital-gains-{year}.csv"'},
        )
    if format == "html":
        return Response(
            content=report_html(report),
            media_type="text/html",
            headers={"Content-Disposition": f'inline; filename="capital-gains-{year}.html"'},
        )
    return CapitalGainsReport(**report)


@router.get("/dividends", response_model=DividendReport)
def dividends(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    year: Annotated[int | None, Query()] = None,
    format: CsvFormat = "json",
    scope: OwnReportScope = "own",
) -> Response | DividendReport:
    # M137 P1-1 orphan, retired as an orphan by M160-R2 (t_85311c34): the
    # Dividends panel reads this route, so it can never be gated again.
    assert_report_route_live("/reports/dividends")
    report = dividends_service.dividend_report(db, current_user.id, year)
    if format == "csv":
        return _csv_response(
            "dividends",
            dt.date(int(report["year"]), 1, 1),
            dt.date(int(report["year"]), 12, 31),
            reports_service.report_csv(
                list(report["items"]),
                [
                    "security_id",
                    "symbol",
                    "name",
                    "dividend_cad",
                    "distribution_cad",
                    "roc_cad",
                    "total_cad",
                    "payment_count",
                    "last_payment_date",
                ],
            ),
        )
    return DividendReport(**report)
