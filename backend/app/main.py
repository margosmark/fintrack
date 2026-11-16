"""fin_tracker FastAPI application factory (docs/01)."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import (
    acb,
    accounts,
    assistant,
    auth,
    auth_oidc,
    auth_second_factor,
    backup,
    bill_reminders,
    budgets,
    categories,
    debts,
    dividend_calendar,
    export,
    forecast,
    fx,
    goals,
    health,
    imports,
    insights,
    institutions,
    mcp_audit,
    networth_snapshot,
    notifications,
    partner,
    portfolio,
    prices,
    projections,
    reconciliation,
    reports,
    rules,
    search,
    securities,
    sharing,
    spending_alerts,
    spending_comparison,
    subscriptions,
    suggestions,
    sync,
    tags,
    tax_alerts,
    tax_projection,
    trades,
    transactions,
    users,
    vault,
    watchlist,
    ynab,
)
from app.api.deps import require_docs_access
from app.core.config import get_settings
from app.core.errors import (
    APIError,
    api_error_handler,
    register_exception_handlers,
    validation_error_handler,
)
from app.core.ratelimit import limiter, rate_limit_exceeded_handler


class _StderrHandler(logging.Handler):
    """Writes to whatever `sys.stderr` is at EMIT time.

    `logging.StreamHandler` binds `sys.stderr` when constructed, which would
    freeze the interpreter's original stderr and bypass pytest's capture.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
        except Exception:  # - logging must never raise into a request
            self.handleError(record)


def _configure_app_logging() -> None:
    """Open the `app` tree to INFO on stderr — uvicorn never does it.

    uvicorn's default LOGGING_CONFIG configures ONLY the `uvicorn*` loggers and
    never the root logger, so every `logging.getLogger("app.*")` record fell
    through to `logging.lastResort`, which is WARNING-only. The result was
    silent: this module's own "Database connection verified" and the docs/07
    carry-forward record in `tax/acb.py` (which publication day priced a
    holiday trade — the audit line for a non-publication date) never reached
    `docker logs` at all. Propagation stays ON, so pytest's `caplog` keeps
    seeing exactly the records it saw before; only the destination changes.
    """
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    if not app_logger.handlers:
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        app_logger.addHandler(handler)


