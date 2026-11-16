"""Venue-code aliases for `securities.exchange` (M164, docs/07 §Venue aliases).

`securities.exchange` holds a VENUE CODE, and `uq_securities_symbol UNIQUE
(user_id, symbol, exchange)` (docs/02) means a row that spells the same venue
differently is a SECOND row for ONE listing: it splits that listing's prices,
trades and ACB pool across two ids, and — because
``market_data.security_symbol_index`` counts two rows sharing a stem as two
LISTINGS — every fund leg spelled with the bare ticker stays unlinked behind an
"ambiguous" stem that is not ambiguous at all (prod 2026-10-11: tenant 343's
``600900`` legs, ``PNDORA`` legs).

This module owns THE alias table. Two halves of the fix read it and nothing
else does:

* ``market_data._normalize_exchange`` canonicalizes the VENUE CODE on the way
  in, so a provider payload can never write a second spelling of a venue that
  is already stored (the same seam that has mapped Yahoo's Canadian MICs to
  ``TSX`` since M142);
* ``security_dedup`` groups by the canonical code when it decides "two rows,
  one listing", so rows written BEFORE this table existed merge with the same
  keeper rule, move/repoint machinery and ACB refusal it has always used.

`security_symbol_index` deliberately does NOT read this table: it must never
guess a listing, and after the repair there is one row per listing to find.

## A pair enters this table only with same-listing evidence

LIVE-PROBED 2026-10-11 from the api container — for each alias, the provider
answers the SAME LISTING under both spellings:

* ``SHSE`` → ``SHH`` (Shanghai): ``600900.SS`` answers SHH "Shanghai", and
  prod row 2894 ``603986.SS`` was stored under SHSE by the same provider;
* ``OCSE`` → ``CPH`` (Copenhagen): ``PNDORA.CO`` answers CPH "Copenhagen",
  while prod row 1721 stores OCSE;
* ``TOKY1`` → ``JPX`` (Tokyo): bare ``7182`` answers TOKY1 "Japan Post Bank Co
  Ltd", ``7182.T`` answers JPX "Tokyo" "JAPAN POST BANK CO LTD" — one listing;
* ``JASDQ`` → ``JPX`` (Tokyo): bare ``2454`` answers JASDQ "All About Inc",
  ``2454.T`` answers JPX "Tokyo" "ALL ABOUT INC";
* ``HONGK`` → ``HKG`` (Hong Kong): bare ``1299`` answers HONGK "AIA Group
  Ltd", ``1299.HK`` answers HKG "HKSE" "AIA".

The Canadian ``TSX`` family (``TOR``/``TSE``/``XTSE``/``XTSX``/``TSXV``) is the
same table's oldest entries — M142 wrote them straight into
``_normalize_exchange``; they move here unchanged (``TSXV`` → ``TSX`` is the
app's Canadian convention: a Venture issuer never carries a main-board symbol).

## What must NEVER be aliased — the falsifying probes

The table is per PAIR of venue codes and is never derived from a symbol, because
one symbol stem can name different companies on different venues. Probed the
same day, from the same container:

* ``9503`` bare → SAUDI "Baazeem Trading Co" while ``9503.T`` → JPX "KANSAI
  ELECTRIC POWER CO INC" — two companies, one numeric stem. ``SAUDI`` and
  ``JPX`` are therefore NOT aliases and no symbol-wide rule can ever be right;
* ``2330`` → JSD "For-side.com Co Ltd" while ``2330.TW`` → TAI "TSMC";
* ``ROP`` → NMS "Roper Technologies, Inc." while ``ROP.SW`` → EBS "ROCHE PS";
* ``VIU``/``VIU.TO``, ``VUN``/``VUN.TO``, ``XBB``/``XBB.TO``, ``BN``/`BN.TO`,
  ``SHEL``/``SHEL.L`` (prod) — a US-listed namesake of a Canadian/UK listing
  (the CSV import's twin rule, docs/05);
* ``SHOP`` NYSE vs ``SHOP`` TSX — the card's own danger case;
* the Nasdaq TIERS the provider names separately — ``NMS`` "NasdaqGS",
  ``NGM`` "NasdaqGM", ``NCM`` "NasdaqCM", ``NAS`` "Nasdaq" — stay distinct: if
  the provider tells them apart, so do we;
* ``CHIX`` is NOT ruled: its prod rows probe to ``CXE`` "Cboe UK", but the code
  also names Chi-X Canada. A pair with region-ambiguous evidence stays out.

Unknown codes pass through untouched — the worst an unlisted venue can do is
not be canonicalized, which is exactly what happened before this table.
"""

from __future__ import annotations

#: alias → the one spelling this app stores for that venue. Keys are already
#: uppercase; ``canonical_exchange`` applies the strip/upper.
VENUE_ALIASES: dict[str, str] = {
    # Canadian TSX-family MICs — M142's convention, moved here unchanged.
    "TOR": "TSX",
    "TSE": "TSX",
    "XTSE": "TSX",
    "XTSX": "TSX",
    "TSXV": "TSX",
    # M164: provider spelling drift, each pair live-probed (module docstring).
    "SHSE": "SHH",  # Shanghai Stock Exchange
    "OCSE": "CPH",  # Nasdaq Copenhagen
    "TOKY1": "JPX",  # Tokyo Stock Exchange (bare-symbol microsite code)
    "JASDQ": "JPX",  # Tokyo Stock Exchange (JASDAQ merged into it, 2022)
    "HONGK": "HKG",  # Hong Kong Exchanges
}


def canonical_exchange(exchange: str | None) -> str:
    """The one spelling this app stores for the venue ``exchange`` names.

    ``strip().upper()`` first (the ``uq_securities_symbol`` normalization rule,
    docs/02), then the alias table. A code the table does not know is returned
    as normalized — never rejected, never guessed at.
    """
    clean = (exchange or "").strip().upper()
    return VENUE_ALIASES.get(clean, clean)
