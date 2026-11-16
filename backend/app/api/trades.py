"""trades — CRUD over the action enum (docs/02, docs/05, docs/08).

Signed-quantity convention (pinned for all future milestones): buy/drip store
POSITIVE quantity, sell stores NEGATIVE; cash-only actions (income/cash-flow)
carry quantity=0 with price_native holding the gross cash amount; commission
applies only to buy | sell | drip. corporate_action_adjustment is accepted and
stored as given — its semantics are defined in docs/06 / milestone M5.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session
from sqlmodel import select

from app.api._offloop import blocking
from app.api.deps import (
    Pagination,
    get_current_user,
    get_db,
    get_pagination,
    owned_or_404,
    paginate,
)
from app.core.errors import api_error
from app.core.ratelimit import IMPORT_UPLOAD_LIMIT, human_paced_limit
from app.importers.base import RawTrade, normalized_payee
from app.importers.brokerage_profiles import (
    ProfileError,
    detect_profile,
    parse_brokerage_csv,
)
from app.importers.investment_profile import (
    InvestmentCsvProfileError,
    parse_investment_csv,
)
from app.importers.investment_profile import (
    UnmappedRow as UnmappedRowData,
)
from app.models.accounts import INVESTMENT_ACCOUNT_TYPES, Account
from app.models.dividend_history import (
    DividendHistory,
    DividendHistoryRefresh,
    DividendHistoryState,
)
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.securities import Security, SecurityKind
from app.models.trades import CASH_ONLY_ACTIONS, COMMISSION_ACTIONS, Trade, TradeAction
from app.models.transaction_events import TransactionEventKind
from app.models.transactions import Transaction
from app.models.users import User
from app.schemas.imports import CommitBatchBody, ImportBatchPublic
from app.schemas.trades import (
    DividendRecordInput,
    DividendRecordResult,
    DividendSuggestion,
    TradeCreate,
    TradeDryRunResponse,
    TradeDryRunRow,
    TradeDryRunSummary,
    TradeImportResponse,
    TradePage,
    TradePreviewRow,
    TradeRead,
    TradeUpdate,
    UnmappedRow,
)
from app.services import dividend_provenance, fund_enrichment, market_data
from app.services.dividend_refresh import enqueue_due
from app.services.trade_filters import committed_trade_conditions
from app.services.tx_events import record_event
from app.tax.acb import rebuild_user_pools

router = APIRouter(prefix="/trades", tags=["trades"])


def _refuse_when_encrypted(user: User, capability: str) -> None:
    """M181 (docs/33 § "OUTSIDE THE ENVELOPE (M79)"): fail closed.

    `trades` has NO cipher columns at all (docs/33 PLANNED table), and
    `record_dividend` also writes a mandatory-plaintext `transactions` row — so
    for a tenant whose `users.encryption_required` is on, no server-side
    brokerage write can satisfy the ciphertext contract. Refuse the whole
    request before the first `db.add`, exactly like `api/ynab.py::commit_ynab`.
    The copy names the capability and the reason; it never offers "unlock the
    vault" as a remedy, because unlocking does not make a server-side write able
    to emit ciphers (docs/33 §Server-first computing rule 7d).
    """
    if user.encryption_required:
        raise api_error(
            422,
            f"{capability} is unavailable while client-side encryption is enabled: "
            "this path runs server-side and cannot safely write encrypted data, so "
            "nothing was saved",
            "encryption_required",
        )


def _related_or_404(db: Session, user: User, account_id: int, security_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None or account.user_id != user.id:
        raise api_error(404, "Account not found", "not_found")
    security = db.get(Security, security_id)
    if security is None or security.user_id != user.id:
        raise api_error(404, "Security not found", "not_found")
    return account


def _normalize(action: TradeAction, quantity: Decimal) -> Decimal:
    if action in CASH_ONLY_ACTIONS:
        return Decimal("0")
    if action in (TradeAction.buy, TradeAction.drip):
        return abs(quantity)
    if action == TradeAction.sell:
        return -abs(quantity)
    return quantity  # corporate_action_adjustment stored as given


def _apply(action: TradeAction, quantity: Decimal, commission: Decimal) -> tuple[Decimal, Decimal]:
    quantity = _normalize(action, quantity)
    if action not in COMMISSION_ACTIONS:
        commission = Decimal("0")
    return quantity, commission


def _investment_account_or_422(account: Account) -> None:
    """W11 gating: only investment-capable accounts may hold securities."""
    if account.type not in INVESTMENT_ACCOUNT_TYPES:
        raise api_error(
            422,
            f"Account '{account.name}' is a {account.type.value} account and "
            "cannot hold securities",
            "account_not_investment",
        )


def _negative_contribution_or_422(action: TradeAction, price_native: Decimal) -> None:
    """M131: a contribution is money IN — a negative amount is refused outright.

    The importer answers this 422 for the offending CSV row (docs/03); the
    manual create/update paths answer it here so NO write path can store the
    row class that corrupted account 731's contribution math. A withdrawal is
    the honest spelling of money leaving.
    """
    if action == TradeAction.contribution and price_native < 0:
        raise api_error(
            422,
            f"A contribution cannot be negative ({price_native}) — record it as a withdrawal",
            "negative_contribution",
        )


# --- List / Create trades (no path params — MUST come before /{trade_id}) -----


@router.get("", response_model=TradePage)
def list_trades(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[Pagination, Depends(get_pagination)],
    account_id: Annotated[int | None, Query()] = None,
    security_id: Annotated[int | None, Query()] = None,
    action: Annotated[TradeAction | None, Query()] = None,
    committed_only: Annotated[bool, Query()] = False,
) -> TradePage:
    filters = [Trade.user_id == current_user.id]
    if account_id is not None:
        filters.append(Trade.account_id == account_id)
    if security_id is not None:
        filters.append(Trade.security_id == security_id)
    if action is not None:
        filters.append(Trade.action == action)
    stmt = select(Trade)
    if committed_only:
        # Opt-in for derived readers (docs/03): staged import rows stay
        # visible by default — this list is also the review window — but the
        # report input loaders must see exactly what the report SQL sees.
        stmt = stmt.outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id).where(
            *committed_trade_conditions(current_user.id)
        )
    rows, total = paginate(
        db,
        stmt.where(*filters).order_by(Trade.date, Trade.id),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return TradePage(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("", response_model=TradeRead, status_code=201)
def create_trade(
    payload: TradeCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Trade:
    account = _related_or_404(db, current_user, payload.account_id, payload.security_id)
    _investment_account_or_422(account)
    # M181: ownership/account checks first, then the E2EE write boundary — a
    # hand-entered trade lands in the same cipher-less `trades` rows the import
    # refusal blocks, so it is refused for the same reason (docs/33).
    _refuse_when_encrypted(current_user, "Recording a trade")
    _negative_contribution_or_422(payload.action, payload.price_native)
    quantity, commission = _apply(payload.action, payload.quantity, payload.commission)
    trade = Trade(
        user_id=current_user.id,
        account_id=payload.account_id,
        security_id=payload.security_id,
        action=payload.action,
        date=payload.date,
        quantity=quantity,
        price_native=payload.price_native,
        commission=commission,
        fx_rate_used=payload.fx_rate_used,
        # M132 (docs/02 §trades): a hand-entered trade states its price in the
        # ACCOUNT's currency — the create form carries no currency field — so
        # the row records that explicitly instead of leaving the cash leg to
        # assume it.
        currency=account.currency,
    )
    db.add(trade)
    rebuild_user_pools(db, current_user.id)
    db.commit()
    db.refresh(trade)
    return trade


# --- Dividend suggestions + recording (named paths before /{trade_id}) --------


def _history_state_header(
    *,
    pending: int,
    unavailable: int,
    empty: int,
    stale: int,
    quote_miss: int,
    missing: int,
    as_of: datetime | None,
    age_days: int | None,
) -> str:
    """The additive freshness/provenance channel this route answers with
    (docs/07 §M184).

    Counts are per distinct tenant/security source, `as_of` is the OLDEST
    successful refresh in scope (a view is only as fresh as its least fresh
    evidence) and `source` names the provider those events came from — the
    `dividend_history.source = 'yfinance'` CHECK makes that a schema fact.
    `quote_miss` (M186) is the subset of `unavailable` that is a verdict about
    the SYMBOL, `missing` the sources that have never answered at all. The
    body stays `DividendSuggestion[]`: a client that ignores this header loses
    exactly what it had before.
    """
    return json.dumps(
        {
            "pending": pending,
            "unavailable": unavailable,
            "empty": empty,
            "stale": stale,
            "quote_miss": quote_miss,
            "missing": missing,
            "source": dividend_provenance.DIVIDEND_EVENT_SOURCE,
            "as_of": as_of.isoformat() + "Z" if as_of is not None else None,
            "age_days": age_days,
        },
        separators=(",", ":"),
    )


@router.get("/dividend-suggestions", response_model=list[DividendSuggestion])
def dividend_suggestions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
    account_id: Annotated[int | None, Query()] = None,
) -> list[DividendSuggestion]:
    """Cache-first pending dividend estimates for currently-open positions.

    For each (account, security) position that is still open (net quantity > 0),
    fetch dividend history and suggest only dates the user actually held shares on
    — using the point-in-time share count — and exclude dates that already have a
    matching income trade on that account.
    """
    # 1. Load every trade for the user, in date order, so we can derive both the
    #    current open position and the point-in-time balance held on any dividend
    #    date. Trade.quantity is already signed (buy/drip +, sell -, cash-only and
    #    corporate-action rows as given), so the running sum is the share balance.
    #    Committed/manual trades only: a staged import must not make a
    #    non-existent position look open, nor mark a dividend date as already
    #    recorded before the user commits it.
    trades_query = (
        select(Trade.account_id, Trade.security_id, Trade.date, Trade.quantity)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*committed_trade_conditions(current_user.id))
    )
    if account_id is not None:
        trades_query = trades_query.where(Trade.account_id == account_id)
    trade_rows = db.exec(trades_query.order_by(Trade.date)).all()  # type: ignore[attr-defined]

    # 2. Build a per (account, security) timeline of signed deltas and the current
    #    net quantity. Cash-only income trades carry quantity 0 and are no-ops.
    #    Open position = net > 0 ("position is still open").
    timelines: dict[tuple[int, int], list[tuple[date, Decimal]]] = defaultdict(list)
    current_net: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in trade_rows:
        delta = row.quantity
        key = (row.account_id, row.security_id)
        timelines[key].append((row.date, delta))
        current_net[key] += delta

    open_positions = [k for k, net in current_net.items() if net is not None and net > 0]
    if not open_positions:
        response.headers["X-Dividend-History-State"] = _history_state_header(
            pending=0,
            unavailable=0,
            empty=0,
            stale=0,
            quote_miss=0,
            missing=0,
            as_of=None,
            age_days=None,
        )
        return []

    # 3. Load securities for the open positions.
    security_ids = {sec_id for _, sec_id in open_positions}
    securities = {
        s.id: s
        for s in db.exec(
            select(Security).where(
                Security.user_id == current_user.id,
                Security.id.in_(security_ids),
            )
        ).all()
    }
    # Do not create metadata or refresh jobs for a security id referenced by a
    # malformed cross-tenant trade row; account/security ownership is bounded
    # by the security's tenant as well as the ledger's user_id.
    security_ids = {security_id for security_id in security_ids if security_id in securities}

    # 4. Recorded income trades: (account_id, security_id, date) triples.
    #    Committed/manual only — a staged dividend row must not suppress the
    #    suggestion for the real one the user records after committing.
    existing = db.exec(
        select(Trade.account_id, Trade.security_id, Trade.date)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(
            *committed_trade_conditions(current_user.id),
            Trade.action.in_(
                [
                    TradeAction.dividend,
                    TradeAction.distribution,
                    TradeAction.return_of_capital,
                ]
            ),
        )
    ).all()
    recorded: set[tuple[int, int, str]] = {(row[0], row[1], row[2].isoformat()) for row in existing}

    # 5. Read raw tenant-scoped provider events and refresh metadata. Enqueue
    #    due work but never wait for a provider in this foreground request.
    refresh_rows: dict[int, DividendHistoryRefresh] = {}
    histories: dict[int, list[DividendHistory]] = {}
    if not current_user.privacy_mode:
        refresh_rows = enqueue_due(db, current_user.id, security_ids)
        db.commit()
    else:
        refresh_rows = {
            row.security_id: row
            for row in db.exec(
                select(DividendHistoryRefresh).where(
                    DividendHistoryRefresh.user_id == current_user.id,
                    DividendHistoryRefresh.security_id.in_(security_ids),
                )
            ).all()
        }
        for sec_id in security_ids:
            refresh_rows.setdefault(
                sec_id,
                DividendHistoryRefresh(
                    user_id=current_user.id,
                    security_id=sec_id,
                    state=DividendHistoryState.unavailable,
                    error_code="privacy_mode_blocks_external_api",
                ),
            )
    for item in db.exec(
        select(DividendHistory)
        .where(
            DividendHistory.user_id == current_user.id,
            DividendHistory.security_id.in_(security_ids),
            DividendHistory.source == "yfinance",
        )
        .order_by(DividendHistory.security_id, DividendHistory.provider_order)
    ).all():
        histories.setdefault(item.security_id, []).append(item)
    now = datetime.now(UTC).replace(tzinfo=None)
    pending = sum(
        row.state in (DividendHistoryState.pending, DividendHistoryState.refreshing)
        for row in refresh_rows.values()
    )
    unavailable = sum(
        row.state == DividendHistoryState.unavailable for row in refresh_rows.values()
    )
    # M186 observability: the subset of unavailable sources whose failure is a
    # verdict about the SYMBOL (hard Yahoo quote miss, or the M141 dead-symbol
    # flag) rather than the network — the case where retrying cannot help.
    quote_miss = sum(
        row.state == DividendHistoryState.unavailable
        and row.error_code in ("quote_not_found", "dead_symbol")
        for row in refresh_rows.values()
    )
    empty = sum(row.state == DividendHistoryState.empty for row in refresh_rows.values())
    stale = sum(
        row.last_success_at is not None and row.last_success_at < now - timedelta(hours=24)
        for row in refresh_rows.values()
    )
    # M-provenance: a source that has NEVER answered has no events to show —
    # distinct from `unavailable` (an answer we could not read) and from
    # `stale` (an answer older than the 24-hour refresh window). Retained
    # last-good history counts as answered, never as missing.
    missing = sum(row.last_success_at is None for row in refresh_rows.values())
    as_of_values = [row.last_success_at for row in refresh_rows.values() if row.last_success_at]
    oldest = min(as_of_values) if as_of_values else None
    response.headers["X-Dividend-History-State"] = _history_state_header(
        pending=pending,
        unavailable=unavailable,
        empty=empty,
        stale=stale,
        quote_miss=quote_miss,
        missing=missing,
        as_of=oldest,
        age_days=(now.date() - oldest.date()).days if oldest is not None else None,
    )
    suggestions: list[DividendSuggestion] = []
    for key in open_positions:
        account_id, sec_id = key
        security = securities.get(sec_id)
        if security is None:
            continue
        symbol = security.symbol
        history = histories.get(sec_id, [])
        if not history:
            continue

        # Cumulative balance per (sorted) trade date for point-in-time lookup.
        cumulative: list[tuple[date, Decimal]] = []
        running = Decimal("0")
        for trade_date, delta in sorted(timelines[key], key=lambda item: item[0]):
            running += delta
            cumulative.append((trade_date, running))

        for entry in history:
            date_str = entry.ex_date.isoformat()
            if (account_id, sec_id, date_str) in recorded:
                continue
            div_date = date.fromisoformat(date_str)
            # Shares held on the dividend date (trades on that date included).
            held = Decimal("0")
            for trade_date, balance in cumulative:
                if trade_date <= div_date:
                    held = balance
                else:
                    break
            if held <= 0:
                continue
            per_share = Decimal(format(Decimal(entry.amount).normalize(), "f"))
            gross = per_share * held
            suggestions.append(
                DividendSuggestion(
                    security_id=sec_id,
                    account_id=account_id,
                    symbol=symbol,
                    date=date_str,
                    amount=per_share,
                    quantity=str(held.quantize(Decimal("0.01"))),
                    gross_amount=str(gross.quantize(Decimal("0.01"))),
                    currency=security.listing_currency,
                )
            )
    return suggestions


@router.post("/record-dividend", response_model=DividendRecordResult, status_code=201)
def record_dividend(
    payload: DividendRecordInput,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DividendRecordResult:
    """Record a dividend payment: 1-3 rows in a single transaction.

    Creates:
    1. Dividend/distribution/ROC trade (action, quantity=0, price_native=gross)
    2. If drip=True: buy trade (action=drip, fractional quantity)
    3. Transaction on the account for ledger visibility

    Then rebuilds ACB pools (handles DRIP buy impact).
    """
    account = db.get(Account, payload.account_id)
    if account is None or account.user_id != current_user.id:
        raise api_error(404, "Account not found", "not_found")
    _investment_account_or_422(account)

    security = db.get(Security, payload.security_id)
    if security is None or security.user_id != current_user.id:
        raise api_error(404, "Security not found", "not_found")

    # M181: this route CREATES a `transactions` row whose payee/amount columns
    # are mandatory plaintext, so an opted-in tenant cannot record one
    # server-side. Refused before the first `db.add` — no trade, no receipt and
    # no ACB rebuild happen.
    _refuse_when_encrypted(current_user, "Recording a dividend")

    if payload.drip and (payload.drip_price is None or payload.drip_price <= 0):
        raise api_error(422, "drip_price must be > 0 when drip=True", "invalid_drip_price")

    # 1. Dividend trade
    dividend_trade = Trade(
        user_id=current_user.id,
        account_id=payload.account_id,
        security_id=payload.security_id,
        action=payload.action,
        date=payload.date,
        quantity=Decimal("0"),
        price_native=payload.gross_amount,
        commission=Decimal("0"),
        fx_rate_used=payload.fx_rate_used,
        # M132: the suggested amount is per-share in the SECURITY's listing
        # currency (docs/05 §Dividend capture), so the gross and the DRIP price
        # below are stated in that currency — recorded here, never assumed to be
        # the account's.
        currency=security.listing_currency,
    )
    db.add(dividend_trade)
    db.flush()

    # 2. DRIP buy trade (optional)
    drip_trade_id: int | None = None
    if payload.drip:
        available = payload.gross_amount - payload.drip_commission
        drip_quantity = available / payload.drip_price
        if not payload.fractional:
            drip_quantity = drip_quantity.to_integral_value()  # floor to whole shares
        drip_trade = Trade(
            user_id=current_user.id,
            account_id=payload.account_id,
            security_id=payload.security_id,
            action=TradeAction.drip,
            date=payload.date,
            quantity=drip_quantity,
            price_native=payload.drip_price,
            commission=payload.drip_commission,
            fx_rate_used=payload.fx_rate_used,
        )
        db.add(drip_trade)
        db.flush()
        drip_trade_id = drip_trade.id

    # 3. Transaction for ledger visibility
    raw_desc = f"{payload.action.value} {security.symbol}"
    dedup_hash = hashlib.sha256(
        f"{payload.account_id}"
        f"|{payload.date.isoformat()}"
        f"|{payload.gross_amount.quantize(Decimal('0.01'))}"
        f"|{raw_desc}".encode()
    ).hexdigest()
    transaction = Transaction(
        user_id=current_user.id,
        account_id=payload.account_id,
        date=payload.date,
        raw_description=raw_desc,
        normalized_payee=normalized_payee(raw_desc),
        amount_native=payload.gross_amount,
        currency=account.currency,
        dedup_hash=dedup_hash,
    )
    db.add(transaction)
    db.flush()
    # M106 (docs/02 §transaction_events): a ledger row is still a row — the
    # dividend capture's own transaction gets the same `created` receipt an
    # import row does, so no create path is silent.
    record_event(
        db,
        tx_id=transaction.id,
        user_id=current_user.id,
        kind=TransactionEventKind.created,
        payload={"source": "trade"},
        actor=current_user.id,
    )

    # 4. Rebuild ACB pools (handles DRIP buy impact on cost base)
    rebuild_user_pools(db, current_user.id)
    db.commit()

    return DividendRecordResult(
        trade_id=dividend_trade.id,
        drip_trade_id=drip_trade_id,
        transaction_id=transaction.id,
    )


# --- Investment CSV import (docs/05) -------------------------------------------


def _find_existing_security(
    db: Session, user_id: int, clean: str, currency: str
) -> Security | None:
    """The row this file cell already owns, including an M143-repaired twin.

    An exact match first (the M142 rule: the raw cell is the key). Then the
    bare/qualified twin: a repair that moved a row onto the contested side
    of a collision stores the ``.TO`` spelling (``VGRO`` -> ``VGRO.TO``, the
    row keeps its id and its trades), and without this the NEXT import of
    the same file would find no match and insert a second row — splitting
    one holding across two securities (docs/02), the very failure the
    in-place repair exists to avoid.

    The twin only binds when the file's currency agrees with the row's
    (docs/07 §Candidate resolution): a USD cell for ``VFV`` must not latch
    onto a ``VFV.TO`` row that is priced in CAD. The unique key
    ``(user_id, symbol, exchange)`` is untouched by the lookup either way.
    """
    existing = db.exec(
        select(Security).where(Security.user_id == user_id, Security.symbol == clean)
    ).first()
    if existing is not None:
        return existing
    if clean.endswith(".TO"):
        return None
    twin = db.exec(
        select(Security).where(Security.user_id == user_id, Security.symbol == clean + ".TO")
    ).first()
    if twin is None:
        return None
    stated = (currency or "").strip().upper()
    if stated and twin.listing_currency.strip().upper() != stated:
        return None
    return twin


def _resolve_security(db: Session, user_id: int, symbol: str, name: str, currency: str) -> Security:
    """Find or auto-create a Security record for the given symbol.

    Exchange / listing currency / kind are resolved from market data when
    available; lookup failures (or privacy mode) degrade to the import-time
    heuristic so a bad ticker never blocks the import (docs/05).

    The symbol is normalized to ``strip().upper()`` BEFORE the lookup: every
    other write path (``POST /securities``, ``PATCH /securities/{id}``) stores
    an uppercased symbol and the unique key is ``(user_id, symbol, exchange)``,
    so a CSV row reading ``veqt`` must resolve to the existing ``VEQT`` row.
    Matching on the raw cell would instead insert a second row for the same
    holding and split its trades across two securities (docs/02).
    """
    clean = symbol.strip().upper()
    existing = _find_existing_security(db, user_id, clean, currency)
    if existing:
        return existing
    meta = None
    try:
        # M143: the file's own currency cell disambiguates a bare-vs-`.TO`
        # collision (docs/07 §Candidate resolution). Without it a CAD row for
        # `VGRO` would come back `ambiguous` → None → and the fallback below
        # would stamp TSX/CAD from a `.TO` probe that ignored what the file
        # said. With it, exactly one side fits and is selected outright.
        meta = market_data.lookup_symbol(clean, prefer_currency=currency)
    except Exception:  # never block import on lookup failure
        meta = None
    exchange = str((meta or {}).get("exchange") or "").strip()
    listing_currency = (meta or {}).get("listing_currency") or currency
    kind = (meta or {}).get("kind") or (
        SecurityKind.etf if re.search(r"\bETF\b", name, re.IGNORECASE) else SecurityKind.stock
    )
    resolved_name = (meta or {}).get("name") or name
    # yfinance `.info` (used by lookup_symbol) is frequently unavailable; the
    # `.history` seam is the reliable one. Probe the `.TO` form to detect
    # CDN/TSX listings and set exchange + CAD currency accordingly (docs/07).
    if not exchange:
        try:
            to_symbol = clean
            if not to_symbol.endswith(".TO"):
                to_symbol += ".TO"
            if market_data.fetch_latest_close(to_symbol) is not None:
                exchange = "TSX"
                listing_currency = "CAD"
        except Exception:  # privacy mode or network failure
            logging.getLogger(__name__).debug("exchange probe failed", exc_info=True)
    # Exchange is part of the unique key too — normalize it the same way so
    # `nms` and `NMS` cannot become two rows for one listing.
    exchange = exchange.strip().upper() or "UNKNOWN"
    security = Security(
        user_id=user_id,
        symbol=clean,
        name=resolved_name,
        kind=kind,
        listing_currency=listing_currency,
        exchange=exchange,
    )
    db.add(security)
    db.flush()
    return security


def _trade_dedup_hash(
    account_id: int, security_id: int, trade_date, action: str, quantity, price_native
) -> str:
    """SHA256 hex for trade import deduplication."""
    canonical = (
        f"{account_id}|{security_id}|{trade_date.isoformat()}|{action}|{quantity}|{price_native}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Investment / brokerage CSV import (docs/05, docs/41) ---------------------


#: Reason fragments that mean the ROW itself is unimportable rather than merely
#: unmappable: a guessed date or a guessed number is exactly the silent-wrong
#: failure docs/41 refuses, so staging answers 422 instead of skipping it.
_UNIMPORTABLE_ROW = re.compile(
    r"Unparseable date"
    r"|Invalid (?:quantity|unit_price|commission|net_cash_amount):"
    r"|row has \d+ fields, header has \d+"
    r"|data/header arity mismatch"
    r"|has no Header row"
)


def _blocked_code(row: UnmappedRowData) -> str | None:
    """The envelope code staging refuses on, or None when the row is skippable.

    A row that arrives with its OWN refusal class (M131's
    ``negative_contribution``) keeps it — the generic ``stuck``/``parse_error``
    pair is the fallback for reason-matched rows only, so the client can tell a
    malformed export from a semantically impossible one.
    """
    if row.code is not None:
        return row.code
    return "stuck" if _UNIMPORTABLE_ROW.search(row.reason) else None


def _stamp_blocked(unmapped: list[UnmappedRowData]) -> list[UnmappedRowData]:
    """Tag the rows that make staging refuse the file, for the response payload."""
    return [replace(u, code=_blocked_code(u)) for u in unmapped]


@dataclass(frozen=True)
class _ParsedUpload:
    rows: list[RawTrade]
    unmapped: list[UnmappedRowData]
    notices: list[str]
    profile: str | None
    institution: str | None

    @property
    def unimportable(self) -> list[UnmappedRowData]:
        return [u for u in self.unmapped if _blocked_code(u) is not None]


@dataclass(frozen=True)
class _PlannedRow:
    """One parsed row resolved against the account — the shared dry-run/stage plan."""

    rt: RawTrade
    security: Security | None
    canonical: str
    duplicate: bool


def _parse_upload(account: Account, raw: bytes, profile_slug: str | None) -> _ParsedUpload:
    """Profile-aware parse (docs/41). No writes, no security resolution.

    An explicit slug names a profile; without one the header is matched against
    every profile. A file that matches the **Wealthsimple** layout deliberately
    falls back to the legacy parser so existing Wealthsimple imports stay
    byte-identical — the `wealthsimple` profile is opt-in via `?profile=`.
    """
    slug = (profile_slug or "").strip() or None
    try:
        if slug is None:
            detected = detect_profile(raw)
            slug = None if detected is None else detected.slug
            if slug == "wealthsimple":
                slug = None  # legacy parser, byte-identical (docs/41)
        if slug is None:
            rows, unmapped = parse_investment_csv(raw, account_currency=account.currency)
            return _ParsedUpload(rows, _stamp_blocked(unmapped), [], None, None)
        outcome = parse_brokerage_csv(raw, slug, account_currency=account.currency)
    except ProfileError as exc:
        raise api_error(422, str(exc), exc.code) from None
    except InvestmentCsvProfileError as exc:
        raise api_error(422, str(exc), "parse_error") from None
    if outcome is None:  # pragma: no cover — parse_brokerage_csv never returns None for a slug
        rows, unmapped = parse_investment_csv(raw, account_currency=account.currency)
        return _ParsedUpload(rows, _stamp_blocked(unmapped), [], None, None)
    return _ParsedUpload(
        rows=outcome.rows,
        unmapped=_stamp_blocked(outcome.unmapped),
        notices=outcome.notices,
        profile=outcome.profile.slug,
        institution=outcome.profile.label,
    )


def _read_size_guard(raw: bytes) -> None:
    if len(raw) > 5_000_000:
        raise api_error(413, "File exceeds the 5 MB limit", "file_too_large")


def _row_cap_guard(parsed: _ParsedUpload) -> None:
    if len(parsed.rows) + len(parsed.unmapped) > 2000:
        raise api_error(413, "File contains more than 2000 rows", "too_many_rows")


def _cash_security(
    db: Session, user_id: int, listing_currency: str, *, create: bool
) -> Security | None:
    """The per-user `CASH` security used by symbol-less cash actions."""
    existing = db.exec(
        select(Security).where(Security.user_id == user_id, Security.symbol == "CASH")
    ).first()
    if existing is not None:
        return existing
    if not create:
        return None
    security = Security(
        user_id=user_id,
        symbol="CASH",
        name="Cash / No Security",
        kind=SecurityKind.stock,
        listing_currency=listing_currency,
        exchange="CASH",
    )
    db.add(security)
    db.flush()
    return security


def _lookup_security(
    db: Session, user_id: int, rt: RawTrade, account_currency: str, *, create: bool
) -> Security | None:
    """Find the row's security; create it only when staging (never in a dry run)."""
    if not rt.symbol:
        return _cash_security(db, user_id, account_currency, create=create)
    # Same normalization as _resolve_security: the preview verdict
    # ("existing" vs "create") must agree with what staging actually does,
    # otherwise a lower-case CSV cell previews as a brand-new security and
    # then silently reuses (or duplicates) a row.
    clean = rt.symbol.strip().upper()
    if create:
        # M132: the security's listing currency is the ROW's currency (the
        # file's own cell), falling back to the account's when the file states
        # none — the same authority `trades.currency` stores.
        return _resolve_security(
            db, user_id, clean, rt.security_name, rt.currency or account_currency
        )
    return db.exec(
        select(Security).where(Security.user_id == user_id, Security.symbol == clean)
    ).first()


