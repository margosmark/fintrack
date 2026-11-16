"""merge the PR-23 tenancy-index branch with the M168 provider-registry branch.

Revision ID: 0051_merge_two_heads
Revises: 0050_tenancy_idx, 0050_provider_fund_registry
"""

revision: str = "0051_merge_two_heads"
down_revision: tuple[str, str] = ("0050_tenancy_idx", "0050_provider_fund_registry")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
