"""
ticker_extractor.py
===================
Extracts stock ticker symbols from financial news text.

Two-pass approach:
  1. Pattern matching  — catches $AAPL, (AAPL), NYSE: AAPL  (high precision)
  2. Company name map  — maps ~80 major company names to their tickers

Returns a sorted, deduplicated list of ticker strings.
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_DOLLAR_PATTERN   = re.compile(r'\$([A-Z]{1,5})\b')
_PAREN_PATTERN    = re.compile(r'\(([A-Z]{1,5})\)')
_EXCHANGE_PATTERN = re.compile(r'(?:NYSE|NASDAQ|AMEX):\s*([A-Z]{1,5})\b')

# Words that look like tickers but are not — filtered out after extraction.
_FALSE_POSITIVES: frozenset[str] = frozenset({
    # Abbreviations / acronyms common in financial text
    "THE", "AND", "FOR", "ARE", "NOT", "NEW", "ALL", "INC", "LLC", "LTD",
    "PLC", "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED",
    "GDP", "CPI", "USD", "EUR", "GBP", "JPY", "YOY", "QOQ", "MOM",
    "ESG", "AI", "ML", "EV", "API", "ATH", "ATL", "YTD", "OTC",
    # Exchange / market names
    "NYSE", "NASDAQ", "AMEX", "LSE", "TSX",
    # Common financial terms that appear uppercased
    "HOLD", "SELL", "BUY", "RATE", "BOND", "DEBT", "CASH", "RISK",
    "LOSS", "GAIN", "FUND", "RATE", "NOTE", "BILL", "SWAP",
})

# ---------------------------------------------------------------------------
# Company name → ticker map
# ---------------------------------------------------------------------------

_COMPANY_TICKERS: dict[str, str] = {
    # Big Tech / MAMAA
    "apple":                    "AAPL",
    "microsoft":                "MSFT",
    "amazon":                   "AMZN",
    "alphabet":                 "GOOGL",
    "google":                   "GOOGL",
    "meta":                     "META",
    "netflix":                  "NFLX",
    "nvidia":                   "NVDA",
    "amd":                      "AMD",
    "intel":                    "INTC",
    "qualcomm":                 "QCOM",
    "broadcom":                 "AVGO",
    "tsmc":                     "TSM",
    "taiwan semiconductor":     "TSM",
    "arm holdings":             "ARM",
    # Finance
    "jpmorgan":                 "JPM",
    "jp morgan":                "JPM",
    "goldman sachs":            "GS",
    "morgan stanley":           "MS",
    "bank of america":          "BAC",
    "wells fargo":              "WFC",
    "citigroup":                "C",
    "berkshire hathaway":       "BRK-B",
    "blackrock":                "BLK",
    "charles schwab":           "SCHW",
    "american express":         "AXP",
    "visa":                     "V",
    "mastercard":               "MA",
    # EV / Auto
    "tesla":                    "TSLA",
    "ford":                     "F",
    "general motors":           "GM",
    "toyota":                   "TM",
    "rivian":                   "RIVN",
    "lucid":                    "LCID",
    # Pharma / Biotech
    "pfizer":                   "PFE",
    "moderna":                  "MRNA",
    "johnson & johnson":        "JNJ",
    "johnson and johnson":      "JNJ",
    "eli lilly":                "LLY",
    "merck":                    "MRK",
    "abbvie":                   "ABBV",
    "bristol-myers":            "BMY",
    "bristol myers":            "BMY",
    "astrazeneca":              "AZN",
    "novartis":                 "NVS",
    "regeneron":                "REGN",
    "gilead":                   "GILD",
    # Energy
    "exxonmobil":               "XOM",
    "exxon":                    "XOM",
    "chevron":                  "CVX",
    "conocophillips":           "COP",
    "shell":                    "SHEL",
    # Retail / Consumer
    "walmart":                  "WMT",
    "target":                   "TGT",
    "home depot":               "HD",
    "costco":                   "COST",
    "starbucks":                "SBUX",
    "mcdonald's":               "MCD",
    "mcdonalds":                "MCD",
    "nike":                     "NKE",
    # Telecom
    "at&t":                     "T",
    "verizon":                  "VZ",
    "t-mobile":                 "TMUS",
    # Aerospace / Defense
    "boeing":                   "BA",
    "lockheed martin":          "LMT",
    "raytheon":                 "RTX",
    "northrop grumman":         "NOC",
    # Crypto-adjacent
    "coinbase":                 "COIN",
    "microstrategy":            "MSTR",
    "robinhood":                "HOOD",
    # Software / Cloud / SaaS
    "salesforce":               "CRM",
    "oracle":                   "ORCL",
    "ibm":                      "IBM",
    "adobe":                    "ADBE",
    "servicenow":               "NOW",
    "workday":                  "WDAY",
    "snowflake":                "SNOW",
    "palantir":                 "PLTR",
    "crowdstrike":              "CRWD",
    "datadog":                  "DDOG",
    # Payments / Fintech
    "paypal":                   "PYPL",
    "block":                    "SQ",
    "square":                   "SQ",
    "stripe":                   "STRP",
    # Consumer / Entertainment
    "disney":                   "DIS",
    "comcast":                  "CMCSA",
    "spotify":                  "SPOT",
    "uber":                     "UBER",
    "lyft":                     "LYFT",
    "airbnb":                   "ABNB",
    "shopify":                  "SHOP",
    "zoom":                     "ZM",
}


# ---------------------------------------------------------------------------
# Extractor class
# ---------------------------------------------------------------------------

class TickerExtractor:
    """
    Extracts stock tickers from news title + description text.

    Parameters
    ----------
    extra_mappings:
        Optional additional {company_name: ticker} pairs to merge with the
        built-in dictionary.
    """

    def __init__(self, extra_mappings: Optional[dict[str, str]] = None) -> None:
        self._mappings: dict[str, str] = dict(_COMPANY_TICKERS)
        if extra_mappings:
            self._mappings.update({k.lower(): v for k, v in extra_mappings.items()})

    def extract(self, title: str, description: str) -> tuple[str, ...]:
        """Return a sorted tuple of unique ticker symbols found in the text."""
        text = f"{title} {description}"
        found: set[str] = set()

        # Pass 1 — explicit patterns ($TICKER, (TICKER), NYSE: TICKER)
        for pattern in (_DOLLAR_PATTERN, _PAREN_PATTERN, _EXCHANGE_PATTERN):
            for m in pattern.finditer(text):
                found.add(m.group(1).upper())

        # Pass 2 — company name lookup (word-boundary matched, case-insensitive)
        text_lower = text.lower()
        for name, ticker in self._mappings.items():
            if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
                found.add(ticker)

        found -= _FALSE_POSITIVES
        return tuple(sorted(found))
