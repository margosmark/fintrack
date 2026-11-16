"""Static company → country map (M147, docs/05 §Exposure fallback chain).

Last-resort geography for composition rows and fundamentals that arrived
WITHOUT a country: the deterministic middle step between "the provider
said it" and `Unclassified`. A shipped lookup, never an LLM call — the
exposure buckets must be reproducible from the cache alone.

The keys are the tickers providers actually return (Yahoo bare symbols
like ``NVDA``; exchange-suffixed forms like ``RY.TO`` / ``ASML.AS``).
``country_for_symbol`` matches exactly first, then retries without a
known exchange suffix, so one entry answers both spellings. Values use
the same country names yfinance writes into ``fundamentals.country``
(``United States``, ``Canada``, ``South Korea`` …) so two rows for the
same country never split the pie.

Deliberately incomplete: a name missing here stays Unclassified rather
than being guessed — coverage grows by adding verified rows, never by
inferring from the ticker text.
"""

from __future__ import annotations

# Exchange suffixes stripped before the fallback match (the Yahoo/ISO
# venue suffixes that show up in issuer CSVs and international rows).
_SUFFIXES = (
    ".TO",
    ".NE",
    ".V",
    ".QC",
    ".AS",
    ".L",
    ".SW",
    ".T",
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
)

