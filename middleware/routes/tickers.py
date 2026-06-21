from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Query

router = APIRouter()

# Module-level price cache: symbol -> (data_dict, expires_at)
# TTL of 55s means each 60s frontend poll always gets a fresh fetch while
# simultaneous requests within the same window are served from cache.
_cache: dict[str, tuple[dict[str, Any], float]] = {}
_CACHE_TTL = 55


def _fetch_prices_sync(symbols: list[str]) -> dict[str, Any]:
    try:
        import yfinance as yf
    except ImportError:
        return {s: {"symbol": s, "price": None, "change": None, "change_pct": None} for s in symbols}

    now = time.time()
    result: dict[str, Any] = {}
    to_fetch: list[str] = []

    for sym in symbols:
        cached = _cache.get(sym)
        if cached and cached[1] > now:
            result[sym] = cached[0]
        else:
            to_fetch.append(sym)

    for sym in to_fetch:
        try:
            fi = yf.Ticker(sym).fast_info
            price = fi.last_price
            prev = fi.previous_close
            if price is not None and prev:
                chg = round(float(price) - float(prev), 2)
                chg_pct = round(chg / float(prev) * 100, 2)
                entry: dict[str, Any] = {
                    "symbol": sym,
                    "price": round(float(price), 2),
                    "change": chg,
                    "change_pct": chg_pct,
                }
            else:
                entry = {"symbol": sym, "price": None, "change": None, "change_pct": None}
        except Exception:
            entry = {"symbol": sym, "price": None, "change": None, "change_pct": None}

        _cache[sym] = (entry, now + _CACHE_TTL)
        result[sym] = entry

    return result


@router.get("/prices")
async def get_ticker_prices(
    symbols: str = Query(..., description="Comma-separated ticker symbols, max 20"),
) -> dict[str, Any]:
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
    if not sym_list:
        return {"prices": {}}

    prices = await asyncio.to_thread(_fetch_prices_sync, sym_list)
    return {"prices": prices}


# Richer quote cache (separate from the light /prices cache the ticker tape uses).
_quote_cache: dict[str, tuple[dict[str, Any], float]] = {}

_NULL_QUOTE_FIELDS = (
    "price", "change", "change_pct", "volume",
    "market_cap", "day_high", "day_low", "prev_close",
)


def _g(fi: Any, attr: str) -> Any:
    """Safe fast_info attribute read (returns None on any error)."""
    try:
        return getattr(fi, attr)
    except Exception:  # noqa: BLE001
        return None


def _fetch_quotes_sync(symbols: list[str]) -> dict[str, Any]:
    """Full live quote per symbol via yfinance fast_info (price, %chg, volume,
    market cap, day range). Cached like /prices."""
    def _null(sym: str) -> dict[str, Any]:
        return {"symbol": sym, **{k: None for k in _NULL_QUOTE_FIELDS}}

    try:
        import yfinance as yf
    except ImportError:
        return {s: _null(s) for s in symbols}

    now = time.time()
    result: dict[str, Any] = {}
    to_fetch: list[str] = []
    for sym in symbols:
        cached = _quote_cache.get(sym)
        if cached and cached[1] > now:
            result[sym] = cached[0]
        else:
            to_fetch.append(sym)

    for sym in to_fetch:
        try:
            fi = yf.Ticker(sym).fast_info
            price = _g(fi, "last_price")
            prev = _g(fi, "previous_close")
            chg = chg_pct = None
            if price is not None and prev:
                chg = round(float(price) - float(prev), 2)
                chg_pct = round(chg / float(prev) * 100, 2)
            vol = _g(fi, "last_volume")
            mc = _g(fi, "market_cap")
            dh = _g(fi, "day_high")
            dl = _g(fi, "day_low")
            entry: dict[str, Any] = {
                "symbol": sym,
                "price": round(float(price), 2) if price is not None else None,
                "change": chg,
                "change_pct": chg_pct,
                "volume": int(vol) if vol else None,
                "market_cap": float(mc) if mc else None,
                "day_high": round(float(dh), 2) if dh else None,
                "day_low": round(float(dl), 2) if dl else None,
                "prev_close": round(float(prev), 2) if prev else None,
            }
        except Exception:  # noqa: BLE001
            entry = _null(sym)
        _quote_cache[sym] = (entry, now + _CACHE_TTL)
        result[sym] = entry

    return result


@router.get("/quotes")
async def get_ticker_quotes(
    symbols: str = Query(..., description="Comma-separated ticker symbols, max 25"),
) -> dict[str, Any]:
    """Richer live quotes (price/%chg/volume/market cap/day range) — used by the
    Catalysts tab to show each ranked ticker's current market data."""
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:25]
    if not sym_list:
        return {"quotes": {}}
    quotes = await asyncio.to_thread(_fetch_quotes_sync, sym_list)
    return {"quotes": quotes}