def _plan_trade_rows(
    db: Session,
    current_user: User,
    account: Account,
    rows: list[RawTrade],
    *,
    create: bool,
) -> list[_PlannedRow]:
    """The ONE decision function behind the dry run and staging (docs/41).

    `create=False` is the zero-write dry run: an unknown symbol is reported,
    never inserted. A symbol with no `securities` row can have no prior `trades`
    row in this account, so such a row can never already be a duplicate.
    """
    planned: list[_PlannedRow] = []
    seen: set[str] = set()
    for rt in rows:
        security = _lookup_security(db, current_user.id, rt, account.currency, create=create)
        canonical = _trade_dedup_hash(
            account.id,
            security.id if security is not None else 0,
            rt.date,
            rt.action,
            rt.quantity,
            rt.price_native,
        )
        existing = (
            db.exec(
                select(Trade.id)
                .where(Trade.account_id == account.id)
                .where(Trade.dedup_hash == canonical)
                .limit(1)
            ).first()
            is not None
        )
        duplicate = existing or canonical in seen
        seen.add(canonical)
        planned.append(
            _PlannedRow(rt=rt, security=security, canonical=canonical, duplicate=duplicate)
        )
    return planned


def _require_investment_account(db: Session, current_user: User, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None or account.user_id != current_user.id:
        raise api_error(404, "Account not found", "not_found")
    _investment_account_or_422(account)
    return account


def _unmapped_payload(unmapped: list[UnmappedRowData]) -> list[UnmappedRow]:
    return [UnmappedRow(**asdict(u)) for u in unmapped]


@router.post("/import/preview", response_model=TradeDryRunResponse, status_code=201)
@human_paced_limit(IMPORT_UPLOAD_LIMIT)
async def import_trades_preview(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="Investment / brokerage CSV")],
    account_id: Annotated[int, Form()],
    profile: Annotated[str | None, Query(description="Brokerage profile slug")] = None,
) -> TradeDryRunResponse:
    """Dry run: what ``POST /trades/import`` WOULD do. Selects only (docs/41).

    No `securities`, `trades` or `import_batches` row is written, and a symbol
    with no existing security is reported `security_action="create"` rather
    than created. Rows that would make staging answer 422 are still listed here
    with `unmapped[].code == "stuck"` so the user can see exactly what is wrong.
    """
    account = await blocking(_require_investment_account)(db, current_user, account_id)
    raw = await file.read()
    _read_size_guard(raw)

    # M149: the profile parse, the row cap and the duplicate/security planning
    # (DB reads, one per row) are blocking work — off the loop, docs/01
    # § "Sessions and the event loop".
    def core() -> TradeDryRunResponse:
        parsed = _parse_upload(account, raw, profile)
        _row_cap_guard(parsed)

        planned = _plan_trade_rows(db, current_user, account, parsed.rows, create=False)
        dry_rows: list[TradeDryRunRow] = []
        new = duplicates = 0
        for index, row in enumerate(planned):
            verdict = "duplicate" if row.duplicate else "new"
            if row.duplicate:
                duplicates += 1
            else:
                new += 1
            dry_rows.append(
                TradeDryRunRow(
                    index=index,
                    date=row.rt.date,
                    symbol=row.rt.symbol or "CASH",
                    security_name=row.rt.security_name or "Cash",
                    action=row.rt.action,
                    quantity=row.rt.quantity,
                    price_native=row.rt.price_native,
                    commission=row.rt.commission,
                    currency=row.rt.currency,
                    verdict=verdict,
                    security_action="existing" if row.security is not None else "create",
                )
            )
        return TradeDryRunResponse(
            profile=parsed.profile,
            rows=dry_rows,
            unmapped=_unmapped_payload(parsed.unmapped),
            notices=parsed.notices,
            summary=TradeDryRunSummary(
                total=len(dry_rows),
                new=new,
                duplicates=duplicates,
                unmapped=len(parsed.unmapped),
            ),
        )

    return await blocking(core)()


