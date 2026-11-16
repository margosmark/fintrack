"""Sector classification sources for the exposure engine (M166, docs/16 §Sector chain).

Sector is the one exposure dimension no provider publishes for every sub-holding
(M166): the iShares issuer CSVs and the Vanguard CANADIAN gateway carry a GICS
column per row, but the Vanguard US profile JSON does not — it names the holding
(`ticker` / `isin` / `cusip` / `sedol`) and its weight, never its sector. Left
alone that left 62.77% of the portfolio's sector pie `Unclassified` while the
same companies were already labelled somewhere else in the same cache.

This module holds the three cache-only sources the sector bucket falls back to,
in order. None of them fetches — the breakdown stays a pure aggregation
(AGENTS §2, docs/07 cache-only convention).

1. **One vocabulary** (``canonical_sector``). Providers and yfinance spell the
   same GICS sector differently — iShares writes ``Information Technology``,
   yfinance writes ``Technology`` — and two spellings split one pie into two
   slices. Canonical values are the yfinance names, exactly the discipline the
   country map uses for ``fundamentals.country`` (``company_country_map``).
   An unrecognised label passes through UNCHANGED: an unknown word is not a
   reason to drop a value the provider actually published.
2. **The cross-fund sector index** (``SectorIndex``). The same listed company
   appears in many of a user's funds, so a label that arrived with one of them
   answers for every other — S&P 500 rows imported from IVV's issuer CSV label
   the very tickers VTI's rows leave blank. Built from the user's OWN
   ``etf_holdings`` rows at breakdown time (per-tenancy, so nothing crosses a
   tenant boundary) and exact by construction: a ticker resolves only when the
   rows that carry it agree, or when the row's own country picks one of them.
   ``SU``, ``IAG``, ``KEY`` and 103 others are genuinely different companies on
   different exchanges, so a bare-ticker disagreement REFUSES to resolve —
   the M143 lesson (a bare-ticker collision silently picked the wrong VGRO).
3. **The static maps** (:class:`SectorMaps`, rows in ``provider_fund_registry``
   since M168 — families ``static_sector`` and ``fund_sector_mix``).
   The M147 country-map pattern, two shapes:
   * a hand-curated ticker -> sector table for the handful of names no provider
     in the cache has ever labelled;
   * a hand-curated fund ticker -> the fund's OWN published GICS sector mix,
     for US-domiciled equity funds whose profile publishes one and whose
     per-holding rows do not. The engine splits only value that would
     otherwise be `Unclassified` across that mix, and flags the result
     `estimated` (docs/05 §Sector estimation contract) — the mix is a real
     published number, but it is a fund-level allocation, not a per-company
     fact, so the pie says so.

Deliberately incomplete, like the country map: an unknown ticker or fund stays
`Unclassified`. Coverage grows by adding verified rows, never by inferring a
sector from ticker text.
"""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import Session

from app.models.provider_registry import ProviderFundFamily
from app.services import provider_registry

# --- 1. One sector vocabulary -------------------------------------------------
#
# Canonical values are the names yfinance writes into ``fundamentals.sector``,
# because that vocabulary already labels the slices the pies show today
# ("Financial Services", "Technology", "Energy"). Every provider spelling maps
# onto it; the mapping is applied to composition rows, to fundamentals rows and
# to the two static tables below, so no code path can reintroduce a split.
#
# Kept deliberately small: only labels seen live in the three providers
# (iShares `latest-holdings.csv` Sector, Vanguard CA `gicsSectorDescription`,
# the Vanguard US profile `sectorExposure` mix). A label that is not listed
# passes through unchanged rather than being dropped.
SECTOR_ALIASES: dict[str, str] = {
    # GICS 2016 names — the iShares CSVs and both Vanguard payloads
    "information technology": "Technology",
    "materials": "Basic Materials",
    "financials": "Financial Services",
    "health care": "Healthcare",
    "consumer discretionary": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "communication": "Communication Services",
    "telecommunications": "Communication Services",
    "telecom services": "Communication Services",
    # the canonical (yfinance) names themselves — idempotent
    "technology": "Technology",
    "basic materials": "Basic Materials",
    "financial services": "Financial Services",
    "healthcare": "Healthcare",
    "consumer cyclical": "Consumer Cyclical",
    "consumer defensive": "Consumer Defensive",
    "communication services": "Communication Services",
    "industrials": "Industrials",
    "energy": "Energy",
    "real estate": "Real Estate",
    "utilities": "Utilities",
}

