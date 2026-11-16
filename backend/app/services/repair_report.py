"""Repair-needed report: which OWNED rows still point at the wrong listing (M155).

M141/M142/M143 closed the two ways a row could *become* wrong at add time — a
typed name now resolves to its canonical ticker, and a bare ticker that lives
in two markets now answers a pick-one chooser instead of whichever side
probed first. What they cannot do is reach BACKWARD: the rows stored before
those fixes (prod 294 = *Virtus Silvant* under a Vanguard holding, 293 =
`APOTEX` stored as a symbol, 296 = `VFV, USD`) were each found by a human who
happened to look, then corrected one at a time with `scripts/repair_symbol.py`.
The next one is still out there, quietly holding the wrong instrument.

This module is the standing answer: a re-runnable SCAN over every security a
committed trade or a watchlist row has ever touched, asking each of them two
questions and reporting the ones whose answers disagree with the ledger.

**Family 1 — bare collisions (the M143 class).** `resolve_symbol` answers
`ambiguous`, so TWO listings are live candidates for the row's symbol. The
report then asks which one the LEDGER means, in the order the docs already
trust: the row's own committed trade currencies first (a CAD buy against a USD
listing is the whole of the 294 bug), and — only when the currency reading is
inconclusive — the M144 price evidence, `deep_backfill.score_series` scoring
each candidate's closes against the recorded trade prices. Two rows that are
the SAME listing under two spellings (`APTX` + `APTX.TO`, the 293/298 leftover
`security_dedup` can never merge) are found here too, with no network at all.

**Family 2 — live-symbol heuristics (the M141/M142 class).** One
`resolve_symbol` per row, then the stored identity is compared with what the
listing says NOW: a symbol that is really a name (`match: search`), a listing
currency that disagrees with the live venue, an exchange naming a different
market, a name belonging to another instrument, a `dead_symbol` verdict the
market has since contradicted (or confirmed).

**What it deliberately does NOT do:** write anything. Every actionable finding
carries the exact `scripts/repair_symbol.py` invocation that would fix it and
the operator applies it — a repair never picks a listing for a human
(docs/07 §Symbol repair), and this report is not going to start.

Two house rules shape the implementation:

- **It cannot be an endpoint.** AGENTS §2: *"Ingestion and report endpoints
  never fetch — cache reads only."* Every check here is an external probe, so
  the surface is an operator CLI (`scripts/repair_needed_report.py`) run on a
  schedule, the way the M148 autoheal watchdog runs (docs/07 §Repair-needed
  report).
- **No session is ever held across a provider wait** (docs/01 §Sessions and
  the event loop — the M152/M153 rule). `scan()` runs in phases: ONE session
  reads every row into plain frozen values and closes; the network phase
  probes with no session in existence; a second session scores the price
  tie-breaks and closes. Pinned by
  `tests/test_repair_report.py::test_no_session_survives_a_provider_wait`.

Noise control, because a report nobody trusts is worse than no report:

- **A canary answers first.** `probe_quote(canary)` must come back `ok`,
  otherwise market data is unreachable and the run says so ONCE instead of
  printing a wall of false `no_listing` findings — a network blip is not a
  delisting, the same distinction M141 draws between `unavailable` and
  `not_found`.
- **Privacy mode still means zero packets** (docs/11): the row's owner binds
  the ContextVar before its own probes, a refused row is reported as
  `fetch_refused`, and when every in-scope owner is in privacy mode the
  canary is never sent either.
- **The `CASH` placeholder is skipped** (`deep_backfill.never_held_shares`):
  `CASH` really resolves to Pathward Financial (docs/07 §Backfill depth), and
  reporting cash would be a false positive on a row that is not an instrument.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.models.import_batches import ImportBatch
from app.models.securities import QuoteOutcome, Security
from app.models.trades import Trade
from app.models.users import User
from app.services import market_data
from app.services.deep_backfill import (
    TradeEvidence,
    _FxLookup,  # the same fx seam the M144 scoring uses
    held_security_ids,
    never_held_shares,
    score_series,
    trade_evidence,
)
from app.services.market_data import CandidateSeries, SymbolResolution
from app.services.trade_filters import committed_trade_conditions

log = logging.getLogger(__name__)

#: A probe symbol that must answer before ANY row is judged. Deliberately the
#: most liquid listing there is: the question is "can we reach Yahoo at all",
#: never "does this row exist". Override with `--canary`.
CANARY_SYMBOL = "AAPL"


class Severity(StrEnum):
    """What the operator should do with a finding (docs/07 §Repair-needed report)."""

    repair = "repair"  # the row contradicts the market: run the given command
    review = "review"  # a human has to look — the ledger cannot decide alone
    info = "info"  # recorded for completeness, no action implied


class Check(StrEnum):
    """Machine-readable check ids — stable keys, asserted by tests."""

    # Family 1: bare collisions.
    collision_wrong_side = "bare_collision_wrong_side"
    collision_agrees = "bare_collision_agrees"
    collision_undecided = "bare_collision_undecided"
    bare_twin = "bare_twin_rows"
    # Family 2: live-symbol heuristics.
    name_stored_as_symbol = "name_stored_as_symbol"
    currency_mismatch = "currency_mismatch"
    exchange_mismatch = "exchange_mismatch"
    name_mismatch = "name_mismatch"
    name_is_symbol = "name_is_symbol"
    identity_missing = "identity_missing"
    unresolved_still = "unresolved_still_no_listing"
    no_listing = "no_listing"
    dead_symbol_stale = "dead_symbol_stale"
    dead_symbol_confirmed = "dead_symbol_confirmed"
    fetch_refused = "fetch_refused"


@dataclass(frozen=True)
class Finding:
    """One row, one disagreement, one suggested next step."""

    security_id: int
    user_id: int
    symbol: str
    check: Check
    severity: Severity
    detail: str
    #: The exact operator command (docs/07 runbook), or `None` when there is
    #: nothing to run — a finding with no command is never a `repair`.
    suggestion: str | None = None

    def line(self) -> str:
        head = f"security {self.security_id} (user {self.user_id}) {self.symbol}: {self.detail}"
        return f"{head}\n      -> {self.suggestion}" if self.suggestion else head

    def to_dict(self) -> dict[str, object]:
        return {
            "security_id": self.security_id,
            "user_id": self.user_id,
            "symbol": self.symbol,
            "check": str(self.check),
            "severity": str(self.severity),
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class OwnedRow:
    """One owned `securities` row, snapshotted to plain values.

    Frozen on purpose: it outlives the session that read it (phase 1 closes
    before the first provider call), so an ORM instance would be detached —
    the exact trap M152 documented for `_candidates`.
    """

    id: int
    user_id: int
    symbol: str
    name: str
    kind: str
    listing_currency: str
    exchange: str
    dead_symbol: bool
    quote_miss_count: int
    dead_symbol_reason: str | None
    privacy_mode: bool
    trades: int
    watched: bool
    #: Committed trades that moved shares, in the currency each row states
    #: (`deep_backfill.trade_evidence`) — the ledger's own say in a collision.
    evidence: tuple[TradeEvidence, ...]
    #: Every committed trade moved zero shares: the per-user `CASH`
    #: placeholder, not a listed instrument (docs/07 §Backfill depth).
    cash_placeholder: bool = False

    @property
    def base_symbol(self) -> str:
        """`VGRO.TO` -> `VGRO`: the bare/qualified twin key (docs/05)."""
        symbol = self.symbol.strip().upper()
        return symbol[: -len(".TO")] if symbol.endswith(".TO") else symbol

    @property
    def qualified(self) -> bool:
        return self.symbol.strip().upper().endswith(".TO")

    @property
    def evidence_currencies(self) -> set[str]:
        return {ev.currency.upper() for ev in self.evidence}


@dataclass
class ScanReport:
    """Everything one run did — asserted by tests, printed by the CLI."""

    canary: str = CANARY_SYMBOL
    #: `True` when market data answered, `False` when the run was suppressed,
    #: `None` when no probe was needed (every owner in privacy mode).
    canary_ok: bool | None = None
    probe_error: str | None = None
    rows_scanned: int = 0
    rows_skipped: Counter[str] = field(default_factory=Counter)
    findings: list[Finding] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def counts(self) -> Counter[Severity]:
        return Counter(finding.severity for finding in self.findings)

    @property
    def repairs(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.repair]

    @property
    def reviews(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.review]

    @property
    def exit_code(self) -> int:
        """0 = nothing to do, 1 = a repair is warranted, 2 = scan could not run."""
        if self.probe_error:
            return 2
        return 1 if self.repairs else 0

    def summary(self) -> str:
        counts = self.counts
        parts = [
            f"{self.rows_scanned} owned row(s)",
            f"{counts[Severity.repair]} repair",
            f"{counts[Severity.review]} review",
            f"{counts[Severity.info]} info",
        ]
        if self.rows_skipped:
            parts.append(
                "skipped "
                + ", ".join(f"{count}x {why}" for why, count in sorted(self.rows_skipped.items()))
            )
        if self.probe_error:
            parts.append(f"SCAN INCOMPLETE: {self.probe_error}")
        return "; ".join(parts)

    def render(self) -> str:
        """The human report: repairs first, then reviews, then info."""
        lines: list[str] = []
        if self.probe_error:
            lines.append(f"scan incomplete: {self.probe_error}")
        for severity, heading in (
            (Severity.repair, "REPAIR NEEDED"),
            (Severity.review, "REVIEW"),
            (Severity.info, "INFO"),
        ):
            bucket = [f for f in self.findings if f.severity == severity]
            if not bucket:
                continue
            lines.append(f"{heading} ({len(bucket)})")
            lines.extend(f"  [{finding.check}] {finding.line()}" for finding in bucket)
        lines.append(self.summary())
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        counts = self.counts
        return {
            "canary": self.canary,
            "canary_ok": self.canary_ok,
            "probe_error": self.probe_error,
            "rows_scanned": self.rows_scanned,
            "rows_skipped": dict(self.rows_skipped),
            "counts": {
                str(Severity.repair): counts[Severity.repair],
                str(Severity.review): counts[Severity.review],
                str(Severity.info): counts[Severity.info],
            },
            "findings": [finding.to_dict() for finding in self.findings],
            "elapsed_seconds": round(self.elapsed, 1),
        }


# --------------------------------------------------------------------------- #
# phase 1 — read every row into plain values, then close the session
# --------------------------------------------------------------------------- #


def load_owned_rows(session: Session, user_id: int | None = None) -> list[OwnedRow]:
    """Every security a committed trade or a watchlist row has touched.

    The `deep_backfill.held_security_ids` scope — the same "owned" set the
    deep backfill sweeps — because that is the set a wrong listing can
    mis-price. Evidence, attachment counts and the owner's privacy flag are
    all read HERE; nothing after this point needs the database until phase 3.
    """
    wanted = held_security_ids(session, user_id)
    if not wanted:
        return []
    securities = session.exec(
        select(Security)
        .where(Security.id.in_(sorted(wanted)))
        .order_by(Security.user_id, Security.id)
    ).all()
    privacy = {
        row_id: bool(is_private)
        for row_id, is_private in session.exec(select(User.id, User.privacy_mode)).all()
    }
    rows: list[OwnedRow] = []
    for security in securities:
        trades, watched = _attachment_counts(session, security)
        evidence = tuple(trade_evidence(session, security)) if trades else ()
        rows.append(
            OwnedRow(
                id=int(security.id),
                user_id=security.user_id,
                symbol=security.symbol,
                name=security.name,
                kind=security.kind.value,
                listing_currency=security.listing_currency,
                exchange=security.exchange,
                dead_symbol=bool(security.dead_symbol),
                quote_miss_count=int(security.quote_miss_count),
                dead_symbol_reason=security.dead_symbol_reason,
                privacy_mode=privacy.get(security.user_id, False),
                trades=trades,
                watched=watched,
                evidence=evidence,
                cash_placeholder=never_held_shares(session, security),
            )
        )
    return rows


def _attachment_counts(session: Session, security: Security) -> tuple[int, bool]:
    """`(committed trade rows, watchlisted)` — index reads, no network."""
    from app.models.watchlist import WatchlistItem

    trade_rows = session.exec(
        select(Trade.id)
        .outerjoin(ImportBatch, Trade.import_batch_id == ImportBatch.id)
        .where(*committed_trade_conditions(security.user_id), Trade.security_id == security.id)
    ).all()
    watched = (
        session.exec(
            select(WatchlistItem.id).where(WatchlistItem.security_id == security.id)
        ).first()
        is not None
    )
    return len(trade_rows), watched


# --------------------------------------------------------------------------- #
# phase 2 — the network: one resolution per row, with no session in existence
# --------------------------------------------------------------------------- #


def _probe_rows(
    rows: list[OwnedRow], *, sleep: float
) -> tuple[dict[int, SymbolResolution], list[Finding]]:
    """Resolve every in-scope row. Returns `(resolutions, refusal findings)`.

    A privacy-mode owner is refused WITHOUT a packet (its flag binds the
    ContextVar before the row's own probe), and one row's exception never
    aborts the sweep — the report is a list of verdicts, not a transaction.
    """
    resolutions: dict[int, SymbolResolution] = {}
    refused: list[Finding] = []
    current_owner: int | None = None
    try:
        for row in rows:
            if row.user_id != current_owner:
                market_data.bind_privacy_lock(row.privacy_mode)
                current_owner = row.user_id
            if row.privacy_mode:
                refused.append(
                    Finding(
                        security_id=row.id,
                        user_id=row.user_id,
                        symbol=row.symbol,
                        check=Check.fetch_refused,
                        severity=Severity.info,
                        detail="privacy mode is on for this owner — zero packets were sent",
                    )
                )
                continue
            try:
                resolutions[row.id] = market_data.resolve_symbol(row.symbol, disambiguate=True)
            except Exception as exc:  # the seam raises its own envelope
                refused.append(
                    Finding(
                        security_id=row.id,
                        user_id=row.user_id,
                        symbol=row.symbol,
                        check=Check.fetch_refused,
                        severity=Severity.info,
                        detail=f"lookup refused: {exc}",
                    )
                )
            if sleep:
                time.sleep(sleep)
    finally:
        # Never leave the lock bound to a tenant's flag in this context — the
        # same discipline `deep_backfill.deep_backfill_all` ends with.
        market_data.bind_privacy_lock(False)
    return resolutions, refused


def _needs_price_tiebreak(row: OwnedRow, resolution: SymbolResolution) -> bool:
    """The M144 price evidence, used ONLY when the currency reading cannot say.

    A collision whose ledger currency picks a side is already decided (the 294
    case); fetching two full series to re-confirm it would spend Yahoo calls on
    the one answer we have. The tie-break earns its keep on same-currency
    collisions (`APTX.TO` vs `APTX.NE`, both CAD), where the recorded buy
    prices are the only thing that can tell the two listings apart.
    """
    if resolution["match"] != "ambiguous" or not row.evidence:
        return False
    side, _conclusive = _currency_side(row, resolution["candidates"])
    return side is None


def _collect_series(
    row: OwnedRow, candidates: list[dict[str, object]]
) -> dict[str, CandidateSeries]:
    """Every candidate's closes over the row's own evidence window.

    One `fetch_history_candidates(row.symbol)` answers a bare/`.TO` collision
    outright (it probes both sides); a SEARCH collision (a stored name) is
    fetched candidate by candidate, because the stored spelling resolves to
    nothing. Fetches run with NO session open — phase 3 scores them.
    """
    evidence = list(row.evidence)
    if not evidence:
        return {}
    today = date.today()
    window_start = min(ev.date for ev in evidence)
    found: dict[str, CandidateSeries] = {}
    for series in market_data.fetch_history_candidates(row.symbol, window_start, today):
        found[series.symbol.upper()] = series
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol or symbol in found:
            continue
        for series in market_data.fetch_history_candidates(symbol, window_start, today):
            if series.symbol.upper() == symbol:
                found[symbol] = series
    return found


# --------------------------------------------------------------------------- #
# the verdicts — pure functions, so each one is a unit test
# --------------------------------------------------------------------------- #


def _currency_side(
    row: OwnedRow, candidates: list[dict[str, object]]
) -> tuple[dict[str, object] | None, bool]:
    """`(the candidate the row's own trade currencies name, conclusive?)`.

    One stated currency across every committed trade and exactly ONE candidate
    carrying it = an answer. Anything else (mixed currencies, a currency no
    candidate quotes) is NOT an answer — guessing here is how 294 happened.
    """
    want = row.evidence_currencies
    if len(want) != 1:
        return None, False
    code = next(iter(want))
    matches = [
        candidate
        for candidate in candidates
        if str(candidate.get("listing_currency") or "").upper() == code
    ]
    return (matches[0], True) if len(matches) == 1 else (None, False)


def _price_side(
    row: OwnedRow,
    candidates: list[dict[str, object]],
    series: dict[str, CandidateSeries],
    fx: _FxLookup,
) -> dict[str, object] | None:
    """The candidate whose closes explain a MAJORITY of the trades (M144)."""
    best: dict[str, object] | None = None
    best_agreeing = 0
    for candidate in candidates:
        candidate_series = series.get(str(candidate.get("symbol") or "").upper())
        if candidate_series is None or not candidate_series.closes:
            continue
        comparable, agreeing = score_series(candidate_series, list(row.evidence), fx)
        if comparable and agreeing * 2 >= comparable and agreeing > best_agreeing:
            best, best_agreeing = candidate, agreeing
    return best


def _names_agree(stored: str, live: str) -> bool:
    """Tolerant name comparison — a Yahoo `shortName` is not a stable string.

    Equal after normalization, or one CONTAINED in the other (stored
    `Virtus Silvant Growth Opportuni` vs live `Virtus Silvant Growth
    Opportunities ETF` is the same instrument), else no agreement.
    """

    def norm(value: str) -> str:
        # `&` folds to nothing so "S&P" and Yahoo's "SP" are one string; every
        # other separator becomes a boundary, so punctuation drift never reads
        # as a different instrument.
        without_ampersand = value.casefold().replace("&", "")
        cleaned = "".join(ch if ch.isalnum() else " " for ch in without_ampersand)
        return " ".join(cleaned.split())

    left, right = norm(stored), norm(live)
    if not left or not right:
        return not left and not right
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 6 and shorter in longer:
        return True
    # Abbreviation drift: Yahoo's "Vanguard Information Technology ETF" vs a
    # row stored from the truncated "…Information Tech ETF". Every token of
    # the SHORTER side must prefix-match a token of the longer one — one
    # wholly-unshared token is a different instrument, and that is the case
    # this check exists for.
    short_tokens, long_tokens = shorter.split(), longer.split()
    return bool(short_tokens) and all(
        any(long_token.startswith(token) for long_token in long_tokens) for token in short_tokens
    )


def _command(security_id: int, pick: str | None = None) -> str:
    """The exact operator invocation (docs/07 §Symbol repair runbook)."""
    pick_part = f" --symbol {pick}" if pick else ""
    return (
        f"ssh example.internal 'docker exec fin_tracker-api-1 python "
        f"scripts/repair_symbol.py --security-id {security_id}{pick_part} --apply'"
    )


def collision_findings(
    row: OwnedRow,
    resolution: SymbolResolution,
    price_side: dict[str, object] | None = None,
) -> list[Finding]:
    """Family 1 — TWO listings answer for this row's symbol (M143)."""
    candidates = resolution["candidates"]
    stored = row.symbol.strip().upper()
    is_quote_collision = any(
        str(candidate.get("symbol") or "").upper() == stored for candidate in candidates
    )
    named = ", ".join(
        f"{candidate.get('symbol')} ({candidate.get('listing_currency')}, {candidate.get('name')})"
        for candidate in candidates
    )

    side, conclusive = _currency_side(row, candidates)
    if side is None and price_side is not None:
        side, conclusive = price_side, False
    stated = ", ".join(sorted(row.evidence_currencies)) or "no stated trade currency"

    if side is None:
        return [
            Finding(
                security_id=row.id,
                user_id=row.user_id,
                symbol=row.symbol,
                check=Check.collision_undecided,
                severity=Severity.review,
                detail=(
                    "several listings match and the ledger cannot say which one "
                    f"({stated}): {named}"
                ),
                suggestion=(
                    f"ssh example.internal 'docker exec fin_tracker-api-1 python "
                    f"scripts/repair_symbol.py --security-id {row.id} --symbol <PICK> --apply'"
                ),
            )
        ]

    side_symbol = str(side.get("symbol") or "")
    if is_quote_collision and side_symbol.upper() == stored:
        return [
            Finding(
                security_id=row.id,
                user_id=row.user_id,
                symbol=row.symbol,
                check=Check.collision_agrees,
                severity=Severity.info,
                detail=(
                    "two listings answer but the ledger agrees with the stored side "
                    f"({side_symbol}, {side.get('listing_currency')}): {named}"
                ),
            )
        ]

    return [
        Finding(
            security_id=row.id,
            user_id=row.user_id,
            symbol=row.symbol,
            # A stored NAME matching several listings is not a market collision
            # at all: the row's own spelling is not a ticker (the 299/293 shape).
            check=Check.collision_wrong_side if is_quote_collision else Check.name_stored_as_symbol,
            severity=Severity.repair,
            detail=(
                f"the ledger's {'trade currency' if conclusive else 'recorded trade prices'} "
                f"says {side_symbol} ({side.get('listing_currency')}, {side.get('name')}), "
                f"the row stores {row.symbol} ({row.listing_currency}, {row.name}): {named}"
            ),
            suggestion=_command(row.id, side_symbol),
        )
    ]


def live_findings(row: OwnedRow, resolution: SymbolResolution) -> list[Finding]:
    """Family 2 — ONE listing answered: does the stored identity still match it?"""
    match = resolution["match"]
    result = resolution["result"]
    findings: list[Finding] = []

    def add(check: Check, severity: Severity, detail: str, suggestion: str | None = None) -> None:
        findings.append(
            Finding(
                security_id=row.id,
                user_id=row.user_id,
                symbol=row.symbol,
                check=check,
                severity=severity,
                detail=detail,
                suggestion=suggestion,
            )
        )

    if result is None:  # match == "none"
        if not row.listing_currency.strip():
            add(
                Check.unresolved_still,
                Severity.info,
                "unresolved row (no listing currency) and the market still names no listing",
            )
        elif row.dead_symbol:
            add(
                Check.dead_symbol_confirmed,
                Severity.info,
                f"dead_symbol confirmed by a live probe "
                f"({row.dead_symbol_reason or 'no reason recorded'})",
            )
        else:
            add(
                Check.no_listing,
                Severity.review,
                "the stored symbol answers no listing at all",
                f"POST /api/v1/securities/{row.id}/refresh  # books the M141 verdict",
            )
        return findings

    live_symbol = str(result.get("symbol") or "")
    live_name = str(result.get("name") or "")
    live_currency = str(result.get("listing_currency") or "")
    live_exchange = str(result.get("exchange") or "")
    stored_symbol = row.symbol.strip().upper()
    stored_currency = row.listing_currency.strip()

    if not stored_currency:
        add(
            Check.identity_missing,
            Severity.repair,
            f"unresolved row now resolves: {live_symbol} — {live_name} "
            f"({live_currency}, {live_exchange})",
            _command(row.id),
        )
        return findings

    if match == "search":
        add(
            Check.name_stored_as_symbol,
            Severity.repair,
            f"the stored symbol is a NAME — the listing is {live_symbol} "
            f"({live_name}, {live_currency})",
            _command(row.id),
        )
        return findings

    # `direct` / `suffix`: one listing answered, so compare field by field.
    if live_currency and live_currency.upper() != stored_currency.upper():
        corroborated = bool(row.evidence) and row.evidence_currencies == {live_currency.upper()}
        add(
            Check.currency_mismatch,
            Severity.repair if corroborated else Severity.review,
            f"row stores {stored_currency}, the listing quotes {live_currency}"
            + (" — and every recorded trade agrees with the listing" if corroborated else ""),
            _command(row.id) if corroborated else None,
        )
    if (
        live_exchange
        and row.exchange.strip()
        and live_exchange.upper() != row.exchange.strip().upper()
        and live_symbol.upper() == stored_symbol
    ):
        add(
            Check.exchange_mismatch,
            Severity.repair,
            f"row stores exchange {row.exchange}, the listing trades on {live_exchange} "
            "(same symbol, so a repair drops no closes)",
            _command(row.id),
        )
    if live_name:
        if row.name.strip().upper() == stored_symbol:
            if not _names_agree(row.name, live_name):
                add(
                    Check.name_is_symbol,
                    Severity.info,
                    f"the row's name is its own ticker — the listing calls itself "
                    f"'{live_name}' (cosmetic; --apply fills it in)",
                    _command(row.id),
                )
        elif not _names_agree(row.name, live_name):
            add(
                Check.name_mismatch,
                Severity.review,
                f"row is named '{row.name}', the listing is '{live_name}' "
                "(a rename may have been deliberate — a repair makes market data win)",
                _command(row.id),
            )
    if row.dead_symbol:
        add(
            Check.dead_symbol_stale,
            Severity.review,
            f"dead_symbol (misses={row.quote_miss_count}) but {live_symbol} answers again",
            f"POST /api/v1/securities/{row.id}/refresh  # probe_dead clears a stale verdict",
        )
    return findings


def twin_findings(rows: list[OwnedRow]) -> list[Finding]:
    """Family 1, DB-only: `X` and `X.TO` both owned by one user (293/298).

    `security_dedup` merges only rows whose NORMALIZED key collides, so a
    bare/qualified pair keeps one holding split across two rows — the "Known
    leftovers" M142 documented. Reported, never merged: choosing which row
    survives is the operator's call (M117 repoints trades and drops the other).
    """
    groups: dict[tuple[int, str], list[OwnedRow]] = {}
    for row in rows:
        groups.setdefault((row.user_id, row.base_symbol), []).append(row)
    findings: list[Finding] = []
    for (user_id, _base), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        if not any(m.qualified for m in members) or not any(not m.qualified for m in members):
            continue
        anchor = next((m for m in members if m.trades), members[0])
        ids = ", ".join(f"{m.id} ({m.symbol})" for m in sorted(members, key=lambda m: m.id))
        findings.append(
            Finding(
                security_id=anchor.id,
                user_id=user_id,
                symbol=anchor.symbol,
                check=Check.bare_twin,
                severity=Severity.review,
                detail=f"one listing is owned as TWO rows: {ids} — trades split across them",
                suggestion=(
                    "fold them by hand (docs/05): the dedup pass only merges rows whose "
                    "normalized keys collide, so this pair never will"
                ),
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# the scan
# --------------------------------------------------------------------------- #


def scan(
    engine: Engine,
    *,
    user_id: int | None = None,
    canary: str = CANARY_SYMBOL,
    sleep: float = 0.0,
) -> ScanReport:
    """Run every check over every owned row. READ-ONLY: nothing is written.

    Three phases, and the boundaries are the point (docs/01 §Sessions and the
    event loop): the session that reads the rows CLOSES before the first
    provider call, and the session that scores the price tie-breaks opens
    only after the last one.
    """
    started = time.monotonic()
    report = ScanReport(canary=canary)

    # phase 1 — read (one session, closed on the way out)
    with Session(engine, expire_on_commit=False) as session:
        rows = load_owned_rows(session, user_id)
    report.rows_skipped = Counter("cash_placeholder" for row in rows if row.cash_placeholder)
    probeable = [row for row in rows if not row.cash_placeholder]

    # phase 2 — the network, with no session in existence
    refused: list[Finding] = []
    resolutions: dict[int, SymbolResolution] = {}
    if any(not row.privacy_mode for row in probeable):
        try:
            outcome, reason = market_data.probe_quote(canary)
        except Exception as exc:  # the seam raises its own envelope
            outcome, reason = QuoteOutcome.unavailable, str(exc)
        if outcome != QuoteOutcome.ok:
            report.canary_ok = False
            report.probe_error = (
                f"canary '{canary}' did not answer ({outcome.value}): "
                f"{reason or 'no reason'} — market data looks unreachable, so no row was judged"
            )
            report.findings = [
                Finding(
                    security_id=row.id,
                    user_id=row.user_id,
                    symbol=row.symbol,
                    check=Check.fetch_refused,
                    severity=Severity.info,
                    detail="privacy mode is on for this owner — zero packets were sent",
                )
                for row in probeable
                if row.privacy_mode
            ]
            report.rows_scanned = len(probeable)
            report.elapsed = time.monotonic() - started
            return report
        report.canary_ok = True
    resolutions, refused = _probe_rows(probeable, sleep=sleep)

    # phase 2b — price evidence for the collisions the currency could not decide
    tiebreaks: dict[int, dict[str, CandidateSeries]] = {}
    for row in probeable:
        resolution = resolutions.get(row.id)
        if resolution is None or not _needs_price_tiebreak(row, resolution):
            continue
        try:
            tiebreaks[row.id] = _collect_series(row, resolution["candidates"])
        except Exception as exc:  # one row must not abort the sweep
            refused.append(
                Finding(
                    security_id=row.id,
                    user_id=row.user_id,
                    symbol=row.symbol,
                    check=Check.fetch_refused,
                    severity=Severity.info,
                    detail=f"price evidence refused: {exc}",
                )
            )

    # phase 3 — score the tie-breaks (a session, after the last fetch)
    price_sides: dict[int, dict[str, object] | None] = {}
    if tiebreaks:
        with Session(engine, expire_on_commit=False) as session:
            fx = _FxLookup(session)
            for row in probeable:
                series = tiebreaks.get(row.id)
                resolution = resolutions.get(row.id)
                if not series or resolution is None:
                    continue
                price_sides[row.id] = _price_side(row, resolution["candidates"], series, fx)

    # phase 4 — pure evaluation
    findings: list[Finding] = list(refused)
    for row in probeable:
        resolution = resolutions.get(row.id)
        if resolution is None:
            continue
        if resolution["match"] == "ambiguous":
            findings.extend(collision_findings(row, resolution, price_sides.get(row.id)))
        else:
            findings.extend(live_findings(row, resolution))
    findings.extend(twin_findings(probeable))

    order = {Severity.repair: 0, Severity.review: 1, Severity.info: 2}
    report.findings = sorted(findings, key=lambda f: (order[f.severity], f.user_id, f.security_id))
    report.rows_scanned = len(probeable)
    report.elapsed = time.monotonic() - started
    log.info("repair report: %s (%.1fs)", report.summary(), report.elapsed)
    return report