@router.post("/import", response_model=TradeImportResponse, status_code=201)
@human_paced_limit(IMPORT_UPLOAD_LIMIT)
async def import_trades(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="Investment / brokerage CSV")],
    account_id: Annotated[int, Form()],
    profile: Annotated[str | None, Query(description="Brokerage profile slug")] = None,
) -> TradeImportResponse:
    """Stage trades from an investment / brokerage CSV (docs/05, docs/41).

    Parses through the matching profile, maps activity types to TradeAction,
    resolves/creates Securities, stages with dedup and returns a preview for
    review. Refuses the whole file rather than guessing at a row (docs/41).
    """
    account = await blocking(_require_investment_account)(db, current_user, account_id)
    # M181: the import stages cipher-less `trades` rows, so an opted-in tenant's
    # file is refused before the body is read (docs/33).
    _refuse_when_encrypted(current_user, "Importing a brokerage activity file")

    raw = await file.read()
    _read_size_guard(raw)

    # M149: parse, plan, stage and commit all block — worker thread, not the
    # loop (docs/01 § "Sessions and the event loop").
    def core() -> TradeImportResponse:
        parsed = _parse_upload(account, raw, profile)
        _row_cap_guard(parsed)

        blocked = parsed.unimportable
        if blocked:
            first = blocked[0]
            # A row with its own refusal class (M131 `negative_contribution`)
            # answers under that code; the reason-matched rows keep the generic
            # `parse_error` the wizard has always shown (docs/41).
            code = first.code if first.code not in (None, "stuck") else "parse_error"
            raise api_error(
                422,
                f"line {first.line}: {first.reason} — {len(blocked)} row(s) cannot be "
                "imported; fix the export rather than accept a guessed value",
                code,
            )

        batch = ImportBatch(
            user_id=current_user.id,
            account_id=account.id,
            institution=parsed.institution or "investment_csv",
            file_name=file.filename or "upload.csv",
            status=ImportBatchStatus.pending_review,
        )
        db.add(batch)
        db.flush()

        planned = _plan_trade_rows(db, current_user, account, parsed.rows, create=True)

        imported = duplicates_skipped = 0
        preview: list[TradePreviewRow] = []
        staged: list[tuple[Trade, RawTrade, bool]] = []
        for row in planned:
            rt = row.rt
            if row.security is None:  # create=True always resolves one
                raise api_error(500, "planned import row has no security", "internal_error")
            action_enum = TradeAction(rt.action)
            if row.duplicate:
                # Salt the hash so forced duplicates stay distinct (Trade has no
                # UNIQUE dedup constraint, but salt uniformly for one staging path).
                dedup_hash = f"{row.canonical}:{uuid.uuid4().hex}"
                is_dup = True
                duplicates_skipped += 1
            else:
                dedup_hash = row.canonical
                is_dup = False
                imported += 1
            quantity, commission = _apply(action_enum, rt.quantity, rt.commission)
            trade = Trade(
                user_id=current_user.id,
                account_id=account.id,
                security_id=row.security.id,
                action=action_enum,
                date=rt.date,
                quantity=quantity,
                price_native=rt.price_native,
                commission=commission,
                # M132 (docs/02 §trades): the row's OWN currency, captured here for
                # the first time — before this the per-row currency existed only in
                # the CSV and the cash leg face-summed USD into a CAD total.
                currency=rt.currency or account.currency,
                dedup_hash=dedup_hash,
                import_batch_id=batch.id,
                is_duplicate=is_dup,
                force_include=False,
            )
            db.add(trade)
            staged.append((trade, rt, row.duplicate))

        db.flush()  # assign ids to staged rows before building preview
        for trade, rt, duplicate in staged:
            preview.append(
                TradePreviewRow(
                    id=trade.id,
                    date=rt.date,
                    symbol=rt.symbol or "CASH",
                    security_name=rt.security_name or "Cash",
                    action=rt.action,
                    quantity=rt.quantity,
                    price_native=rt.price_native,
                    commission=rt.commission,
                    currency=rt.currency,
                    duplicate=duplicate,
                )
            )

        batch.imported = imported
        batch.duplicates_skipped = duplicates_skipped
        db.commit()
        db.refresh(batch)

        return TradeImportResponse(
            batch_id=batch.id,
            profile=parsed.profile,
            institution=parsed.institution,
            imported=imported,
            duplicates_skipped=duplicates_skipped,
            preview=preview,
            unmapped=_unmapped_payload(parsed.unmapped),
            notices=parsed.notices,
        )

    return await blocking(core)()


