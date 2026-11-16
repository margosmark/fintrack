"""dividend-calendar — upcoming expected dividend income (docs/08, docs/24 §M29).

`?format=csv` is this route's **row-currency export** (t_67b48642): one row per
projection with the headers pinned to the JSON field names this route emits.
The `currency` column travels with the money columns, so an exported row can
never state an amount without the denomination the ledger recorded it in, and a
withheld projection (mixed or unknown rows) exports all three of its money cells
empty together — the same all-or-nothing refusal the JSON contract and the panel
render as the app's dash (docs/24 §M29 "Row authority and refusal").

The export is the SAME computation as the JSON twin, never a second reader: it
serializes `compute_dividend_calendar`'s own `upcoming` list, so the exported
rows and the on-screen `summary.by_currency` subtotals are one population.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.reports import CsvFormat
from app.models.users import User
from app.services.dividend_calendar import compute_dividend_calendar
from app.services.reports import report_csv
from app.services.txn_filters import today_et

router = APIRouter(prefix="/reports", tags=["reports"])
OwnReportScope = Literal["own"]

# Headers are pinned to the field names `compute_dividend_calendar` emits, in
# the docs/24 §M29 wire-contract order (denomination beside its money).
CSV_FIELDS = [
    "security_id",
    "symbol",
    "frequency",
    "currency",
    "avg_amount",
    "annual_projection",
    "last_payment_date",
    "next_expected_date",
    "num_payments",
]


# `response_model=dict` keeps the published JSON schema exactly what the plain
# `-> dict` annotation derived before this route grew a `Response` twin: FastAPI
# refuses to derive a response field from `Response | dict`, and the union is
# only the export's return path — a `Response` instance bypasses serialization.
@router.get("/dividend-calendar", response_model=dict)
def dividend_calendar(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    format: CsvFormat = "json",
    scope: OwnReportScope = "own",
) -> Response | dict:
    """Upcoming expected dividend income (docs/24 §M29).

    `format=csv` renders the same `upcoming` rows the JSON answers with, money
    as two-decimal strings and `currency` on every row that carries a figure.
    There is no blended total in either format: the legacy scalar summary keys
    stay `null` and the CSV has no total row, so no client can print one number
    across denominations (docs/24 §M29 denomination ruling).
    """
    payload = compute_dividend_calendar(db, current_user.id)
    if format == "csv":
        return Response(
            content=report_csv(payload["upcoming"], CSV_FIELDS),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="dividend-calendar-{today_et().isoformat()}.csv"'
                )
            },
        )
    return payload
