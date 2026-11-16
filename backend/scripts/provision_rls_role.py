"""Idempotently create the unprivileged request-path role (docs/55)

Run once per Postgres CLUSTER, as a role that holds CREATEROLE (the schema
owner `fintrack` is superuser on the deploy host):

    cd backend && uv run python scripts/provision_rls_role.py

Password resolution: APP_ROLE_PASSWORD when set, else the DATABASE_URL
password (the maintenance role's). Rotating it independently means setting
APP_ROLE_PASSWORD AND the password inside APP_DATABASE_URL together.

This creates the ROLE only. Per-database grants (CONNECT, schema, tables,
sequences, the RLS helpers) are applied by alembic revision
`0058_row_level_security`, so `alembic upgrade head` must follow on every
database the app serves — the api container already runs it before uvicorn
starts, and the test harness runs it per suite database. Startup refuses to
serve with a privileged or ungranted request role (app.core.rls), so a
database that skipped a step fails loudly instead of enforcing nothing.
"""

import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.rls import APP_ROLE


def main() -> int:
    settings = get_settings()
    url = make_url(settings.database_url)
    password = settings.app_role_password or (url.password or "")
    if not password:
        print(
            "refusing to provision a role with an empty password (set APP_ROLE_PASSWORD)",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            role_exists = connection.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                {"role": APP_ROLE},
            ).scalar_one()
            # Escaped literal, not a bind parameter: CREATE/ALTER ROLE are
            # utility statements and do not accept parameters.
            literal_password = password.replace("'", "''")
            try:
                if role_exists:
                    connection.execute(
                        text(f"ALTER ROLE {APP_ROLE} LOGIN PASSWORD '{literal_password}'")
                    )
                    print(f"updated login password for existing role {APP_ROLE}")
                else:
                    connection.execute(
                        text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{literal_password}'")
                    )
                    print(f"created role {APP_ROLE}")
            except Exception as exc:  # BLE001 is not enabled; classified below
                denied = (
                    "permission denied" in str(exc).lower()
                    or "must be superuser" in str(exc).lower()
                )
                if not denied:
                    raise
                if not role_exists:
                    print(
                        f"refusing: {url.username!r} lacks CREATEROLE on this cluster.\n"
                        "  Create the role as a superuser, e.g.:\n"
                        f"    psql -U postgres -c \"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '..'\"\n"
                        "  then re-run this script (or continue: the migrations skip"
                        " their\n  grants for a missing role and startup verification"
                        " will refuse to serve).",
                        file=sys.stderr,
                    )
                    return 2
                # The role EXISTS but this role may not ALTER it: PostgreSQL
                # 16+ requires CREATEROLE AND the ADMIN option on the target
                # (only whoever CREATEd it — a plain membership GRANT defaults
                # to no admin option — holds that). Continue: the existing
                # password may already be right, and a wrong one fails loudly
                # at boot (app.core.rls).
                print(
                    f"warning: {APP_ROLE} exists but {url.username!r} may not ALTER it"
                    f" ({exc.orig if getattr(exc, 'orig', None) else exc}).\n"
                    f"  To let this role rotate the password:"
                    f" GRANT {APP_ROLE} TO {url.username} WITH ADMIN OPTION;\n"
                    "  If APP_DATABASE_URL's password is wrong, boot refuses"
                    " loudly (docs/55).",
                    file=sys.stderr,
                )
    finally:
        engine.dispose()

    print(
        "next: `alembic upgrade head` on each database, then set"
        " APP_DATABASE_URL (docs/55 §Rollout)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
