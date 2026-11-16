"""provider_fund_registry — the coverage tables, as DATA (M168, docs/07 §Provider registry).

The provider registries used to be Python constants in
``services/market_data.py`` (the Vanguard US frozenset, the Vanguard CA
ticker → portId dict, the iShares product-URL dict) and
``services/sector_map.py`` (the static ticker → sector map and the static
fund → published-mix map): five tables of hand-verified rows that only a
code change could extend, so a Vanguard Canada listing added after the
seed — or any fund family nobody had hand-listed — simply answered nothing
until somebody edited a literal.

They are rows now. ONE table, keyed ``UNIQUE (family, ticker)``, holding:

* **Provider endpoint registries** — ``vanguard_us`` (no payload; a verified
  row means ``GET /{TICKER}-AdditionalFundData`` answered 200 with a
  parseable ``holdingDetails``), ``vanguard_ca`` (``port_id`` + the Yahoo
  ``venue_suffix`` a fund-leg ticker is stored with), ``ishares_us``
  (``endpoint`` = the product stem ``…/latest-holdings.csv`` is appended to).
* **Read-time classification data** — ``static_sector`` (``sector``) and
  ``fund_sector_mix`` (``sector_mix``), the M166 sector chain's tier 3 and
  tier 4 (docs/16 §Sector chain).

**Tenancy:** there is none, deliberately. These are issuer facts about the
world, identical for every user — the operator-level data AGENTS §2 means
by "except the shared ``fx_rates`` cache", now two shared caches. No
``user_id`` column: a registry row is created once and answers for every
tenant, while ``etf_holdings`` stays strictly per-user (docs/07 §Tenancy).

**Status.** ``verified`` means the row was confirmed by a live 200 with a
parseable payload (the iShares discipline, now automated). ``miss`` is the
NEGATIVE cache — an issuer answered 404 / an empty payload for this ticker —
so the chain never probes the same dead ticker twice (the ``dead_symbol``
lesson, docs/07 §Dead symbols). A ``miss`` row carries ``miss_count`` and
``last_probe_at`` instead of ``verified_at``.

Discovery writes both kinds (``services/provider_registry.py``); nobody
edits a literal to add coverage.
"""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Enum, UniqueConstraint
from sqlmodel import Field

from app.models.mixins import TimestampMixin


class ProviderFundFamily(enum.StrEnum):
    """Which issuer (and which endpoint template) a registry row belongs to.

    One family = one URL template + one parser + one registry family — the
    additive-module pattern docs/07 §Provider registry names for a future
    Schwab/SPDR/Fidelity/TRP product. ``static_sector`` / ``fund_sector_mix``
    are not issuers at all: they are the two hand-curated classification
    tables the M166 sector chain reads at breakdown time, moved into the same
    table so "add a verified row" is the single way coverage grows.
    """

    vanguard_us = "vanguard_us"
    vanguard_ca = "vanguard_ca"
    ishares_us = "ishares_us"
    static_sector = "static_sector"
    fund_sector_mix = "fund_sector_mix"


class ProviderFundStatus(enum.StrEnum):
    """A row's verdict: probed and good, or probed and absent."""

    verified = "verified"
    miss = "miss"


class ProviderFundRegistry(TimestampMixin, table=True):
    __tablename__ = "provider_fund_registry"
    __table_args__ = (
        UniqueConstraint("family", "ticker", name="uq_provider_fund_family_ticker"),
        # A sector or a mix is only ever read off a VERIFIED row, and a miss
        # row never carries endpoint data — the CHECK keeps the two shapes
        # from drifting apart as discovery writes both.
        CheckConstraint(
            "(status = 'verified' AND ticker <> '') OR status = 'miss'",
            name="ck_provider_fund_registry_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    family: ProviderFundFamily = Field(
        sa_column=Column(
            Enum(ProviderFundFamily, native_enum=False, length=32),
            nullable=False,
        )
    )
    # Normalised key: UPPERCASE ticker as the issuer spells it, WITHOUT the
    # Yahoo venue suffix for vanguard_ca (the suffix lives in venue_suffix)
    # and with the provider's own spelling for static_sector (`BRK/B`,
    # `2330.TW`) — lookups normalise exactly the way the old dict lookups did.
    ticker: str = Field(max_length=32)
    status: ProviderFundStatus = Field(
        default=ProviderFundStatus.verified,
        sa_column=Column(
            Enum(ProviderFundStatus, native_enum=False, length=16),
            nullable=False,
            server_default="verified",
        ),
    )
    # vanguard_ca: the portId the gpx GraphQL gateway is keyed by.
    port_id: str | None = Field(default=None, max_length=16)
    # vanguard_ca: `.TO` / `.NE` — what a fund-LEG ticker is stored WITH
    # (`VCN` inside VEQT lands as `VCN.TO`, docs/07).
    venue_suffix: str | None = Field(default=None, max_length=8)
    # ishares_us: the product stem `{stem}/latest-holdings.csv` is built from.
    endpoint: str | None = Field(default=None, max_length=255)
    # static_sector: the GICS sector name (canonical vocabulary).
    sector: str | None = Field(default=None, max_length=64)
    # fund_sector_mix: `{"Technology": "40.90", ...}` — sector -> percent of
    # NAV as a STRING, because JSON has no Decimal and the value must round
    # exactly the way the literal `Decimal("40.90")` did (docs/02: Decimal
    # end-to-end; the loader re-quantizes on read).
    sector_mix: dict | None = Field(default=None, sa_column=Column(JSON))
    # Consecutive issuer verdicts that said "no such fund" (miss rows only).
    miss_count: int = Field(default=0, sa_column=Column(nullable=False, server_default="0"))
    last_probe_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    verified_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    # Who/what wrote the row: `seed:0050`, `discovery:<family>`,
    # `cli:verify`, `cli:set` — the audit trail a re-verification sweep needs.
    verified_by_run: str | None = Field(default=None, max_length=64)

    # Read back as Decimals, never floats (AGENTS §2).
    def mix_decimals(self) -> dict[str, Decimal]:
        """The published fund mix with every percent as a ``Decimal``."""
        raw = self.sector_mix or {}
        return {str(name): Decimal(str(value)) for name, value in raw.items() if value is not None}
