"""
market_screener.py
==================
Numeric stock screener backed by **Yahoo Finance's predefined screens** via the
``yfinance`` library.

This replaces the earlier Finviz HTML scraper. Two reasons:
  1. **No gray area** — we use yfinance against Yahoo's documented screener
     endpoint (yfinance handles the crumb/cookie), rather than scraping Finviz's
     free page behind a browser-TLS impersonation to dodge their Cloudflare
     bot-block. No circumvention of a deliberate access control.
  2. **Works from datacenter IPs** — Finviz blocks Render's IP range outright;
     Yahoo does not, so the Screener tab works on the hosted deployment too.

Public surface (unchanged, so callers don't care about the source):
  - ``available_presets()``                  -> list[str]
  - ``preset_labels()``                       -> {id: human label}
  - ``fetch_screener(preset, limit, ...)``    -> {rows, count, preset, status}
  - ``fetch_market_caps(tickers)``            -> {ticker: market_cap_usd}

yfinance is synchronous, so the async wrappers run it in a worker thread.
Everything degrades gracefully: any failure yields empty rows + a ``status``
string rather than raising.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger("market_screener")

# friendly id -> (Yahoo predefined screen id, human label). Curated to equity
# movers + small-cap screens that suit a catalyst dashboard.
_PRESETS: dict[str, tuple[str, str]] = {
    "top_gainers":        ("day_gainers", "Top Gainers"),
    "top_losers":         ("day_losers", "Top Losers"),
    "most_active":        ("most_actives", "Most Active"),
    "small_cap_gainers":  ("small_cap_gainers", "Small-Cap Gainers"),
    "aggressive_small":   ("aggressive_small_caps", "Aggressive Small Caps"),
    "growth_tech":        ("growth_technology_stocks", "Growth Tech"),
    "undervalued_growth": ("undervalued_growth_stocks", "Undervalued Growth"),
    "most_shorted":       ("most_shorted_stocks", "Most Shorted"),
}
DEFAULT_PRESET = "top_gainers"


def available_presets() -> list[str]:
    return list(_PRESETS.keys())


def preset_labels() -> dict[str, str]:
    return {k: v[1] for k, v in _PRESETS.items()}


# --- value coercion -------------------------------------------------------- #

def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _round(v: Any) -> Optional[float]:
    f = _f(v)
    return round(f, 2) if f is not None else None


def _row_from_quote(q: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map a Yahoo screener quote to the ScreenerRow shape the frontend expects."""
    sym = (q.get("symbol") or "").upper()
    if not sym:
        return None
    return {
        "ticker": sym,
        "company": q.get("shortName") or q.get("longName") or "",
        "sector": q.get("sector") or "",            # not in screener payload -> ""
        "industry": q.get("industry") or "",
        "country": q.get("region") or "",
        "market_cap": _f(q.get("marketCap")),
        "pe": _f(q.get("trailingPE")),
        "price": _f(q.get("regularMarketPrice")),
        "change_pct": _round(q.get("regularMarketChangePercent")),
        "volume": _i(q.get("regularMarketVolume")),
    }


# --- yfinance calls (sync; run via to_thread) ------------------------------ #

def _screen_sync(yahoo_id: str, count: int) -> list[dict[str, Any]]:
    import yfinance as yf
    res = yf.screen(yahoo_id, count=count)
    quotes = res.get("quotes", []) if isinstance(res, dict) else []
    rows: list[dict[str, Any]] = []
    for q in quotes:
        row = _row_from_quote(q)
        if row is not None:
            rows.append(row)
    return rows


def _market_caps_sync(tickers: list[str]) -> dict[str, float]:
    import yfinance as yf
    out: dict[str, float] = {}
    for sym in tickers:
        try:
            fi = yf.Ticker(sym).fast_info
            mc = None
            try:
                mc = fi["market_cap"]
            except Exception:  # noqa: BLE001
                mc = getattr(fi, "market_cap", None)
            if mc:
                out[sym] = float(mc)
        except Exception:  # noqa: BLE001
            continue
    return out


# --- async public API ------------------------------------------------------ #

async def fetch_screener(
    *,
    preset: str = DEFAULT_PRESET,
    filters: Optional[str] = None,   # accepted for signature compat; unused (Yahoo presets are fixed)
    limit: int = 30,
    session: Any = None,             # accepted for signature compat; yfinance manages its own session
) -> dict[str, Any]:
    """Run a predefined Yahoo screen. Returns {rows, count, preset, status}."""
    preset = preset if preset in _PRESETS else DEFAULT_PRESET
    yahoo_id = _PRESETS[preset][0]
    limit = max(1, min(limit, 100))

    try:
        import yfinance  # noqa: F401
    except ImportError:
        return {"rows": [], "count": 0, "preset": preset, "status": "yfinance not installed"}

    try:
        rows = await asyncio.to_thread(_screen_sync, yahoo_id, limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("yahoo screen failed (%s): %s", preset, type(exc).__name__)
        return {"rows": [], "count": 0, "preset": preset,
                "status": f"yahoo screen error: {type(exc).__name__}"}

    if not rows:
        return {"rows": [], "count": 0, "preset": preset, "status": "yahoo returned no rows"}

    rows = rows[:limit]
    return {"rows": rows, "count": len(rows), "preset": preset, "status": None}


async def fetch_market_caps(tickers: list[str], *, session: Any = None) -> dict[str, float]:
    """Return ``{ticker: market_cap_usd}`` via yfinance; {} on failure (graceful)."""
    syms = [t.strip().upper() for t in dict.fromkeys(tickers) if t and t.strip()]
    if not syms:
        return {}
    try:
        return await asyncio.to_thread(_market_caps_sync, syms)
    except Exception as exc:  # noqa: BLE001
        log.warning("yahoo market-cap fetch failed: %s", exc)
        return {}


# --- short-interest data (squeeze "fuel"; via yfinance .info) --------------- #

def _short_metrics_sync(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Per-ticker short-interest fields via yfinance ``.info`` (sequential —
    .info is a per-symbol fetch; fine for the few dozen names a squeeze run scans)."""
    import yfinance as yf
    out: dict[str, dict[str, Any]] = {}
    for sym in tickers:
        try:
            info = yf.Ticker(sym).info
        except Exception:  # noqa: BLE001
            continue
        spf = _f(info.get("shortPercentOfFloat"))   # fraction, 0.289 = 28.9%
        sr = _f(info.get("shortRatio"))             # days to cover
        fl = _f(info.get("floatShares"))
        ss = _f(info.get("sharesShort"))
        if spf is None and sr is None and ss is None:
            continue  # no short data published -> skip
        out[sym] = {
            "short_pct_float": spf,
            "short_ratio": sr,
            "float_shares": fl,
            "shares_short": ss,
        }
    return out


async def fetch_short_metrics(
    tickers: list[str], *, session: Any = None
) -> dict[str, dict[str, Any]]:
    """``{ticker: {short_pct_float, short_ratio, float_shares, shares_short}}``
    via yfinance; {} on failure (graceful). Works from datacenter IPs."""
    syms = [t.strip().upper() for t in dict.fromkeys(tickers) if t and t.strip()]
    if not syms:
        return {}
    try:
        import yfinance  # noqa: F401
    except ImportError:
        return {}
    try:
        return await asyncio.to_thread(_short_metrics_sync, syms)
    except Exception as exc:  # noqa: BLE001
        log.warning("short-metrics fetch failed: %s", type(exc).__name__)
        return {}
