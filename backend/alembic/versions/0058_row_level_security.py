"""row-level security for tenant isolation (docs/55)

Enables RLS on every tenant-owned table with policies keyed to the
per-request tenant GUC, scopes the sharing join tables through their tenant
parents, leaves the auth tables unguarded (their queries run before a
request identity exists), and grants the unprivileged app role
(`fintrack_app`, created by `scripts/provision_rls_role.py`) only the
privileges it serves. Application-level `tenant_id` filters all stay in
place — the database layer is the SECOND lock, never the only one
(docs/55 §Approach).

Revision ID: 0058_row_level_security
Revises: 0057_price_fetched_at
Create Date: 2026-10-30
"""

from alembic import op
from app.db.rls import apply_rls, revoke_rls

# postgresql varchar(32) caps alembic_version.version_num
revision: str = "0058_row_level_security"
down_revision: str | None = "0057_price_fetched_at"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the row-level security helpers and enable the policies"""
    apply_rls(op.get_bind())


def downgrade() -> None:
    """Remove the row-level security policies and helpers"""
    revoke_rls(op.get_bind())
