"""Fund composition auto-enrichment (M147, docs/05 §Auto-enrichment).

A committed import batch that introduces a held fund with NO composition
rows schedules its enrichment in the background — through the SAME
provider chain the manual ``POST /securities/{id}/holdings/import``
endpoint uses (``market_data.import_fund_composition``). The exposure
pies never sit Unclassified because a button went unnoticed.

Background discipline (docs/07): the task opens its own session, every
failure degrades to a warning log — privacy mode's 403 included (docs/11
step 6: the fetch is blocked gracefully, the import itself never crashes).
M153 adds the other half: the task's session is RELEASED before every
provider wait (`market_data.release_before_provider_wait`), so a background
enrichment never parks a pool connection `idle in transaction` while the
provider answers — docs/01 § Sessions and the event loop.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session
from sqlmodel import Session as SQLModelSession
from sqlmodel import select

from app.api.deps import get_engine
from app.models.etf_holdings import EtfHolding
from app.models.securities import Security, SecurityKind
from app.services import market_data

_FUND_KINDS = frozenset({SecurityKind.etf, SecurityKind.mutual_fund})


def funds_needing_enrichment(
    db: Session, user_id: int, security_ids: list[int] | None = None
) -> list[Security]:
    """Fund-kind securities of this user with ZERO composition rows.

    Restricted to ``security_ids`` when given (a just-committed batch's
    securities); a security already carrying ANY row — manual or provider
    — is never auto-touched (manual-first convention, docs/02).
    """
    query = select(Security).where(Security.user_id == user_id, Security.kind.in_(_FUND_KINDS))
    if security_ids:
        query = query.where(Security.id.in_(security_ids))
    candidates = db.exec(query).all()
    return [
        fund
        for fund in candidates
        if db.exec(select(EtfHolding.id).where(EtfHolding.security_id == fund.id).limit(1)).first()
        is None
    ]


def _run_enrichment(security_id: int) -> None:
    """BackgroundTask body — opens its own session (request sessions are
    closed before background tasks run). Import the fund's own
    composition, then deepen into its sub-funds (M19) and refresh its
    fundamentals; anything that raises degrades to a warning log.
    """
    with SQLModelSession(get_engine(), expire_on_commit=False) as session:
        fund = session.get(Security, security_id)
        if fund is None:
            return
        # M153 (docs/01 §Sessions and the event loop): `session.get` above
        # autobegan a transaction, and the very next act of the write path is
        # a provider fetch. Hand the connection back first — the session is
        # `expire_on_commit=False`, so `fund` stays loaded and the release is
        # a no-op write-wise.
        market_data.release_before_provider_wait(session, fund)
        try:
            written = market_data.import_fund_composition(session, fund)
        except Exception:
            logging.getLogger(__name__).warning(
                "Fund composition enrichment blocked/failed for %s; continuing without it",
                fund.symbol,
            )
            return
        if not written:
            logging.getLogger(__name__).info(
                "No provider answered for %s; composition stays unclassified", fund.symbol
            )
            return
        symbols = [str(row.holding_symbol) for row in written if row.holding_symbol]
        try:
            market_data.ensure_sub_fund_composition(session, fund, symbols)
        except Exception:
            logging.getLogger(__name__).warning(
                "Sub-fund enrichment failed for %s; continuing without it", fund.symbol
            )
        try:
            market_data.ensure_fundamentals(session, fund, date.today())
        except Exception:
            logging.getLogger(__name__).warning(
                "Fundamentals enrichment failed for %s; continuing", fund.symbol
            )


def enqueue(
    db: Session, background_tasks: BackgroundTasks, user_id: int, security_ids: list[int]
) -> list[Security]:
    """Queue background composition enrichment for the batch's funds.

    Returns the funds queued (callers may log/report them); an empty
    list means nothing needed fetching. The candidate query runs inside
    the caller's request session — the fetches all happen later, in
    background tasks with their own sessions.
    """
    funds = funds_needing_enrichment(db, user_id, security_ids)
    for fund in funds:
        background_tasks.add_task(_run_enrichment, fund.id)
    return funds