COUNTRY_BY_SYMBOL: dict[str, str] = {
    # --- United States — the mega-caps every US fund's top slice names ------
    "AAPL": "United States",
    "MSFT": "United States",
    "NVDA": "United States",
    "GOOGL": "United States",
    "GOOG": "United States",
    "AMZN": "United States",
    "META": "United States",
    "TSLA": "United States",
    "AVGO": "United States",
    "BRK.B": "United States",
    "BRK-B": "United States",
    "JPM": "United States",
    "V": "United States",
    "MA": "United States",
    "LLY": "United States",
    "XOM": "United States",
    "UNH": "United States",
    "ORCL": "United States",
    "COST": "United States",
    "JNJ": "United States",
    "HD": "United States",
    "WMT": "United States",
    "PG": "United States",
    "NFLX": "United States",
    "AMD": "United States",
    "INTC": "United States",
    "KO": "United States",
    "MRK": "United States",
    "ABBV": "United States",
    "PEP": "United States",
    "TMO": "United States",
    "CRM": "United States",
    "ACN": "United States",
    "CSCO": "United States",
    "MCD": "United States",
    "IBM": "United States",
    "GE": "United States",
    "INTU": "United States",
    "QCOM": "United States",
    "TXN": "United States",
    "CAT": "United States",
    "BA": "United States",
    "GS": "United States",
    "MS": "United States",
    "VZ": "United States",
    "T": "United States",
    "CVX": "United States",
    "COP": "United States",
    "DIS": "United States",
    "CMCSA": "United States",
    "SBUX": "United States",
    "NKE": "United States",
    "PFE": "United States",
    "ABT": "United States",
    "DHR": "United States",
    "LIN": "United States",
    "AMGN": "United States",
    "ISRG": "United States",
    "ADP": "United States",
    "MU": "United States",
    "AMAT": "United States",
    "LRCX": "United States",
    "KLAC": "United States",
    "SNPS": "United States",
    "CDNS": "United States",
    "PANW": "United States",
    "CRWD": "United States",
    "UBER": "United States",
    "ABNB": "United States",
    "BKNG": "United States",
    "GILD": "United States",
    "MDLZ": "United States",
    "VRTX": "United States",
    "HON": "United States",
    "LOW": "United States",
    # --- Canada — the names every Canadian fund's top slice names -----------
    "RY": "Canada",
    "RY.TO": "Canada",
    "TD": "Canada",
    "TD.TO": "Canada",
    "BMO": "Canada",
    "BMO.TO": "Canada",
    "BNS": "Canada",
    "BNS.TO": "Canada",
    "CM": "Canada",
    "CM.TO": "Canada",
    "NA": "Canada",
    "NA.TO": "Canada",
    "MFC": "Canada",
    "MFC.TO": "Canada",
    "SLF": "Canada",
    "SLF.TO": "Canada",
    "ENB": "Canada",
    "ENB.TO": "Canada",
    "CNQ": "Canada",
    "CNQ.TO": "Canada",
    "SU": "Canada",
    "SU.TO": "Canada",
    "CVE": "Canada",
    "CVE.TO": "Canada",
    "CNR": "Canada",
    "CNR.TO": "Canada",
    "CP": "Canada",
    "CP.TO": "Canada",
    "TRP": "Canada",
    "TRP.TO": "Canada",
    "POW": "Canada",
    "POW.TO": "Canada",
    "FFH": "Canada",
    "FFH.TO": "Canada",
    "BN": "Canada",
    "BN.TO": "Canada",
    "BAM": "Canada",
    "BAM.TO": "Canada",
    "SHOP": "Canada",
    "SHOP.TO": "Canada",
    "CSU": "Canada",
    "CSU.TO": "Canada",
    "WCN": "Canada",
    "WCN.TO": "Canada",
    "ATD": "Canada",
    "ATD.TO": "Canada",
    "L": "Canada",
    "L.TO": "Canada",
    "NTR": "Canada",
    "NTR.TO": "Canada",
    "ABX": "Canada",
    "ABX.TO": "Canada",
    "IMG": "Canada",
    "AEM": "Canada",
    "AEM.TO": "Canada",
    "WSP": "Canada",
    "WSP.TO": "Canada",
    "BIRK": "Canada",
    "PPL": "Canada",
    "PPL.TO": "Canada",
    "IAG": "Canada",
    "IAG.TO": "Canada",
    "ECN": "Canada",
    "SES": "Canada",
    "CCL.B": "Canada",
    "CCL-B.TO": "Canada",
    # --- The rest of the world — non-US names in international funds -------
    "TSM": "Taiwan",
    "2330.TW": "Taiwan",
    "005930.KS": "South Korea",
    "005930.KQ": "South Korea",
    "000660.KS": "South Korea",
    "000660.KQ": "South Korea",
    "ASML": "Netherlands",
    "ASML.AS": "Netherlands",
    "ING.AS": "Netherlands",
    "0700.HK": "China",
    "0992.HK": "China",
    "HSBA": "United Kingdom",
    "HSBA.L": "United Kingdom",
    "SHEL": "United Kingdom",
    "SHEL.L": "United Kingdom",
    "AZN": "United Kingdom",
    "AZN.L": "United Kingdom",
    "ULVR.L": "United Kingdom",
    "NOVN.SW": "Switzerland",
    "ROG.SW": "Switzerland",
    "NESN.SW": "Switzerland",
    "SAP": "Germany",
    "SAP.DE": "Germany",
    "SIE.DE": "Germany",
    "ALV.DE": "Germany",
    "MC.PA": "France",
    "OR.PA": "France",
    "SAN.PA": "France",
    "CAP.PA": "France",
    "AC.PA": "France",
    "7203.T": "Japan",
    "8306.T": "Japan",
    "9984.T": "Japan",
    "NVO": "Denmark",
    "NOVO-B.CO": "Denmark",
    "ISP.MI": "Italy",
    "SAN.MC": "Spain",
    "BHP": "Australia",
    "BHP.AX": "Australia",
    "RIO": "Australia",
    "RIO.AX": "Australia",
    "CBA.AX": "Australia",
    "BABA": "China",
    "PDD": "China",
    "NTES": "China",
    "JD": "China",
    "BIDU": "China",
    "TSM.A": "Taiwan",
    "MELI": "United States",
    "SE": "United States",
    "PBR": "United States",
    "SONY": "United States",
    "UBS": "United States",
}