@router.post("/import/{batch_id}/commit")
def commit_trade_batch(
    batch_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
    body: CommitBatchBody | None = None,
) -> ImportBatchPublic:
    """Commit a pending trade import batch: keep forced duplicates, drop the
    rest, then rebuild ACB pools (docs/03, docs/05). M147 then queues
    composition enrichment for any fund the batch holds with zero rows."""
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.user_id != current_user.id:
        raise api_error(404, "Import batch not found", "not_found")
    if batch.status != ImportBatchStatus.pending_review:
        raise api_error(
            409,
            f"Batch is {batch.status}, only pending_review batches can be committed",
            "batch_not_pending_review",
        )

    # M181: the enrol-after-staging hole — a batch staged while the tenant was
    # ordinary must not be released into the ledger afterwards. `discard` stays
    # open (removal only).
    _refuse_when_encrypted(current_user, "Committing a staged trade import")
    rows = db.exec(select(Trade).where(Trade.import_batch_id == batch.id)).all()
    include = set(body.include_ids) if body else set()
    for r in rows:
        if r.is_duplicate:
            r.force_include = r.id in include
    delete_ids = [r.id for r in rows if r.is_duplicate and not r.force_include]
    if delete_ids:
        db.execute(sa_delete(Trade).where(Trade.id.in_(delete_ids)))
    dup_total = sum(1 for r in rows if r.is_duplicate)
    forced = sum(1 for r in rows if r.is_duplicate and r.id in include)
    total = len(rows)
    batch.duplicates_skipped = dup_total - forced
    batch.imported = total - (dup_total - forced)
    batch.status = ImportBatchStatus.committed
    rebuild_user_pools(db, current_user.id)
    db.commit()
    db.refresh(batch)
    # M147: any fund this batch introduced (or already held) with ZERO
    # composition rows gets its enrichment scheduled in the background —
    # same provider chain as the manual import endpoint (docs/05). The
    # task degrades silently, so a commit never fails on market data.
    fund_enrichment.enqueue(
        db,
        background_tasks,
        current_user.id,
        [
            row[0]
            for row in db.execute(
                select(Trade.security_id)
                .where(Trade.import_batch_id == batch.id, Trade.security_id.is_not(None))
                .distinct()
            ).all()
        ],
    )
    return ImportBatchPublic.model_validate(batch)


