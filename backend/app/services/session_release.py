"""The one release seam for the held-session-across-provider-wait class (M175).

`release_before_provider_wait` lived in `services/market_data.py` because the
first paths it served were market-data paths (M153's enrichment chain, M155/
M161's price paths). It is not a market-data thing: it is the session rule of
docs/01 §Sessions and the event loop — **a `Session` is never held across an
unbounded provider wait** — and M175 found the same shape on paths that never
touch `market_data`: the fx client's fetch-on-miss (`services/fx.py`), the
benchmark sweep, the deep-price sweep, the split repair, the LLM batch worker
(60 s egress), the notification dispatcher's SMTP/webhook sends, and four
request routes.

So the canonical home is HERE, and `market_data.release_before_provider_wait`
stays as a re-export so every documented call site keeps working. The full
ledger of provider waits — which caller releases, which one deliberately does
not — is docs/07 §Provider-wait ledger (M175), and
`tests/test_provider_wait_ledger.py` fails when a new provider call appears in
`app/` without being classified there.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

__all__ = ["release_before_provider_wait"]


def release_before_provider_wait(session: Session, security: object | None = None) -> None:
    """Hand the pool connection back before a provider wait (M153/M155,
    docs/01 § Sessions and the event loop, docs/07 §Price-path sessions).

    Every seam on these paths READS first — `session.get`, a `select` — and
    then waits on the network. SQLAlchemy autobegins on that read, so without
    this the connection sits `idle in transaction` for the whole fetch: the
    exact row M148 drained the pool with, and the row M152's prod sweep caught
    on a perfectly healthy operator run. A background task is the SERVING path
    — `api/imports.py` and `api/trades.py` both queue
    `fund_enrichment._run_enrichment` as a FastAPI `BackgroundTask`, and the
    price fan-out queues `_run_backfill` / `_run_ensure_recent_price` the same
    way — so each parked wait is one pool connection nobody can take back
    until the provider answers. On a REQUEST path it is the same connection
    held `idle in transaction` instead of `idle`, which is the row vacuum
    waits on.

    ``commit()`` is the release, and it is a no-op write-wise: every write
    these seams make already commits itself (`import_fund_composition`'s
    delete+insert, `ensure_fundamentals`, `record_quote_outcome`,
    `_cache_closes`, `benchmarks._cache_closes`, `fx._store_fetched`), so
    nothing of the caller's is ever uncommitted at a point this is called.
    That is the CONTRACT, and it is the caller's to keep: release a session
    that has nothing pending, never one carrying uncommitted work. The callers
    that break it are deliberate and are named in docs/07 §Provider-wait
    ledger — `services/symbol_repair.repair_symbol` repoints a symbol and
    drops stale closes inside one transaction that `scripts/repair_symbol.py`
    rolls back for a dry run, and `tax/acb.py::FxRateBook` fetches rates
    inside `rebuild_user_pools`, which docs/02 says NEVER commits (that is why
    `fx.get_rates_between` releases only on its `commit=True` side).

    The one thing a plain ``commit()`` would change is OBJECT state — it
    expires every loaded ORM instance, and the next attribute read
    silently re-opens the transaction we just released. So the
    flag is pinned off for the duration of the commit and restored, which
    makes the release correct on any caller's session and not only on the
    background-task sessions that already configure ``expire_on_commit=False``.

    ``security`` is the row the provider call is about to read off. It is
    touched HERE, inside whatever transaction is still open, so the release is
    the last database contact before the wait instead of the lazy load its
    attribute access would otherwise trigger. Any ORM row works (a `Security`
    on the price/enrichment paths, a `NotificationDelivery`'s user, nothing at
    all where the wait reads no row).
    """
    if security is not None:
        # Read the attributes every provider seam below takes off a Security:
        # touching them inside the open transaction is what keeps the release
        # ahead of the wait rather than behind it.
        _ = (
            getattr(security, "id", None),
            getattr(security, "symbol", None),
            getattr(security, "user_id", None),
            getattr(security, "dead_symbol", None),
        )
    if not session.in_transaction():
        return
    expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        session.commit()
    finally:
        session.expire_on_commit = expire_on_commit
