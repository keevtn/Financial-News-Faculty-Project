"""
finviz_elite.py
===============
Finviz **Elite** screener via the *authorized* CSV export (``export.ashx``).

Unlike the old free-page scraper (removed because it impersonated a browser to
get past Cloudflare), this uses your Elite account's **export API** with your
``FINVIZ_AUTH_TOKEN`` — the sanctioned programmatic channel. No scraping, no
impersonation; just an authenticated HTTP GET that returns CSV.

Active only when ``FINVIZ_AUTH_TOKEN`` is set; the screener route falls back to
the Yahoo source (``market_screener``) when it isn't, or when a call fails.

Same public surface as ``market_screener`` so the route can swap sources:
  - ``has_token()``
  - ``available_presets()`` / ``preset_labels()``
  - ``fetch_screener(preset, limit, ...)`` -> {rows, count, preset, status, source}

Compliance: Elite authorizes *your* access. Keep the deployment private /
advisor-facing (its license is single-user; don't publicly redistribute the
data). The token is a paid-account secret — env var only, never committed.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
from typing import Any, Optional

log = logging.getLogger("finviz_elite")

_BASE_URL = "https://elite.finviz.com/export.ashx"
_VIEW = "111"  # Overview columns
_REQUEST_TIMEOUT = 20

# friendly id -> (Finviz signal `s=`, order `o=`, human label)
_PRESETS: dict[str, tuple[str, str, str]] = {
    "top_gainers":    ("ta_topgainers",    "-change", "Top Gainers"),
    "top_losers":     ("ta_toplosers",     "change",  "Top Losers"),
    "most_active":    ("ta_mostactive",    "-volume", "Most Active"),
    "unusual_volume": ("ta_unusualvolume", "-change", "Unusual Volume"),
    "most_volatile":  ("ta_mostvolatile",  "-change", "Most Volatile"),
    "new_high":       ("ta_newhigh",       "-change", "New High"),
    "new_low":        ("ta_newlow",        "change",  "New Low"),
    "major_news":     ("n_majornews",      "-change", "Major News"),
}
DEFAULT_PRESET = "top_gainers"

_HEADERS = {
    "User-Agent": "FinancialNewsDashboard/1.0 (research/non-commercial)",
    "Accept": "text/csv, */*",
}


def has_token() -> bool:
    return bool(os.environ.get("FINVIZ_AUTH_TOKEN"))


def available_presets() -> list[str]:
    return list(_PRESETS.keys())


def preset_labels() -> dict[str, str]:
    return {k: v[2] for k, v in _PRESETS.items()}


# --- value parsers (CSV export) -------------------------------------------- #

_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _num(s: Any) -> Optional[float]:
    """Float from an export cell. Handles raw numbers, K/M/B/T suffixes, commas."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if not t or t in ("-", "N/A"):
        return None
    mult = _SUFFIX.get(t[-1].upper())
    try:
        return round(float(t[:-1]) * mult, 2) if mult is not None else float(t)
    except ValueError:
        return None


def _pct(s: Any) -> Optional[float]:
    """Percent change. Finviz export gives a percentage (often with a % sign)."""
    if s is None:
        return None
    t = str(s).strip().rstrip("%")
    if not t or t in ("-", "N/A"):
        return None
    try:
        return round(float(t), 2)
    except ValueError:
        return None


def _int(s: Any) -> Optional[int]:
    f = _num(s)
    return int(f) if f is not None else None


def _row_from_csv(d: dict[str, str]) -> Optional[dict[str, Any]]:
    """Map an export CSV row (DictReader) to the ScreenerRow shape."""
    ticker = (d.get("Ticker") or "").strip().upper()
    if not ticker:
        return None
    return {
        "ticker": ticker,
        "company": (d.get("Company") or "").strip(),
        "sector": (d.get("Sector") or "").strip(),
        "industry": (d.get("Industry") or "").strip(),
        "country": (d.get("Country") or "").strip(),
        "market_cap": _num(d.get("Market Cap")),
        "pe": _num(d.get("P/E")),
        "price": _num(d.get("Price")),
        "change_pct": _pct(d.get("Change")),
        "volume": _int(d.get("Volume")),
    }


def _parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    for d in reader:
        row = _row_from_csv(d)
        if row is not None:
            rows.append(row)
    return rows


# --- fetch ----------------------------------------------------------------- #

async def fetch_screener(
    *,
    preset: str = DEFAULT_PRESET,
    filters: Optional[str] = None,
    limit: int = 30,
    session: Any = None,
) -> dict[str, Any]:
    """Run an Elite export screen. Returns {rows, count, preset, status, source}."""
    preset = preset if preset in _PRESETS else DEFAULT_PRESET
    signal, order, _label = _PRESETS[preset]
    limit = max(1, min(limit, 100))

    token = os.environ.get("FINVIZ_AUTH_TOKEN")
    if not token:
        return {"rows": [], "count": 0, "preset": preset,
                "status": "FINVIZ_AUTH_TOKEN not set", "source": "finviz_elite"}

    params = {"v": _VIEW, "s": signal, "o": order, "auth": token}
    if filters:
        params["f"] = filters

    try:
        import aiohttp
    except ImportError:
        return {"rows": [], "count": 0, "preset": preset,
                "status": "aiohttp not installed", "source": "finviz_elite"}

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(headers=_HEADERS)
    try:
        async with session.get(_BASE_URL, params=params, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("finviz elite export HTTP %s", resp.status)
                return {"rows": [], "count": 0, "preset": preset,
                        "status": f"finviz elite HTTP {resp.status}", "source": "finviz_elite"}
            text = await resp.text()
    except Exception as exc:  # noqa: BLE001
        log.warning("finviz elite export failed: %s", type(exc).__name__)
        return {"rows": [], "count": 0, "preset": preset,
                "status": f"finviz elite error: {type(exc).__name__}", "source": "finviz_elite"}
    finally:
        if owns_session:
            await session.close()

    # A paywall/login HTML page instead of CSV means a bad/expired token.
    if "<html" in text[:200].lower():
        return {"rows": [], "count": 0, "preset": preset,
                "status": "finviz elite auth rejected (check FINVIZ_AUTH_TOKEN)",
                "source": "finviz_elite"}

    rows = _parse_csv(text)[:limit]
    return {
        "rows": rows, "count": len(rows), "preset": preset,
        "status": None if rows else "finviz elite returned no rows",
        "source": "finviz_elite",
    }


async def fetch_market_caps(tickers: list[str], *, session: Any = None) -> dict[str, float]:
    """Market caps for specific tickers via the Elite export ``t=`` filter."""
    token = os.environ.get("FINVIZ_AUTH_TOKEN")
    syms = [t.strip().upper() for t in dict.fromkeys(tickers) if t and t.strip()]
    if not token or not syms:
        return {}
    params = {"v": _VIEW, "t": ",".join(syms[:100]), "auth": token}
    try:
        import aiohttp
    except ImportError:
        return {}
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(headers=_HEADERS)
    try:
        async with session.get(_BASE_URL, params=params, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return {}
            text = await resp.text()
    except Exception:  # noqa: BLE001
        return {}
    finally:
        if owns_session:
            await session.close()
    out: dict[str, float] = {}
    for row in _parse_csv(text):
        if row.get("market_cap") is not None:
            out[row["ticker"]] = row["market_cap"]
    return out