def country_for_symbol(symbol: str | None) -> str | None:
    """Country for a composition/fundamentals ticker, or None.

    Exact match first (providers that already store ``RY.TO`` /
    ``ASML.AS`` answer directly), then the bare stem against the same
    map. Unknown tickers are None — a wrong country would corrupt the
    geography pie, an absent one only leaves the row Unclassified.
    """
    if not symbol:
        return None
    clean = symbol.strip().upper()
    if not clean:
        return None
    direct = COUNTRY_BY_SYMBOL.get(clean)
    if direct is not None:
        return direct
    for suffix in _SUFFIXES:
        if clean.endswith(suffix) and len(clean) > len(suffix):
            return COUNTRY_BY_SYMBOL.get(clean[: -len(suffix)])
    return None


# --- ISO3166 alpha-2 -> country name (M147-followup, docs/07) ----------------
#
# The Vanguard payloads never say "Taiwan" — the US profile JSON only carries
# an ISIN (whose first two characters are the issuer's ISO3166 country) and
# the Canadian gateway carries `bloombergIsoCountry` (the same alpha-2 code).
# One table answers both so a pie slice cannot split by provider spelling:
# values MUST match the names yfinance writes into `fundamentals.country` and
# the ticker map above.
#
# Deliberate gaps — an unmapped code leaves the row to the docs/05 chain
# (fundamentals -> ticker map -> `Unclassified`) rather than asserting
# something the payload does not say:
#   * `XS` / `EU` are ISIN ISSUING categories, not places (`XS` = international
#     issues, `EU` = EU-institution bonds);
#   * offshore incorporation domiciles (`KY`, `BM`, `JE`, `GG`, `IM`, `MU`,
#     `AN`, `LR`, `VG`, `MH`, `SC`, `CW`, `BZ`, `SX`, `VI`) are where the
#     ISSUER is incorporated, not where the business operates — Alibaba's
#     ISIN starts `KY`, and labelling that slice "Cayman Islands" would be a
#     geography claim we cannot support from this payload. Worth ~3% of VXUS
#     (the measured cost of leaving it Unclassified, LIVE 2026-10-11).
# Coverage grows by adding codes seen live, never by inferring one from text.
COUNTRY_BY_ISO2: dict[str, str] = {
    "AE": "United Arab Emirates",
    "AT": "Austria",
    "AU": "Australia",
    "BE": "Belgium",
    "BH": "Bahrain",
    "BR": "Brazil",
    "CA": "Canada",
    "CH": "Switzerland",
    "CL": "Chile",
    "CN": "China",
    "CO": "Colombia",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "DE": "Germany",
    "DK": "Denmark",
    "EE": "Estonia",
    "EG": "Egypt",
    "ES": "Spain",
    "FI": "Finland",
    "FO": "Faroe Islands",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "HK": "Hong Kong",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IS": "Iceland",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "South Korea",
    "KW": "Kuwait",
    "LI": "Liechtenstein",
    "LU": "Luxembourg",
    "MT": "Malta",
    "MX": "Mexico",
    "MY": "Malaysia",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "PA": "Panama",
    "PE": "Peru",
    "PH": "Philippines",
    "PL": "Poland",
    "PT": "Portugal",
    "PR": "Puerto Rico",
    "QA": "Qatar",
    "RO": "Romania",
    "SA": "Saudi Arabia",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "TH": "Thailand",
    "TR": "Turkey",
    "TW": "Taiwan",
    "US": "United States",
    "ZA": "South Africa",
}


def country_for_iso2(code: str | None) -> str | None:
    """Country name for an ISO3166 alpha-2 code, or None.

    Unknown codes, the non-geographic ISIN prefixes and every offshore
    domicile above answer None — a wrong country corrupts the geography pie,
    an absent one only leaves the row Unclassified (same rule as
    ``country_for_symbol``).
    """
    if not code:
        return None
    clean = code.strip().upper()
    if len(clean) != 2 or not clean.isalpha():
        return None
    return COUNTRY_BY_ISO2.get(clean)
