"""
finviz_screener.py
==================
A lightweight numeric **stock screener** backed by Finviz's free screener.

Why this exists
---------------
The news pipeline tells us *what is being talked about*; the screener tells us
*what is moving and how big it is* across the **whole market**, not just tickers
that happen to appear in our feeds. Market cap, price, % change, and volume feed
two things:

  1. a "Screener" dashboard tab (top gainers / losers / most active / unusual
     volume / major-news movers), and
  2. a size/liquidity signal the catalyst ranker can use to down-weight
     mega-cap noise versus a genuine small-cap catalyst.

Approach & honesty
------------------
Finviz's CSV ``export.ashx`` is gated behind paid **Elite**, so we parse the
**free** screener HTML table (the Overview view, ``v=111``). That means:

  * We are scraping a public HTML page — done politely (low frequency, on
    demand, real User-Agent) and degrading gracefully: any HTTP/parse failure
    returns an empty result with a ``status`` string rather than throwing.
  * Free Finviz data is end-of-day / delayed, not real-time. Pre/post-market
    *session* prices are an Elite feature; enrich those later via yfinance if
    needed. What we get reliably: Market Cap, Price, Change %, Volume, Sector.
  * The HTML layout can change. The parser is defensive (skips malformed rows)
    and the column order for ``v=111`` is pinned in ``_OVERVIEW_COLUMNS``.

Everything here is a pure function except ``fetch_screener`` (one HTTP GET per
page), so the parsing is unit-testable without a network.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

log = logging.getLogger("finviz_screener")

_BASE_URL = "https://finviz.com/screener.ashx"

# A real browser UA — Finviz returns a stub/redirect to bare clients.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

_PAGE_SIZE = 20          # Finviz Overview shows 20 rows per page
_MAX_PAGES = 5           # hard cap so we never hammer Finviz (<=100 rows)
_REQUEST_TIMEOUT = 20    # seconds per page

# Column order for the Overview view (v=111), verified against live HTML.
_OVERVIEW_COLUMNS = [
    "no", "ticker", "company", "sector", "industry", "country",
    "market_cap", "pe", "price", "change", "volume",
]

# Friendly preset -> Finviz query params. ``s`` is a signal, ``o`` an ordering.
# Kept small and curated; callers may also pass a raw ``filters`` string.
_PRESETS: dict[str, dict[str, str]] = {
    "top_gainers":    {"s": "ta_topgainers",    "o": "-change"},
    "top_losers":     {"s": "ta_toplosers",     "o": "change"},
    "most_active":    {"s": "ta_mostactive",    "o": "-volume"},
    "unusual_volume": {"s": "ta_unusualvolume", "o": "-change"},
    "most_volatile":  {"s": "ta_mostvolatile",  "o": "-change"},
    "new_high":       {"s": "ta_newhigh",       "o": "-change"},
    "new_low":        {"s": "ta_newlow",        "o": "change"},
    "major_news":     {"s": "n_majornews",      "o": "-change"},
}

DEFAULT_PRESET = "top_gainers"

# --- Value parsers (pure) -------------------------------------------------- #

_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _parse_marketcap(s: str) -> Optional[float]:
    """'333.63M' -> 333630000.0 ; '1.2B' -> 1.2e9 ; '-' -> None."""
    s = (s or "").strip()
    if not s or s == "-":
        return None
    mult = _SUFFIX.get(s[-1].upper())
    try:
        if mult is not None:
            return round(float(s[:-1]) * mult, 2)
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _parse_pct(s: str) -> Optional[float]:
    """'56.70%' -> 56.7 ; '-2.34%' -> -2.34 ; '-' -> None."""
    s = (s or "").strip().rstrip("%")
    if not s or s == "-":
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _parse_float(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _parse_int(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s or s == "-":
        return None
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return None


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ROW_RE = re.compile(r'<tr class="[^"]*styled-row[^"]*"[^>]*>(.*?)</tr>', re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _clean(cell: str) -> str:
    """Strip tags + collapse whitespace + decode the couple entities we see."""
    text = _TAG_RE.sub("", cell)
    text = (
        text.replace("&amp;", "&").replace("&#39;", "'")
        .replace("&quot;", '"').replace("&nbsp;", " ")
    )
    return _WS_RE.sub(" ", text).strip()


def parse_overview(html: str) -> list[dict[str, Any]]:
    """
    Parse the Finviz Overview (v=111) screener HTML into typed row dicts.

    Pure function — defensive: any row whose cell count doesn't match the
    pinned column layout is skipped rather than mis-parsed.
    """
    rows: list[dict[str, Any]] = []
    for row_html in _ROW_RE.findall(html):
        cells = [_clean(c) for c in _CELL_RE.findall(row_html)]
        if len(cells) != len(_OVERVIEW_COLUMNS):
            continue
        raw = dict(zip(_OVERVIEW_COLUMNS, cells))
        ticker = raw["ticker"].upper()
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", ticker):
            continue
        rows.append({
            "ticker": ticker,
            "company": raw["company"],
            "sector": raw["sector"],
            "industry": raw["industry"],
            "country": raw["country"],
            "market_cap": _parse_marketcap(raw["market_cap"]),
            "pe": _parse_float(raw["pe"]),
            "price": _parse_float(raw["price"]),
            "change_pct": _parse_pct(raw["change"]),
            "volume": _parse_int(raw["volume"]),
        })
    return rows


# --- Network --------------------------------------------------------------- #

async def _fetch_page(session: Any, params: dict[str, str]) -> Optional[str]:
    """One screener page; returns HTML or None on any failure."""
    try:
        async with session.get(
            _BASE_URL, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT
        ) as resp:
            if resp.status != 200:
                log.warning("finviz page HTTP %s for %s", resp.status, params)
                return None
            return await resp.text()
    except Exception as exc:  # noqa: BLE001
        log.warning("finviz fetch failed (%s): %s", params, type(exc).__name__)
        return None


async def fetch_screener(
    *,
    preset: str = DEFAULT_PRESET,
    filters: Optional[str] = None,
    limit: int = 30,
    session: Any = None,
) -> dict[str, Any]:
    """
    Fetch a screen of up to ``limit`` rows for a named ``preset``.

    Returns a dict: ``{rows, count, preset, status}``. ``status`` is None on
    success or a short reason string when we fall back to an empty list (so the
    API/UI can show *why* instead of a silent blank). Caller owns no aiohttp
    knowledge: pass an existing ``session`` or we create a throwaway one.
    """
    preset = preset if preset in _PRESETS else DEFAULT_PRESET
    base_params = {"v": "111", **_PRESETS[preset]}
    if filters:
        base_params["f"] = filters

    limit = max(1, min(limit, _MAX_PAGES * _PAGE_SIZE))
    n_pages = (limit + _PAGE_SIZE - 1) // _PAGE_SIZE

    try:
        import aiohttp
    except ImportError:
        return {"rows": [], "count": 0, "preset": preset,
                "status": "aiohttp not installed"}

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    rows: list[dict[str, Any]] = []
    status: Optional[str] = None
    try:
        for page in range(n_pages):
            params = dict(base_params)
            if page:
                params["r"] = str(page * _PAGE_SIZE + 1)  # Finviz row offset (1-based)
            html = await _fetch_page(session, params)
            if html is None:
                if not rows:
                    status = "finviz unreachable or blocked (no rows)"
                break
            page_rows = parse_overview(html)
            if not page_rows:
                if not rows and page == 0:
                    status = "finviz returned no parseable rows (layout changed?)"
                break
            rows.extend(page_rows)
            if len(rows) >= limit:
                break
            if page < n_pages - 1:
                await asyncio.sleep(0.4)  # be polite between pages
    finally:
        if owns_session:
            await session.close()

    rows = rows[:limit]
    return {"rows": rows, "count": len(rows), "preset": preset, "status": status}


def available_presets() -> list[str]:
    """Preset keys a caller may request (for the API/UI to enumerate)."""
    return list(_PRESETS.keys())


async def fetch_market_caps(
    tickers: list[str], *, session: Any = None
) -> dict[str, float]:
    """
    Return ``{ticker: market_cap_usd}`` for a specific list of tickers via one
    Finviz screen (the ``t=`` ticker filter), paginating if needed.

    Used by the catalyst ranker to size-adjust its scoring. Tickers Finviz
    doesn't recognise are simply absent from the result; on any HTTP/parse
    failure this returns ``{}`` so the caller degrades to size-neutral scoring
    rather than breaking.
    """
    syms = [t.strip().upper() for t in dict.fromkeys(tickers) if t and t.strip()]
    if not syms:
        return {}
    syms = syms[:_MAX_PAGES * _PAGE_SIZE]  # bound the request
    base_params = {"v": "111", "t": ",".join(syms)}

    try:
        import aiohttp
    except ImportError:
        return {}

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    caps: dict[str, float] = {}
    try:
        n_pages = (len(syms) + _PAGE_SIZE - 1) // _PAGE_SIZE
        for page in range(min(n_pages, _MAX_PAGES)):
            params = dict(base_params)
            if page:
                params["r"] = str(page * _PAGE_SIZE + 1)
            html = await _fetch_page(session, params)
            if html is None:
                break
            rows = parse_overview(html)
            if not rows:
                break
            for row in rows:
                if row.get("market_cap") is not None:
                    caps[row["ticker"]] = row["market_cap"]
            if len(caps) >= len(syms):
                break
            if page < n_pages - 1:
                await asyncio.sleep(0.4)
    finally:
        if owns_session:
            await session.close()

    return caps
