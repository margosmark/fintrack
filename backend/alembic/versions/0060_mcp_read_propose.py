"""M189: widen the mcp_tokens.policy vocabulary with `read_propose` (docs/59 §1).

Revision ID: 0060_mcp_read_propose
Revises: 0059_assistant (current head)
Create Date: 2026-11-10

The BYO-AI connector scope: read tools plus `proposals_create` and no domain
writes (docs/59 §2). A pure CHECK-constraint swap — `policy` stays a
VARCHAR(16) (`read_propose` fits), no table rewrite, no backfill: existing
`full` / `read_only` rows are already valid under the wider vocabulary.

Downgrade restores the two-value CHECK and therefore REFUSES any row still
minted as `read_propose` — revoke those tokens first (docs/59 §1).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0060_mcp_read_propose"
down_revision: str | None = "0059_assistant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_mcp_tokens_policy", "mcp_tokens", type_="check")
    op.create_check_constraint(
        "ck_mcp_tokens_policy",
        "mcp_tokens",
        "policy IN ('full', 'read_only', 'read_propose')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_mcp_tokens_policy", "mcp_tokens", type_="check")
    op.create_check_constraint(
        "ck_mcp_tokens_policy",
        "mcp_tokens",
        "policy IN ('full', 'read_only')",
    )
