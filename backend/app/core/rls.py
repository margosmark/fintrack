"""Per-request tenant context for row-level security (docs/55)

The database policies (app/db/rls) read the session GUC `app.tenant_id`.
This module is the only writer of that GUC on the request path:

* `bind_request_tenant(user_id)` — publishes the authenticated tenant into a
  ContextVar; the engine-level `begin` listener stamps it transaction-locally
  (`set_config(..., is_local=true)`) on whichever connection the request's
  next transaction begins on. Transaction-local means the value dies with
  the transaction: a pooled connection can never carry one request's tenant
  into the next request, and an unstamped transaction reads NULL (which
  matches no rows — fail closed, never fail open).
* `set_tenant_now(session, user_id)` — for the two flows that must write
  tenant rows BEFORE any identity exists (register and the demo seed): binds
  the ContextVar AND stamps the live transaction immediately, so rows added
  later in the same transaction pass their own WITH CHECK.
* `verify_request_role(engine)` — startup guard. RLS only constrains roles
  that are not the table owner, not superusers, and not BYPASSRLS; a request
  engine pointed at any of those would silently enforce nothing, so when
  `app_database_url` is configured the app refuses to boot unless the live
  connection is a genuinely constrained role that holds the expected grants.

Background workers, scripts, operator psql and alembic keep connecting as
the schema owner (DATABASE_URL) and are deliberately outside every policy —
they are system paths, not request paths, and the app-level tenant filters
stay on their queries exactly as before (docs/55 §Out of scope).
"""

import logging
from contextvars import ContextVar

from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The GUC the policies read; schema-qualified constant shared with the DDL.
TENANT_GUC = "app.tenant_id"

# The authenticated principal for the current request, or "" when there is
# none. Set ONLY from a verified token (get_current_user / the SSE variant)
# or from the pre-identity seed helpers — never from request input.
tenant_ctx: ContextVar[str] = ContextVar("fintrack_rls_tenant", default="")


def bind_request_tenant(user_id: int) -> None:
    """Publish the request's tenant so every new transaction stamps it"""
    tenant_ctx.set(str(user_id))


def clear_request_tenant() -> None:
    """Forget the tenant (test teardown / defensive resets)"""
    tenant_ctx.set("")


def set_tenant_now(session: Session, user_id: int) -> None:
    """Bind the tenant AND stamp it onto the session's live transaction

    For flows that already opened a transaction before an identity existed
    (the register and demo-seed paths): the `begin` listener stamped "" when
    the transaction started, and a transaction-local GUC can only be changed
    from now on — set_config applies to the rest of THIS transaction, which
    is exactly the window the subsequent tenant-row writes run in.
    """
    bind_request_tenant(user_id)
    session.execute(
        text(f"SELECT set_config('{TENANT_GUC}', :tenant, true)"),
        {"tenant": str(user_id)},
    )


def stamp_open_transaction(connection: Connection) -> None:
    """Engine `begin` listener: stamp the current request's tenant

    Fires whenever a transaction begins on a pooled connection, so every
    transaction — including one a session opens long after checkout — starts
    from the CURRENT request's tenant, and the previous transaction's value
    is already gone with that transaction.
    """
    connection.execute(
        text(f"SELECT set_config('{TENANT_GUC}', :tenant, true)"),
        {"tenant": tenant_ctx.get()},
    )


def attach_request_stamping(engine: Engine) -> None:
    """Make every transaction on `engine` carry the current request's tenant"""
    event.listen(engine, "begin", stamp_open_transaction)


def verify_request_role(engine: Engine) -> None:
    """Refuse to serve requests from a connection RLS cannot constrain

    Raises:
        RuntimeError: the request engine's role is the schema owner, a
            superuser, BYPASSRLS, or lacks the granted access — any of which
            would let the app boot while tenant isolation silently does
            nothing (docs/55 §Verification).
    """
    with engine.connect() as connection:
        role_name, is_superuser, bypasses_rls, owns_public_table, can_read_transactions = (
            connection.execute(
                text(
                    """
                SELECT current_user,
                       current_setting('is_superuser') = 'on',
                       (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user),
                       EXISTS (
                           SELECT 1 FROM pg_tables
                           WHERE schemaname = 'public' AND tableowner = current_user
                       ),
                       has_table_privilege(current_user, 'public.transactions', 'SELECT')
                """
                )
            ).one()
        )

    if is_superuser or bypasses_rls or owns_public_table:
        raise RuntimeError(
            "Refusing to start: requests must use the unprivileged app role with"
            " row-level security (docs/55), but the configured request connection"
            f" is role={role_name!r} superuser={is_superuser} bypassrls={bypasses_rls}"
            f" owns_public_tables={owns_public_table}."
            " Point APP_DATABASE_URL at the provisioned app role"
            " (backend/scripts/provision_rls_role.py), not at the schema owner."
        )
    if not can_read_transactions:
        raise RuntimeError(
            f"Refusing to start: the request role {role_name!r} cannot read public.transactions"
            " — the RLS grants have not been applied to this database. Run"
            " backend/scripts/provision_rls_role.py, then `alembic upgrade head` (docs/55)."
            " If the database is ALREADY at head, that upgrade re-applies nothing —"
            " replay 0058 as the schema owner: `alembic downgrade -1`, then"
            " `upgrade head`, with no concurrent readers (its downgrade takes"
            " ACCESS-EXCLUSIVE locks)."
        )
    logger.info("Request role %r verified: constrained by row-level security", role_name)


def log_rls_status(request_engine: Engine) -> None:
    """Startup line naming whether the second lock is actually armed"""
    from app.core.config import get_settings

    if get_settings().app_database_url:
        verify_request_role(request_engine)
    else:
        logger.warning(
            "APP_DATABASE_URL is not configured: request sessions share the schema-owner"
            " role and row-level security is NOT enforced (the pre-RLS behaviour; docs/55"
            " §Rollout)"
        )