# Labels the providers put in a SECTOR column that are not sectors at all.
# iShares' cash legs read "Cash and/or Derivatives"; an empty GICS cell reads
# "Not Applicable". Both mean "this row has no sector", so they resolve to
# None and the row continues down the chain instead of growing a pie slice
# nothing else can be compared against.
SECTOR_NON_SECTORS = frozenset(
    {
        "cash and/or derivatives",
        "cash and derivatives",
        "not applicable",
        "not applicable - cash",
        "n/a",
        "na",
        "-",
    }
)


def canonical_sector(label: str | None) -> str | None:
    """One pie slice per GICS sector, whatever the provider called it.

    ``None`` (and the empty string) stays ``None`` — an unstated sector is not
    a sector, and neither is a cash leg (``SECTOR_NON_SECTORS``). A label
    outside :data:`SECTOR_ALIASES` returns itself: the provider published it,
    and dropping it would silently shrink the pie. Two rows for the same
    company can therefore still split only if a NEW provider spelling is added
    without a mapping entry, which is what the table's comment asks for.
    """
    if not label:
        return None
    clean = label.strip()
    if not clean:
        return None
    lowered = clean.lower()
    if lowered in SECTOR_NON_SECTORS:
        return None
    return SECTOR_ALIASES.get(lowered, clean)


# --- 2. The cross-fund sector index -------------------------------------------


class SectorIndex:
    """Ticker -> GICS sector, harvested from one tenant's own labelled rows.

    Two lookup shapes, in this order:

    * ``(symbol, country)`` — the exact pair, which disambiguates the 106 bare
      tickers that name two different companies on two exchanges (``SU`` is
      Suncor in Canada and Sogefi in Italy);
    * ``symbol`` alone — only when every labelled row carrying that ticker
      agrees on one sector, so a disagreement can never be resolved by luck.

    Both are keyed on the UPPERCASED ticker exactly as the providers wrote it;
    nothing is normalised, guessed or trimmed beyond case.
    """

    __slots__ = ("_by_symbol_country", "_unambiguous")

    def __init__(
        self,
        by_symbol_country: dict[str, dict[str | None, str]],
        unambiguous: dict[str, str],
    ) -> None:
        self._by_symbol_country = by_symbol_country
        self._unambiguous = unambiguous

    def __len__(self) -> int:
        return len(self._unambiguous) + sum(
            1 for entry in self._by_symbol_country.values() if len(entry) > 1
        )

    def lookup(self, symbol: str | None, country: str | None) -> str | None:
        """Sector for a composition row's ticker, or None (never a guess)."""
        if not symbol:
            return None
        clean = symbol.strip().upper()
        if not clean:
            return None
        exact = self._by_symbol_country.get(clean)
        if exact is not None:
            hit = exact.get(country)
            if hit is not None:
                return hit
        return self._unambiguous.get(clean)


def build_sector_index(rows: list) -> SectorIndex:
    """Build the index from ``etf_holdings`` rows (any tenant, cache-only).

    Rows without both a symbol and a canonicalisable sector contribute nothing.
    A ticker seen under two different canonical sectors is dropped from the
    bare-symbol table entirely and survives only as an exact
    ``(symbol, country)`` pair — see :class:`SectorIndex`.
    """
    by_symbol_country: dict[str, dict[str | None, str]] = {}
    by_symbol: dict[str, set[str]] = {}
    for row in rows:
        sector = canonical_sector(row.sector)
        if sector is None or not row.holding_symbol:
            continue
        symbol = row.holding_symbol.strip().upper()
        if not symbol:
            continue
        by_symbol_country.setdefault(symbol, {})[row.country] = sector
        by_symbol.setdefault(symbol, set()).add(sector)
    unambiguous = {
        symbol: next(iter(sectors)) for symbol, sectors in by_symbol.items() if len(sectors) == 1
    }
    return SectorIndex(by_symbol_country, unambiguous)


# --- 3. The static maps, as DATA (M168) ---------------------------------------
#
# Both tables are rows in `provider_fund_registry` now (families
# `static_sector` and `fund_sector_mix`), seeded by the literals this module
# used to carry: 28 ticker -> sector rows and 11 published fund mixes. The
# lookups below are therefore argument-taking rather than module-global, and
# the whole family is loaded ONCE per breakdown — one query each, then plain
# dict lookups for the 25,000-row walk (docs/16 §Sector chain).
#
# Nothing else about the chain changed: same normalisation, same refusal to
# guess, same `estimated` contract. Adding coverage is inserting a row
# (`fintrackerctl provider-funds set`), never editing code.


