"""M137 P1-1 — the orphan-report deprecation switch (docs/33 §Server-first
computing, docs/08 §Report routes).

Four `/reports/*` routes once had no UI consumer: `/dividends`,
`/cashflow-sankey` and `/total-return` (M134 §3.6.2's three orphans) plus
`/networth-snapshot`, the fourth @qabot found by re-counting the prefix. They
were the pivot's cheapest slice — retiring them desyncs nothing.

M160-R2 (t_85311c34) consumed the first three for real: `DividendsPanel`,
`SankeyPanel` and `TotalReturnMetrics` now read their own route (thin reads in
`frontend/src/api/reports.ts`), so they MOVED OUT of the orphan set — a live
screen route can never be gateable. `/reports/networth-snapshot` remains the
one orphan.

`SERVER_REPORTS_DEPRECATED` defaults OFF: without it every route keeps
answering, so the flag can never turn a working deployment into a broken one.
ON → 410 Gone with an envelope that names the route and points at the
surviving set. The routes the UI consumes are deliberately NOT run through
this guard — they are the ones the screens depend on.
"""

from app.core.config import get_settings
from app.core.errors import api_error

#: The one remaining zero-consumer route, in docs/08's order.
ORPHAN_REPORT_ROUTES = ("/reports/networth-snapshot",)


def assert_report_route_live(route: str) -> None:
    """410 when the operator retired this orphan; a no-op otherwise.

    Read LIVE (`get_settings()` at call time, AGENTS §5) so a test can flip the
    flag without restarting the app.
    """
    if route not in ORPHAN_REPORT_ROUTES:
        # Belt and braces: a re-pointed route must never be gateable.
        return
    if get_settings().server_reports_deprecated:
        raise api_error(
            410,
            f"{route} is gone — no UI surface ever consumed it, and the server-first "
            "pivot retired it. See docs/08 §Report routes for the surviving "
            "/reports/* set and docs/33 §Server-first computing.",
            "report_route_deprecated",
        )
