"""Look-through exposure breakdown (M18/M19, docs/05).

Distributes every open position's market value into company / sector /
geography / asset-class buckets. Pure aggregation over CACHED rows —
positions from ``valuation.compute_positions``, composition from
``etf_holdings`` and research data from ``fundamentals``; no network
anywhere (docs/07 cache-only convention for derived views).

Look-through is RECURSIVE (M19): a composition row whose ticker resolves to
another fund of the same user with its own rows is distributed through those
rows in turn, down to ``MAX_DEPTH`` levels, with a visited-set guard against
cycles. The per-level denominator rule is unchanged: weights are distributed
against ``max(sum_of_weights, 100)`` — over-known funds renormalize
(overlapping share classes never inflate exposure), partially-known funds
keep face weights and their missing share lands in ``Unclassified``.

SECTOR has one extra resolution tier (M166, docs/16 §Sector chain): no provider
publishes a sector for every sub-holding, so between the row's own column and
``Unclassified`` the bucket consults the tenant's own cross-fund sector index
and the two static tables in ``services.sector_map``. Whatever those leave
unattributed inside a fund whose OWN published sector mix is known is split
across that mix and flagged ``estimated`` — a fund-level allocation is not a
per-company fact and the payload says so.

DRILL (M169, docs/16 §Exposure drill): the walk already knows every cent's
trail — which position, through which funds, into which leaf — and simply
dropped it after bucketing. ``breakdown(..., drill=<bucket key>)`` keeps that
trail for ONE bucket and returns it as a ``drill`` block: the contributing
positions, each fund's named holdings, and the ``via`` chain that says how the
money got there. It is aggregation over the SAME walk, so the drill's numbers
reconcile to the bucket by construction — the walk is never run twice and no
attribution rule is re-implemented here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Literal

from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.etf_holdings import EtfAssetClass, EtfHolding
from app.models.fundamentals import Fundamentals
from app.models.securities import Security, SecurityKind
from app.services import valuation
from app.services.company_country_map import country_for_symbol
from app.services.sector_map import (
    SectorIndex,
    SectorMaps,
    build_sector_index,
    canonical_sector,
    fund_sector_mix,
    static_sector_for_symbol,
)
from app.tax.acb import cents

Dimension = Literal["sector", "geography", "asset_class", "company"]

UNCLASSIFIED = "Unclassified"
_FUND_KINDS = frozenset({SecurityKind.etf, SecurityKind.mutual_fund})
# M147 honesty threshold: a fund whose rows sum to >= 99% is NOT reported
# as "partial" — a sub-1% residual is noise, not something a note explains.
_PARTIAL_WEIGHT_MAX = Decimal("99")
_HUNDRED = Decimal("100")
# Position fund = level 1; a fund that would sit at level MAX_DEPTH + 1 is a
# leaf keyed by its own ticker (docs/05).
MAX_DEPTH = 3

#: M169: a drill row is a headline, not an audit log. Each level of the drill
#: (the bucket's contributors, and every fund's own holdings) keeps its top
#: ``DRILL_N`` by value and folds the rest into one ``other`` block, so a fund
#: with 300 holdings opens as 25 rows plus a count — the same rule M167's
#: ``exposureFold`` applies to the exposure LIST, on the wire this time.
DRILL_N = 25


@dataclass(slots=True)
class _Trail:
    """One attribution the walk performed (M169, docs/16 §Exposure drill).

    The walk always knew this and threw it away after adding ``amount`` to
    ``buckets[bucket]``. Keeping it for ONE bucket is what turns "Canada is
    20.5% of the portfolio" into "…because these names are". Nothing here
    recomputes anything: every field is a value the walk already had in hand
    at the moment it placed the cent.

    ``chain`` is the money's path — the funds it passed through, OUTERMOST
    FIRST, each as ``(security_id, symbol)``. A stock held directly has an
    empty chain; a Microsoft leaf reached as VEQT → VXUS has
    ``((VEQT, "VEQT"), (VXUS, "VXUS"))`` and renders ``via: ["VEQT", "VXUS"]``.
    That is the depth-honesty rule in one field: the leaf is named AND the
    path is named, so the payload can say "your $x reaches Microsoft of the
    USA via VEQT→VXUS" instead of implying you hold Microsoft directly.
    """

    bucket: str
    amount: Decimal
    chain: tuple[tuple[int | None, str | None], ...]
    position_security_id: int | None
    #: The accounts the TOP-LEVEL position sits in — where the money actually
    #: is. A leaf inside a fund is held through whatever accounts hold the
    #: fund; it never has an account of its own.
    position_accounts: tuple[int, ...]
    #: The composition row this cent came from. ``None`` for a whole-position
    #: leaf (a direct stock) or a fund-level allocation the sink performed.
    holding: EtfHolding | None = None
    #: The security the row's ticker resolved to, when it resolved at all.
    sub: Security | None = None
    #: How the walk labelled this cent. ``named`` — a row or a stock with
    #: fundamentals named it. ``estimated`` — M166's fund-mix split moved it
    #: with no row behind it. ``unattributed`` — the denominator rule left it
    #: in ``Unclassified`` (a residual, or a fund with no rows at all).
    kind: Literal["named", "estimated", "unattributed"] = "named"


def _cents(value: Decimal) -> Decimal:
    """Breakdown money rounding — ROUND_HALF_EVEN, DELIBERATELY (docs/16 §4).

    docs/16 pins `market_value_cad` / `total_cad` / `unclassified_cad` /
    `estimated_cad` as "cents-quantized (`ROUND_HALF_EVEN`) `Decimal` values
    serialized as strings", while pinning `weight_pct` separately as
    ROUND_HALF_UP (line 391 below uses that mode, per the same doc). The
    distinction is specified, so it is preserved rather than normalized.

    Routed through the canonical `app.tax.acb.cents` with the mode stated, so
    there is ONE rounding implementation app-wide. Do NOT "fix" this to
    half-up.
    """
    return cents(value, rounding=ROUND_HALF_EVEN)


def _pct(part: Decimal, whole: Decimal) -> float:
    """Share of ``whole`` as a 2-dp float (M169's drill uses the same
    rounding as the items above it, so a drill row and its bucket row can
    never disagree about the same dollar)."""
    if whole <= 0:
        return 0.0
    return float((part / whole * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _sector_bucket(
    holding: EtfHolding,
    sub_fundamentals: Fundamentals | None,
    index: SectorIndex | None,
    sector_maps: SectorMaps | None = None,
) -> str:
    """Sector resolution for one composition row (M166, docs/16 §Sector chain).

    The row's own column → the tenant's cross-fund sector index (the same
    company already labelled by another of the user's funds) → the static
    ticker map → the resolved security's ``fundamentals`` row →
    ``Unclassified``. Every step is exact or absent: the only ESTIMATE in the
    sector pipeline is the fund-mix split in :func:`_sink`, which runs later
    on whatever this function could not place.
    """
    direct = canonical_sector(holding.sector)
    if direct:
        return direct
    if index is not None:
        hit = index.lookup(holding.holding_symbol, holding.country)
        if hit:
            return hit
    hit = static_sector_for_symbol(holding.holding_symbol, sector_maps)
    if hit:
        return hit
    if sub_fundamentals is not None:
        deeper = canonical_sector(sub_fundamentals.sector)
        if deeper:
            return deeper
    return UNCLASSIFIED


def _row_bucket(
    holding: EtfHolding,
    dimension: Dimension,
    sub_fundamentals: Fundamentals | None,
    index: SectorIndex | None = None,
    sector_maps: SectorMaps | None = None,
) -> str:
    """Dimension label for one composition row treated as a leaf: the row's
    own column first, then the matched security's fundamentals, then — for
    geography only — the static company→country map (M147, last resort,
    deterministic), then Unclassified (company never degrades — a row
    always has a name). Sector takes the M166 chain instead; the other three
    dimensions are unchanged."""
    if dimension == "company":
        return holding.holding_symbol or holding.holding_name
    if dimension == "asset_class":
        return holding.asset_class.value
    if dimension == "sector":
        return _sector_bucket(holding, sub_fundamentals, index, sector_maps)
    direct = holding.region or holding.country
    if direct:
        return direct
    if sub_fundamentals is not None and sub_fundamentals.country:
        return sub_fundamentals.country
    # M147: a fundamentals row without a country falls to the static
    # map before Unclassified (yfinance leaves funds' country NULL).
    return country_for_symbol(holding.holding_symbol) or UNCLASSIFIED


def _fundamentals_bucket(
    row: Fundamentals,
    dimension: Dimension,
    security: Security,
    sector_maps: SectorMaps | None = None,
) -> str:
    if dimension == "company":
        return security.symbol
    if dimension == "sector":
        # M166: the same chain as a composition row, one source shorter — a
        # stock's own fundamentals is the row and there is no sub-security.
        return (
            canonical_sector(row.sector)
            or static_sector_for_symbol(security.symbol, sector_maps)
            or UNCLASSIFIED
        )
    if dimension == "geography":
        # M147: a fundamentals row without a country falls to the static
        # map before Unclassified (yfinance leaves funds' country NULL).
        return row.country or country_for_symbol(security.symbol) or UNCLASSIFIED
    return "stock"


def _sink(
    value: Decimal,
    dimension: Dimension,
    fund_symbol: str | None,
    buckets: dict[str, Decimal],
    estimated: dict[str, Decimal],
    *,
    trail: list[_Trail] | None = None,
    drill_key: str | None = None,
    chain: tuple[tuple[int | None, str | None], ...] = (),
    position_security_id: int | None = None,
    position_accounts: tuple[int, ...] = (),
    fund_security: Security | None = None,
    sector_maps: SectorMaps | None = None,
) -> None:
    """Attribute value the sector chain could not place (M166).

    For ``sector`` only, a fund whose OWN published GICS mix is known
    (``services.sector_map``) absorbs it: the amount is split across the mix,
    renormalized against the mix's own total so the M19 full-attribution rule
    still holds — the split only ever redistributes value that was going to
    ``Unclassified``, so no bucket can exceed what it received. Every cent it
    moves is recorded in ``estimated`` so the payload can label the estimate.

    Non-sector dimensions, non-equity rows and funds with no published mix go
    to ``Unclassified`` exactly as before: a bond has no GICS sector, and an
    unlisted fund has no mix to inherit.

    M169: a cent the sink moves still has a trail — it just has no ROW behind
    it, which is exactly what ``kind="estimated"`` / ``"unattributed"`` says.
    The drill reports those slices on the FUND's contribution line (there is
    no holding to name), so a fund that is part sourced, part estimated
    reconciles to the bucket instead of quietly under-reporting.
    """
    if dimension == "sector" and fund_symbol:
        mix = fund_sector_mix(fund_symbol, sector_maps)
        if mix:
            total = sum(mix.values(), Decimal("0"))
            if total > 0:
                for sector, weight in mix.items():
                    share = value * weight / total
                    buckets[sector] += share
                    estimated[sector] += share
                    if trail is not None and sector == drill_key:
                        trail.append(
                            _Trail(
                                bucket=sector,
                                amount=share,
                                chain=chain,
                                position_security_id=position_security_id,
                                position_accounts=position_accounts,
                                holding=None,
                                sub=fund_security,
                                kind="estimated",
                            )
                        )
                return
    buckets[UNCLASSIFIED] += value
    if trail is not None and UNCLASSIFIED == drill_key:
        trail.append(
            _Trail(
                bucket=UNCLASSIFIED,
                amount=value,
                chain=chain,
                position_security_id=position_security_id,
                position_accounts=position_accounts,
                holding=None,
                sub=fund_security,
                kind="unattributed",
            )
        )


def _fallback_bucket(security: Security | None, dimension: Dimension) -> str:
    """Whole-position fallback: Unclassified, except asset_class degrades to
    the security kind and company to the security symbol."""
    if security is not None:
        if dimension == "asset_class":
            return security.kind.value
        if dimension == "company":
            return security.symbol
    return UNCLASSIFIED


def _resolve_sub_security(
    holding: EtfHolding,
    symbol_index: dict[str, list[Security]],
    holdings_by_fund: dict[int, list[EtfHolding]],
) -> Security | None:
    """User security matching a composition row's ticker, if any. Several
    exchanges can share one symbol ((user_id, symbol, exchange) is the
    unique constraint), so prefer: fund WITH composition rows → any fund →
    lowest id. Exact case-insensitive match only — no exchange guessing."""
    if not holding.holding_symbol:
        return None
    matches = symbol_index.get(holding.holding_symbol.upper())
    if not matches:
        return None
    for candidate in matches:
        if candidate.kind in _FUND_KINDS and holdings_by_fund.get(candidate.id):
            return candidate
    for candidate in matches:
        if candidate.kind in _FUND_KINDS:
            return candidate
    return matches[0]


def _distribute(
    value: Decimal,
    holdings: list[EtfHolding],
    depth: int,
    visited: set[int],
    buckets: dict[str, Decimal],
    dimension: Dimension,
    *,
    symbol_index: dict[str, list[Security]],
    holdings_by_fund: dict[int, list[EtfHolding]],
    fundamentals_by_security: dict[int, Fundamentals],
    sector_index: SectorIndex | None = None,
    estimated: dict[str, Decimal] | None = None,
    fund_symbol: str | None = None,
    trail: list[_Trail] | None = None,
    drill_key: str | None = None,
    chain: tuple[tuple[int | None, str | None], ...] = (),
    position_security_id: int | None = None,
    position_accounts: tuple[int, ...] = (),
    fund_security: Security | None = None,
    sector_maps: SectorMaps | None = None,
) -> None:
    """Distribute one position's value through composition rows, recursing
    into sub-funds (docs/05). At every level the denominator is
    max(sum_of_weights, 100) and the residual lands in :func:`_sink`, so the
    full value is always attributed — nothing is lost.

    ``fund_symbol`` is the symbol of the fund these ``holdings`` belong to; it
    is what a M166 fund-mix estimate is looked up by, and it changes as the
    walk descends (``VUN``'s unattributed share inherits ``VTI``'s mix, not
    ``VUN``'s). A row only reaches the sink when it is EQUITY — a cash, bond
    or commodity leg has no GICS sector and stays Unclassified.

    M169 (``trail``/``drill_key``): when the caller is drilling ONE bucket,
    every attribution is ALSO recorded as a :class:`_Trail` carrying the
    chain of funds the money passed through. Both knobs default off, so the
    aggregate walk is byte-identical to the pre-M169 one — the trail is a
    second consumer of the same decision, never a second walk.
    """
    if estimated is None:
        estimated = defaultdict(lambda: Decimal("0"))
    total_weight = sum((h.weight_pct for h in holdings), Decimal("0"))
    if total_weight <= 0:
        _sink(
            value,
            dimension,
            fund_symbol,
            buckets,
            estimated,
            trail=trail,
            drill_key=drill_key,
            chain=chain,
            position_security_id=position_security_id,
            position_accounts=position_accounts,
            fund_security=fund_security,
            sector_maps=sector_maps,
        )
        return
    denominator = max(total_weight, _HUNDRED)
    allocated = Decimal("0")
    for holding in holdings:
        amount = value * holding.weight_pct / denominator
        allocated += amount
        sub = _resolve_sub_security(holding, symbol_index, holdings_by_fund)
        sub_rows = holdings_by_fund.get(sub.id, []) if sub is not None else []
        if (
            sub is not None
            and sub.kind in _FUND_KINDS
            and sub_rows
            and depth < MAX_DEPTH
            and sub.id not in visited
        ):
            _distribute(
                amount,
                sub_rows,
                depth + 1,
                visited | {sub.id},
                buckets,
                dimension,
                symbol_index=symbol_index,
                holdings_by_fund=holdings_by_fund,
                fundamentals_by_security=fundamentals_by_security,
                sector_index=sector_index,
                estimated=estimated,
                fund_symbol=sub.symbol,
                trail=trail,
                drill_key=drill_key,
                # The money stepped ONE fund deeper — the leaf's `via` grows
                # by the fund it just entered, not by the row that named it.
                chain=(*chain, (sub.id, sub.symbol)),
                position_security_id=position_security_id,
                position_accounts=position_accounts,
                fund_security=sub,
                sector_maps=sector_maps,
            )
        else:
            sub_fundamentals = fundamentals_by_security.get(sub.id) if sub is not None else None
            bucket = _row_bucket(holding, dimension, sub_fundamentals, sector_index, sector_maps)
            if (
                bucket == UNCLASSIFIED
                and holding.asset_class == EtfAssetClass.stock
                and dimension == "sector"
            ):
                _sink(
                    amount,
                    dimension,
                    fund_symbol,
                    buckets,
                    estimated,
                    trail=trail,
                    drill_key=drill_key,
                    chain=chain,
                    position_security_id=position_security_id,
                    position_accounts=position_accounts,
                    fund_security=fund_security,
                    sector_maps=sector_maps,
                )
            else:
                buckets[bucket] += amount
                if trail is not None and bucket == drill_key:
                    trail.append(
                        _Trail(
                            bucket=bucket,
                            amount=amount,
                            chain=chain,
                            position_security_id=position_security_id,
                            position_accounts=position_accounts,
                            holding=holding,
                            sub=sub,
                            kind="named",
                        )
                    )
    residual = value - allocated
    if residual:
        _sink(
            residual,
            dimension,
            fund_symbol,
            buckets,
            estimated,
            trail=trail,
            drill_key=drill_key,
            chain=chain,
            position_security_id=position_security_id,
            position_accounts=position_accounts,
            fund_security=fund_security,
            sector_maps=sector_maps,
        )


def _drill_leaf(
    trail: _Trail,
    *,
    bucket_total: Decimal,
) -> dict:
    """One leaf row of a drill (M169) — a composition row that landed here.

    ``via`` is the chain of FUND SYMBOLS the money walked through, outermost
    first, EXCLUDING the leaf itself: a Microsoft row reached as
    VEQT → VXUS reports ``["VEQT", "VXUS"]``. The depth-honesty rule is that
    the chain is always present and always the walk's own path — never a
    reconstruction, never truncated to flatter the reader.
    """
    holding = trail.holding
    sub = trail.sub
    symbol = (sub.symbol if sub is not None else holding.holding_symbol) or None
    name = (sub.name if sub is not None else holding.holding_name) or None
    return {
        # A holding that RESOLVED to a fund we could not walk into (depth cap,
        # cycle, no rows) is a `sub_fund`: the money stopped there, and the
        # payload says so rather than implying the ticker is a company.
        "position_kind": "sub_fund" if sub is not None and sub.kind in _FUND_KINDS else "direct",
        "security_id": sub.id if sub is not None else holding.holding_security_id,
        "symbol": symbol,
        "name": name,
        # The weight the PROVIDER published for this row in its own parent —
        # the native figure, before this walk's renormalization against
        # max(sum, 100). Both numbers are true; they answer different questions.
        "native_weight_share": str(_cents(holding.weight_pct)),
        "market_value_cad": _cents(trail.amount),
        "weight_pct": _pct(trail.amount, bucket_total),
        # Where this ROW's weights came from (M149's provenance). A leaf with
        # no row of its own (the estimate / residual below) reports null.
        "provenance_source": holding.source.value,
        "estimated": False,
        "unattributed": False,
        "via": [hop for _, hop in trail.chain],
        "depth": len(trail.chain),
        "account_ids": list(trail.position_accounts),
        "account_count": len(trail.position_accounts),
    }


def _drill_synthetic(
    trail: _Trail,
    *,
    bucket_total: Decimal,
) -> dict:
    """The slice of a drill with NO row behind it (M169).

    M166's fund-mix estimate and the denominator residual are real dollars in
    a real bucket; they just are not attributable to a named holding. The
    honest row is the FUND that produced them — "VEQT's published mix says
    so" — flagged, with the chain stopping AT that fund. Merging them into a
    total instead would make the drill fail to reconcile to the bucket, which
    is the one thing M91-grade honesty forbids.
    """
    fund = trail.sub
    # The path TO the fund, not through it: this slice IS the fund's own
    # say-so, so the fund is the row and its symbol is not its own `via`.
    via = [hop for _, hop in trail.chain[:-1]]
    return {
        "position_kind": "sub_fund",
        "security_id": fund.id if fund is not None else None,
        "symbol": fund.symbol if fund is not None else None,
        "name": (fund.name if fund is not None else None) or "Unattributed",
        "native_weight_share": None,
        "market_value_cad": _cents(trail.amount),
        "weight_pct": _pct(trail.amount, bucket_total),
        "provenance_source": None,
        "estimated": trail.kind == "estimated",
        "unattributed": trail.kind == "unattributed",
        "via": via,
        "depth": len(via),
        "account_ids": list(trail.position_accounts),
        "account_count": len(trail.position_accounts),
    }


def _merge_synthetic(rows: list[dict], bucket_total: Decimal) -> list[dict]:
    """Collapse a fund's several no-row slices into ONE row (M169).

    The M166 sink fires once per sector a fund's residual is split across and
    once per residual event, so one fund can produce several synthetic rows
    with nothing to tell them apart but their amount. The docstring's promise
    is "the FUND that produced them" — singular — so rows that share a symbol,
    a kind and a chain are one fact reported several times. Real leaves are
    never merged: two holdings with the same ticker are two holdings.
    """
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
        if row["provenance_source"] is not None or row["native_weight_share"] is not None:
            # A named leaf — it has a row behind it, so it stands alone.
            key = ("leaf", id(row))
            merged[key] = row
            order.append(key)
            continue
        key = (row["symbol"], row["estimated"], row["unattributed"], tuple(row["via"]))
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            order.append(key)
        else:
            existing["market_value_cad"] += row["market_value_cad"]
    rows = [merged[key] for key in order]
    for row in rows:
        row["weight_pct"] = _pct(row["market_value_cad"], bucket_total)
    return rows


def _fold_drill_rows(
    rows: list[dict], bucket_total: Decimal, limit: int
) -> tuple[list[dict], dict | None]:
    """Sort by value desc, keep the top ``limit``, and account for the rest.

    M167's truncation rule, on the wire: a drill must not infinite-scroll, so
    the tail is ONE honest block — its exact cent-sum and its count — never a
    silently dropped remainder. ``bucket_total`` is what the kept + other rows
    are weighed against, so the percentages still describe the bucket.
    """
    rows.sort(key=lambda row: (row["market_value_cad"], row["symbol"] or ""), reverse=True)
    kept = rows[: max(0, limit)]
    tail = rows[max(0, limit) :]
    tail_value = sum((row["market_value_cad"] for row in tail), Decimal("0"))
    other = (
        {
            "count": len(tail),
            "market_value_cad": str(_cents(tail_value)),
            "weight_pct": _pct(tail_value, bucket_total),
        }
        if tail
        else None
    )
    for row in kept:
        row["market_value_cad"] = str(row["market_value_cad"])
    return kept, other


def _drill_block(
    trails: list[_Trail],
    *,
    key: str,
    dimension: Dimension,
    bucket_value: Decimal,
    total_cad: Decimal,
    securities: dict[int, Security],
    limit: int = DRILL_N,
) -> dict:
    """The M169 drill block for ONE bucket (docs/16 §Exposure drill).

    Contributions are the TOP-LEVEL positions that fed the bucket — a stock
    held directly is one row that IS its own leaf; a fund is a row whose
    ``holdings`` are the leaves it contributed, at whatever depth the M19 walk
    actually used. Every leaf carries ``via``, so a name reached two funds
    deep is never mistaken for something the user holds directly.

    Reconciliation is by construction: the same ``trails`` that produced the
    bucket's value produce these rows, so Σ contributions == the bucket (up to
    per-row cent rounding, which ``contributions_cad`` absorbs by being the
    SERVER's total rather than a client sum).
    """
    by_position: dict[int | None, list[_Trail]] = defaultdict(list)
    for trail in trails:
        by_position[trail.position_security_id].append(trail)

    contributions: list[dict] = []
    for security_id, group in by_position.items():
        value = sum((trail.amount for trail in group), Decimal("0"))
        # A trail with an EMPTY chain never entered a fund: it IS the
        # position (a stock held directly, or a fund with no rows at all).
        # Nesting it under itself would print the same dollar twice, so only
        # trails that walked through a fund become `holdings`.
        walked = [trail for trail in group if trail.chain]
        rows = _merge_synthetic(
            [
                _drill_leaf(trail, bucket_total=value)
                if trail.holding is not None
                else _drill_synthetic(trail, bucket_total=value)
                for trail in walked
            ],
            value,
        )
        holdings, holdings_other = _fold_drill_rows(rows, value, limit)
        estimated_cad = _cents(
            sum((trail.amount for trail in group if trail.kind == "estimated"), Decimal("0"))
        )
        unattributed_cad = _cents(
            sum((trail.amount for trail in group if trail.kind == "unattributed"), Decimal("0"))
        )
        accounts = sorted({account for trail in group for account in trail.position_accounts})
        security = securities.get(security_id) if security_id is not None else None
        contributions.append(
            {
                # `direct` — you hold the security itself and it named a bucket;
                # `fund` — a position the walk looked THROUGH (its leaves follow).
                # A fund with no rows lands wholly in Unclassified and reports
                # `unattributed` with empty `holdings` — M147's story, one
                # level down, still reconciling to the cent.
                "position_kind": (
                    "fund" if security is not None and security.kind in _FUND_KINDS else "direct"
                ),
                "security_id": security_id,
                "symbol": security.symbol if security is not None else None,
                "name": security.name if security is not None else None,
                "account_ids": accounts,
                "account_count": len(accounts),
                # A top-level position has no parent to have a weight in.
                "native_weight_share": None,
                "market_value_cad": str(_cents(value)),
                "weight_pct": _pct(value, bucket_value),
                # A POSITION is never itself an estimate; these are the slices
                # OF it that were (M166) and the slices nothing named.
                "provenance_source": None,
                "estimated_cad": str(estimated_cad),
                "estimated": estimated_cad > 0,
                "unattributed_cad": str(unattributed_cad),
                "unattributed": unattributed_cad > 0,
                # `provenance_source` of a DIRECT position is what its own
                # single leaf would carry — null, because a position has no
                # composition row to have a source. A fund's leaves carry
                # theirs individually.
                "via": [],
                "depth": 0,
                "holdings": holdings,
                "holdings_other": holdings_other,
            }
        )
    contributions.sort(
        key=lambda row: (Decimal(row["market_value_cad"]), row["symbol"] or ""), reverse=True
    )

    kept = contributions[: max(0, limit)]
    tail = contributions[max(0, limit) :]
    tail_value = sum((Decimal(row["market_value_cad"]) for row in tail), Decimal("0"))
    return {
        "key": key,
        "dimension": dimension,
        "bucket_market_value_cad": str(_cents(bucket_value)),
        "bucket_weight_pct": _pct(bucket_value, total_cad),
        # THE total for this drill — the server's number, never a client sum of
        # the rows below (which round per-row and can drift a cent).
        "contributions_cad": str(_cents(bucket_value)),
        "contributions": kept,
        "other": (
            {
                "count": len(tail),
                "market_value_cad": str(_cents(tail_value)),
                "weight_pct": _pct(tail_value, bucket_value),
            }
            if tail
            else None
        ),
        # What the WALK stops at (docs/16). A deeper chain stays hidden
        # server-side; this is the disclosure, not a client-side cap.
        "depth_cap": MAX_DEPTH,
    }


def breakdown(
    session: Session,
    user_id: int,
    dimension: Dimension,
    account_id: int | None = None,
    drill: str | None = None,
) -> dict:
    """Aggregate open positions into exposure buckets for one dimension.

    Positions are grouped by security first, so the same security held across
    accounts is looked through exactly once. Funds with ``etf_holdings``
    rows distribute their value recursively through sub-fund composition
    (depth ≤ MAX_DEPTH, cycle-safe); at every level weights are distributed
    against ``max(sum_of_weights, 100)`` — the full position value is always
    attributed. Composition rows without a recursable fund fall back to
    their own column, then the matched security's ``fundamentals`` row.
    Stocks fall back to their ``fundamentals`` row; anything else is wholly
    ``Unclassified`` except that asset_class degrades to the security kind
    and company to the security symbol.

    ``sector`` runs the M166 chain instead of that one-column fallback: the
    row's own column → the tenant's cross-fund sector index → the static
    ticker map → ``fundamentals``. Whatever an EQUITY row still leaves
    unattributed inside a fund whose own published GICS mix is known is
    split across that mix and reported under ``estimated``; everything else
    (bonds, cash, funds with no published mix, and every other dimension)
    lands in ``Unclassified`` exactly as before.

    The response also lists ``unclassified_funds`` — fund positions that
    landed wholly in ``Unclassified`` because they carry NO composition
    rows (M147 honesty copy: the UI explains WHY the slice is empty and
    names the fund to import).

    ``drill`` (M169, docs/16 §Exposure drill) names ONE bucket key. When it
    is set, the SAME walk also records a :class:`_Trail` per attribution into
    that bucket and the response gains a ``drill`` block naming the
    contributing positions, their leaves, and each leaf's ``via`` chain. It
    is a second consumer of the decisions the walk already made — no second
    walk, no new attribution rule, and no network (docs/07).
    """
    buckets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    estimated: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    per_security: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    # M169: which accounts hold each security — a drill row's `account_ids`
    # answer "where is this money", and a fund's leaves are held through the
    # fund's accounts, never one of their own.
    per_security_accounts: dict[int, set[int]] = defaultdict(set)
    # M169: the trail for ONE bucket (empty when not drilling — the aggregate
    # walk then allocates nothing extra at all).
    trail: list[_Trail] | None = [] if drill is not None else None
    # M147: funds whose WHOLE position fell into Unclassified because they
    # carry no composition rows — the honesty copy's evidence list.
    unclassified_funds_value: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for position in valuation.compute_positions(session, user_id, account_id=account_id):
        market_value_cad = position["market_value_cad"]
        if market_value_cad is None:
            continue  # no usable price → nothing to attribute
        per_security[position["security_id"]] += market_value_cad
        per_security_accounts[position["security_id"]].add(position["account_id"])

    securities: dict[int, Security] = {}
    symbol_index: dict[str, list[Security]] = {}
    holdings_by_fund: dict[int, list[EtfHolding]] = {}
    fundamentals_by_security: dict[int, Fundamentals] = {}
    sector_index: SectorIndex | None = None
    # M168: BOTH static families are rows now, so the breakdown loads them
    # once — two queries — and hands the snapshot to every sector lookup.
    # The other three dimensions never read them (the walk stays cache-only
    # and one query lighter than the sector walk by exactly these two).
    sector_maps = SectorMaps.load(session) if dimension == "sector" else None
    if per_security:
        # The recursion resolves arbitrary user securities (a holding ticker
        # need not be held), so preload the whole tenancy — small per user.
        for security in session.exec(
            select(Security).where(Security.user_id == user_id).order_by(Security.id)
        ).all():
            securities[security.id] = security
            symbol_index.setdefault(security.symbol.upper(), []).append(security)
        all_holdings = session.exec(select(EtfHolding).where(EtfHolding.user_id == user_id)).all()
        for holding in all_holdings:
            holdings_by_fund.setdefault(holding.security_id, []).append(holding)
        # M166: the tenant's own labelled rows ARE the cross-fund sector
        # index — a company labelled by one fund answers for every other.
        # Built only for the sector dimension (the other three are unchanged).
        if dimension == "sector":
            sector_index = build_sector_index(all_holdings)
        for row in session.exec(select(Fundamentals).where(Fundamentals.user_id == user_id)).all():
            fundamentals_by_security[row.security_id] = row

    accounts_by_security = {
        security_id: tuple(sorted(account_ids))
        for security_id, account_ids in per_security_accounts.items()
    }

    for security_id, market_value in per_security.items():
        security = securities.get(security_id)
        holdings = holdings_by_fund.get(security_id, [])
        fundamentals_row = fundamentals_by_security.get(security_id)
        accounts = accounts_by_security.get(security_id, ())

        if security is not None and security.kind in _FUND_KINDS and holdings:
            _distribute(
                market_value,
                holdings,
                depth=1,
                visited={security_id},
                buckets=buckets,
                dimension=dimension,
                symbol_index=symbol_index,
                holdings_by_fund=holdings_by_fund,
                fundamentals_by_security=fundamentals_by_security,
                sector_index=sector_index,
                estimated=estimated,
                fund_symbol=security.symbol,
                trail=trail,
                drill_key=drill,
                # The money STARTS at this fund — the leaf's `via` names every
                # fund it passes through, and this is the first one.
                chain=((security.id, security.symbol),) if trail is not None else (),
                position_security_id=security_id,
                position_accounts=accounts,
                fund_security=security,
                sector_maps=sector_maps,
            )
        elif (
            security is not None
            and fundamentals_row is not None
            and security.kind == SecurityKind.stock
        ):
            bucket = _fundamentals_bucket(fundamentals_row, dimension, security, sector_maps)
            buckets[bucket] += market_value
            if trail is not None and bucket == drill:
                # A stock you hold directly IS the leaf: no chain, no row —
                # the position is the whole story of this cent.
                trail.append(
                    _Trail(
                        bucket=bucket,
                        amount=market_value,
                        chain=(),
                        position_security_id=security_id,
                        position_accounts=accounts,
                        holding=None,
                        sub=security,
                        kind="unattributed" if bucket == UNCLASSIFIED else "named",
                    )
                )
        else:
            bucket = _fallback_bucket(security, dimension)
            if security is not None and security.kind in _FUND_KINDS and bucket == UNCLASSIFIED:
                unclassified_funds_value[security_id] += market_value
            buckets[bucket] += market_value
            if trail is not None and bucket == drill:
                trail.append(
                    _Trail(
                        bucket=bucket,
                        amount=market_value,
                        chain=(),
                        position_security_id=security_id,
                        position_accounts=accounts,
                        holding=None,
                        sub=security,
                        kind="unattributed" if bucket == UNCLASSIFIED else "named",
                    )
                )

    total_cad = _cents(sum(buckets.values(), Decimal("0")))
    items = []
    for key, value in buckets.items():
        market_value_cad = _cents(value)
        weight_pct = _pct(value, total_cad)
        estimated_cad = _cents(estimated.get(key, Decimal("0")))
        items.append(
            {
                "key": key,
                "market_value_cad": market_value_cad,
                "weight_pct": weight_pct,
                # M166: how much of THIS bucket came from a fund's published
                # sector mix rather than a sourced per-holding column, and the
                # boolean the legend marks. A bucket can be part sourced and
                # part estimated — the number is what the copy quotes.
                "estimated_cad": estimated_cad,
                "estimated": estimated_cad > 0,
            }
        )
    items.sort(key=lambda item: item["market_value_cad"], reverse=True)
    unclassified = buckets.get(UNCLASSIFIED, Decimal("0"))
    # M147 honesty evidence, two reasons the UI must tell apart:
    # "missing" — the fund has NO composition rows (Import fills it);
    # "partial" — the provider only published its top slice, so the
    # remainder of the position lands in Unclassified by the
    # renormalization rule (docs/16) until a fuller source exists.
    unclassified_funds = [
        {
            "security_id": security_id,
            "symbol": securities[security_id].symbol,
            "name": securities[security_id].name,
            "market_value_cad": _cents(value),
            "reason": "missing",
            "imported_weight_pct": None,
        }
        for security_id, value in sorted(
            unclassified_funds_value.items(), key=lambda kv: kv[1], reverse=True
        )
        if security_id in securities and value > 0
    ]
    for security_id, market_value in per_security.items():
        security = securities.get(security_id)
        rows = holdings_by_fund.get(security_id, [])
        if security is None or security.kind not in _FUND_KINDS or not rows:
            continue
        total_weight = sum((row.weight_pct for row in rows), Decimal("0"))
        if total_weight >= _PARTIAL_WEIGHT_MAX:
            continue  # fully (or near-fully) known — no material residual
        unclassified_funds.append(
            {
                "security_id": security_id,
                "symbol": security.symbol,
                "name": security.name,
                "market_value_cad": _cents(market_value),
                "reason": "partial",
                "imported_weight_pct": str(_cents(total_weight)),
            }
        )
    unclassified_funds.sort(key=lambda item: item["market_value_cad"], reverse=True)
    result = {
        "items": items,
        "total_cad": total_cad,
        "unclassified_cad": _cents(unclassified),
        # M166: the slice of the pie that came from a fund's PUBLISHED sector
        # mix instead of a per-holding column. Zero for every dimension other
        # than sector, and for portfolios whose funds all publish their rows
        # per-holding; the UI labels it (docs/09 §Exposure honesty).
        "estimated_cad": _cents(sum(estimated.values(), Decimal("0"))),
        "unclassified_funds": unclassified_funds,
    }
    if drill is not None:
        # M169: the SAME walk's trail for this one bucket. Absent (not null)
        # when not requested, so a non-drilling consumer's payload is
        # byte-identical to the pre-M169 one.
        result["drill"] = _drill_block(
            trail or [],
            key=drill,
            dimension=dimension,
            bucket_value=buckets.get(drill, Decimal("0")),
            total_cad=total_cad,
            securities=securities,
        )
    return result