_configure_app_logging()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Seed the demo account at startup when enabled (w8-contract pin 3).

    Settings are read LIVE (not captured at import) so tests can flip
    SEED_DEMO_ACCOUNT via env/cache-clear. Failures are logged and swallowed:
    a broken seed must never crash boot.
    """
    # Rate limiting reads its enabled flag LIVE (M9 lesson) so tests can flip
    # RATE_LIMIT_ENABLED via env/cache-clear without rebuilding the app.
    limiter.enabled = get_settings().rate_limit_enabled

    # t_adfb7881 (docs/38 §Key provisioning): eagerly load BOTH RS256
    # signing-key families. A deployment with key paths set but the PEMs
    # unreadable inside the container (a bad/missing ./secrets mount) then
    # fails BOOT with a named error instead of coming up "healthy" and 500-ing
    # every login — and the boot log states file-backed vs EPHEMERAL per
    # family, which is the deploy-verification signal. Dev/test with empty
    # paths pays one cached ephemeral keygen per family here; the same
    # EPHEMERAL warning _load_family already logs now surfaces at boot instead
    # of at the first token operation.
    from app.core.jwt_keys import preload_signing_keys

    _key_sources = preload_signing_keys()  # RuntimeError on an unreadable configured path
    _boot_logger = logging.getLogger(__name__)
    for _family, _source in _key_sources.items():
        _boot_logger.info("JWT %s signing key: %s", _family, _source)

    # Startup DB connectivity check — fail fast if database is unreachable.
    from sqlalchemy import text as sql_text
    from sqlalchemy.exc import OperationalError

    from app.api.deps import get_engine

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        logging.getLogger(__name__).info("Database connection verified")
    except OperationalError as e:
        logging.getLogger(__name__).error(f"Database unavailable at startup: {e}")
        raise

    # Row-level security (docs/55): when APP_DATABASE_URL points the request
    # path at the unprivileged app role, verify the live connection is one the
    # policies can actually constrain — a request role that owns the tables,
    # is superuser, or is BYPASSRLS would serve while enforcing nothing. With
    # the URL unset this only logs the not-armed state.
    from app.api.deps import get_request_engine
    from app.core.rls import log_rls_status

    log_rls_status(get_request_engine())

    if get_settings().seed_demo_account:
        from app.services.demo_seed import seed_demo_from_engine

        try:
            seed_demo_from_engine()
        except Exception:
            logging.getLogger(__name__).exception("Demo account seeding failed; continuing")

    # M59 (docs/37): one in-process delivery worker. Tests force
    # NOTIFY_WORKER_ENABLED=false so every delivery step stays deterministic.
    from app.services.notify_delivery import start_worker, stop_worker

    delivery_worker = start_worker()

    # M150 (docs/07 §Scheduled refresh): one in-process FX rate-book refresh
    # worker, so the ≤7-day carry-forward is only ever spent on BoC's
    # publication calendar. Tests force FX_REFRESH_ENABLED=false, so the suite
    # never opens a Valet fetch behind a fixture's back.
    from app.services.fx_refresh import start_worker as start_fx_refresh
    from app.services.fx_refresh import stop_worker as stop_fx_refresh

    fx_refresh_worker = start_fx_refresh()

    # M173 (docs/07 §Benchmark series): one in-process worker keeping the four
    # vs-Benchmark series current (docs/28). Tests force
    # BENCHMARK_REFRESH_ENABLED=false, like the FX tick, so the suite never
    # opens a yfinance fetch behind a fixture's back.
    from app.services.benchmark_refresh import start_worker as start_benchmark_refresh
    from app.services.benchmark_refresh import stop_worker as stop_benchmark_refresh

    benchmark_refresh_worker = start_benchmark_refresh()
    from app.services.dividend_refresh import start_worker as start_dividend_refresh
    from app.services.dividend_refresh import stop_worker as stop_dividend_refresh

    dividend_refresh_worker = start_dividend_refresh()
    # M187 (docs/07 §Scheduled price refresh): one in-process worker keeping
    # TODAY's closes current on the session's wall-clock schedule — the
    # request fan-out stops at a close already dated today (§Market-hours
    # optimization), so without this the day's bar freezes at whatever the
    # first page load saw. Tests force PRICE_REFRESH_ENABLED=false, like the
    # other workers, so no background tick can fetch behind a fixture.
    from app.services.price_refresh import start_worker as start_price_refresh
    from app.services.price_refresh import stop_worker as stop_price_refresh

    price_refresh_worker = start_price_refresh()
    yield
    await stop_price_refresh(price_refresh_worker)
    await stop_dividend_refresh(dividend_refresh_worker)
    await stop_benchmark_refresh(benchmark_refresh_worker)
    await stop_fx_refresh(fx_refresh_worker)
    await stop_worker(delivery_worker)


def create_app() -> FastAPI:
    settings = get_settings()
    # M87 (docs/42): FastAPI serves /docs, /redoc and /openapi.json OPEN by
    # default. They are re-registered below behind `require_docs_access`
    # instead, so the guard — not an accident of framework defaults — decides
    # who sees the API map. `app.openapi()` itself still builds the schema.
    app = FastAPI(
        title="fin_tracker API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Host-header allowlist (docs/42). Off unless ALLOWED_HOSTS is set: a wrong
    # allowlist 400s EVERY request, healthcheck included, so it must never be
    # enabled implicitly. See `Settings.allowed_host_list`.
    if settings.allowed_host_list:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_docs_access)])
    def openapi_json() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(require_docs_access)])
    def docs_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title="fin_tracker API")

    @app.get("/redoc", include_in_schema=False, dependencies=[Depends(require_docs_access)])
    def redoc_ui() -> HTMLResponse:
        return get_redoc_html(openapi_url="/openapi.json", title="fin_tracker API")

    app.include_router(budgets.router, prefix="/api/v1")
    app.include_router(tax_alerts.router, prefix="/api/v1")
    app.include_router(insights.router, prefix="/api/v1")
    app.include_router(dividend_calendar.router, prefix="/api/v1")
    app.include_router(spending_comparison.router, prefix="/api/v1")
    app.include_router(notifications.router, prefix="/api/v1")
    app.include_router(reconciliation.router, prefix="/api/v1")
    app.include_router(goals.router, prefix="/api/v1")
    app.include_router(networth_snapshot.router, prefix="/api/v1")
    app.include_router(bill_reminders.router, prefix="/api/v1")
    app.include_router(spending_alerts.router, prefix="/api/v1")
    app.include_router(tax_projection.router, prefix="/api/v1")
    app.include_router(debts.router, prefix="/api/v1")
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(auth_second_factor.router, prefix="/api/v1")
    app.include_router(auth_oidc.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(categories.router, prefix="/api/v1")
    app.include_router(tags.router, prefix="/api/v1")
    app.include_router(transactions.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(rules.router, prefix="/api/v1")
    app.include_router(subscriptions.router, prefix="/api/v1")
    app.include_router(suggestions.router, prefix="/api/v1")
    app.include_router(institutions.router, prefix="/api/v1")
    app.include_router(accounts.router, prefix="/api/v1")
    # Registered BEFORE imports: the generic /imports/{batch_id}/commit route
    # would otherwise swallow POST /imports/ynab/commit as batch_id="ynab".
    app.include_router(ynab.router, prefix="/api/v1")
    app.include_router(imports.router, prefix="/api/v1")
    app.include_router(sync.router, prefix="/api/v1")
    app.include_router(securities.router, prefix="/api/v1")
    app.include_router(trades.router, prefix="/api/v1")
    app.include_router(prices.router, prefix="/api/v1")
    app.include_router(fx.router, prefix="/api/v1")
    app.include_router(portfolio.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(projections.router, prefix="/api/v1")
    app.include_router(forecast.router, prefix="/api/v1")
    app.include_router(assistant.router, prefix="/api/v1")
    app.include_router(acb.router, prefix="/api/v1")
    app.include_router(export.router, prefix="/api/v1")
    app.include_router(backup.router, prefix="/api/v1")
    app.include_router(watchlist.router, prefix="/api/v1")
    app.include_router(vault.router, prefix="/api/v1")
    app.include_router(mcp_audit.router, prefix="/api/v1")
    # M90 (docs/43): sharing owns the consent surface (incl. the invitee's
    # one mutation); partner is the five-GET projection. Mounted after the
    # domain routers — neither path collides with any existing prefix.
    app.include_router(sharing.router, prefix="/api/v1")
    app.include_router(partner.router, prefix="/api/v1")

    # Every error uses the {detail, code} envelope (docs/08).
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    # Catch-all: unhandled exceptions return the envelope, never a raw 500.
    register_exception_handlers(app)

    return app


app = create_app()