class SectorMaps:
    """A snapshot of both static families, loaded once per breakdown.

    Read through `services.provider_registry`, which owns the session rules
    (docs/07 §Provider registry): pass the breakdown's own session and the
    snapshot rides the transaction it is already in, pass nothing and one
    short-lived session opens, reads and closes. Either way the maps are
    PLAIN DICTS after this — the walk never touches the database again.
    """

    __slots__ = ("_mix", "_static")

    def __init__(
        self,
        static_by_symbol: dict[str, str],
        mix_by_symbol: dict[str, dict[str, Decimal]],
    ) -> None:
        self._static = static_by_symbol
        self._mix = mix_by_symbol

    @classmethod
    def load(cls, session: Session | None = None) -> SectorMaps:
        """Both families in two queries (one each) — never per-row."""
        static_rows = provider_registry.load_family(
            ProviderFundFamily.static_sector, session=session
        )
        mix_rows = provider_registry.load_family(
            ProviderFundFamily.fund_sector_mix, session=session
        )
        return cls(
            {ticker: str(row.sector) for ticker, row in static_rows.items() if row.sector},
            {ticker: row.mix_decimals() for ticker, row in mix_rows.items() if row.sector_mix},
        )

    @property
    def static_rows(self) -> dict[str, str]:
        """A copy of the ticker -> sector table (tests iterate it)."""
        return dict(self._static)

    @property
    def mix_rows(self) -> dict[str, dict[str, Decimal]]:
        """A copy of the fund -> published mix table (tests iterate it)."""
        return dict(self._mix)

    def static_sector(self, symbol: str | None) -> str | None:
        """The map lookup for one ticker — same normalisation, same refusal."""
        return static_sector_for_symbol(symbol, self)

    def published_mix(self, symbol: str | None) -> dict[str, Decimal] | None:
        """The fund's published mix for one ticker, as Decimals."""
        return fund_sector_mix(symbol, self)

    def __len__(self) -> int:
        return len(self._static) + len(self._mix)


_SUFFIXES = (
    ".TO",
    ".NE",
    ".V",
    ".QC",
    ".AS",
    ".L",
    ".SW",
    ".T",
    ".TW",
    ".KS",
    ".KQ",
    ".HK",
    ".PA",
    ".DE",
    ".MI",
    ".MC",
    ".BR",
    ".ST",
    ".VX",
    ".CN",
    ".AX",
    ".NS",
)


def static_sector_for_symbol(symbol: str | None, maps: SectorMaps | None) -> str | None:
    """Sector from the curated ticker map, or None.

    Exact match first (providers that already write ``BRK/B`` / ``OR.PA``
    answer directly), then the same ticker with a known exchange suffix
    stripped, then the slash-separated share-class form read as a dot —
    the one normalisation the map does, because Vanguard writes ``BRK/B``
    where the country map already knows ``BRK.B``. A miss is ``None``: a
    wrong sector corrupts the pie, an absent one only leaves a row
    Unclassified.
    """
    if not symbol or maps is None:
        return None
    clean = symbol.strip().upper()
    if not clean:
        return None
    table = maps._static
    direct = table.get(clean)
    if direct is not None:
        return direct
    slashed = clean.replace("/", ".")
    if slashed != clean:
        hit = table.get(slashed)
        if hit is not None:
            return hit
    for suffix in _SUFFIXES:
        if clean.endswith(suffix) and len(clean) > len(suffix):
            return table.get(clean[: -len(suffix)])
    return None


def fund_sector_mix(symbol: str | None, maps: SectorMaps | None) -> dict[str, Decimal] | None:
    """The fund's own published sector mix, or None when it publishes none.

    Tries the ticker as stored first, then with a known exchange suffix
    stripped — Canadian wrappers are stored both bare and venue-suffixed
    (``VOO`` / ``VOO.TO``), and the mix belongs to the listing either way.
    An unknown fund answers None: no mix, no estimate, no invented slice.
    The values are ``Decimals`` rebuilt from the row's decimal TEXT, so the
    split is arithmetic-identical to the old literals (docs/02).
    """
    if not symbol or maps is None:
        return None
    clean = symbol.strip().upper()
    if not clean:
        return None
    table = maps._mix
    direct = table.get(clean)
    if direct is not None:
        return dict(direct)
    for suffix in _SUFFIXES:
        if clean.endswith(suffix) and len(clean) > len(suffix):
            hit = table.get(clean[: -len(suffix)])
            if hit is not None:
                return dict(hit)
    return None