@router.post("/import/{batch_id}/discard")
def discard_trade_batch(
    batch_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Discard a pending trade import batch: delete staged trades."""
    from app.models.import_batches import ImportBatch, ImportBatchStatus

    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.user_id != current_user.id:
        raise api_error(404, "Import batch not found", "not_found")
    if batch.status != ImportBatchStatus.pending_review:
        raise api_error(
            409,
            f"Batch is {batch.status}, only pending_review batches can be discarded",
            "batch_not_pending_review",
        )

    # Delete all staged trades for this batch
    staged = db.exec(select(Trade).where(Trade.import_batch_id == batch_id)).all()
    for trade in staged:
        db.delete(trade)

    batch.status = ImportBatchStatus.discarded
    rebuild_user_pools(db, current_user.id)
    db.commit()
    return {"status": "discarded", "batch_id": batch_id}


# --- Single-trade CRUD (path params — MUST come after named routes) ------------


@router.get("/{trade_id}", response_model=TradeRead)
def get_trade(
    trade_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Trade:
    return owned_or_404(Trade, trade_id, current_user, db)


@router.patch("/{trade_id}", response_model=TradeRead)
def update_trade(
    trade_id: int,
    payload: TradeUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Trade:
    trade = owned_or_404(Trade, trade_id, current_user, db)
    data = payload.model_dump(exclude_unset=True)
    account_id = data.get("account_id", trade.account_id)
    security_id = data.get("security_id", trade.security_id)
    account = _related_or_404(db, current_user, account_id, security_id)
    _investment_account_or_422(account)
    # M181: an edit writes the same cipher-less `trades` row a create would
    # (docs/33). `DELETE` below stays open — removal only.
    _refuse_when_encrypted(current_user, "Editing a trade")
    action = data.get("action", trade.action)
    _negative_contribution_or_422(action, data.get("price_native", trade.price_native))
    if "quantity" in data or "action" in data:
        data["quantity"] = _normalize(action, data.get("quantity", trade.quantity))
    if ("commission" in data or "action" in data) and action not in COMMISSION_ACTIONS:
        data["commission"] = Decimal("0")
    for field, value in data.items():
        setattr(trade, field, value)
    db.add(trade)
    rebuild_user_pools(db, current_user.id)
    db.commit()
    db.refresh(trade)
    return trade


@router.delete("/{trade_id}", status_code=204)
def delete_trade(
    trade_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    trade = owned_or_404(Trade, trade_id, current_user, db)
    db.delete(trade)
    rebuild_user_pools(db, current_user.id)
    db.commit()
