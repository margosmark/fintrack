"""mcp_audit — read the per-call receipts (M80, docs/40 §3).

The write path lives on the MCP seam (`POST /auth/internal-mcp-audit`); this
router is the first-party read: an account can always answer "what did my
agent do?" without shell access to the database. Tenancy is enforced by
`get_current_user` + a `user_id` filter on every query (AGENTS §2) — there is
no cross-account read of this table anywhere.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlmodel import func, select

from app.api.deps import MAX_OFFSET, get_current_user, get_db
from app.models.mcp_audit import McpAudit
from app.models.users import User
from app.schemas.mcp_audit import McpAuditPage

router = APIRouter(prefix="/mcp-audit", tags=["mcp-audit"])


@router.get("", response_model=McpAuditPage)
def list_mcp_audit(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    # Own query params (not `get_pagination`), so the M98b F-4 ceiling has to
    # be repeated here: without `le=` an `offset >= 2**63` reached Postgres as
    # a bigint OFFSET it rejects -> the catch-all 500, not a 422.
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> McpAuditPage:
    """Newest-first MCP tool-call receipts for the calling account.

    One row per `tools/call` the MCP server handled, including the calls it
    refused with a 403. `args_hash` is a sha256 digest — arguments themselves
    are never stored (they can carry a password).
    """
    where = McpAudit.user_id == current_user.id
    total = db.exec(select(func.count()).select_from(McpAudit).where(where)).one()
    rows = db.exec(
        select(McpAudit)
        .where(where)
        .order_by(McpAudit.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return McpAuditPage(items=rows, total=total)
