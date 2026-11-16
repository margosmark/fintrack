"""Row-level security schema setup

`apply_rls` and `revoke_rls` compose the helper functions and the table
policies into the single pair the migration and the test harness call.
"""

from sqlalchemy import Connection

from app.db.rls.functions import (
    CURRENT_TENANT_EMAIL,
    CURRENT_TENANT_ID,
    PARTNER_VISIBLE_TENANTS,
    SHARING_LINK_IDENTITY_MATCHES,
    create_helper_functions,
    drop_helper_functions,
)
from app.db.rls.policies import (
    APP_ROLE,
    AUTH_TABLES,
    CHILD_TABLES,
    GLOBAL_READ_TABLES,
    SECURED_TABLES,
    apply_policies,
    drop_default_privileges,
    drop_policies,
    grant_auth_table,
    grant_global_read_table,
    secure_registered_table,
)

__all__ = [
    "APP_ROLE",
    "AUTH_TABLES",
    "CHILD_TABLES",
    "CURRENT_TENANT_EMAIL",
    "CURRENT_TENANT_ID",
    "GLOBAL_READ_TABLES",
    "PARTNER_VISIBLE_TENANTS",
    "SECURED_TABLES",
    "SHARING_LINK_IDENTITY_MATCHES",
    "apply_rls",
    "grant_auth_table",
    "grant_global_read_table",
    "revoke_rls",
    "secure_registered_table",
]


def apply_rls(connection: Connection) -> None:
    """Create the RLS helpers, enable the policies, and grant the app role access"""
    create_helper_functions(connection)
    apply_policies(connection)


def revoke_rls(connection: Connection) -> None:
    """Drop every policy, disable row-level security, and remove the helpers

    Also disarms the default privileges `apply_policies` set: they are not
    policies, so `drop_policies` cannot see them, and a downgrade that left
    them armed would hand every table the migrator RE-CREATES the app role's
    full CRUD on birth (docs/55 §Policies).
    """
    drop_policies(connection)
    drop_default_privileges(connection)
    drop_helper_functions(connection)
