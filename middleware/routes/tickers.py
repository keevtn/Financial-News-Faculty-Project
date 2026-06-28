from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

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


# --- OHLCV history (for candlestick charts) -------------------------------- #

# range key -> (yfinance period, interval). Intraday for short ranges, daily
# bars for longer ones.
_HISTORY_RANGES: dict[str, tuple[str, str]] = {
    "1D": ("1d", "5m"),
    "5D": ("5d", "15m"),
    "1M": ("1mo", "1d"),
    "3M": ("3mo", "1d"),
    "1Y": ("1y", "1d"),
}
_history_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
_HIST_TTL_INTRADAY = 120     # seconds (1D / 5D)
_HIST_TTL_DAILY = 1800       # seconds (1M / 3M / 1Y)


def _fetch_history_sync(symbol: str, range_key: str) -> dict[str, Any]:
    """OHLCV bars for one symbol/range via yfinance; shaped for lightweight-charts."""
    period, interval = _HISTORY_RANGES[range_key]
    now = time.time()
    cache_key = (symbol, range_key)
    cached = _history_cache.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]

    bars: list[dict[str, Any]] = []
    prev_close = None
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        df = tk.history(period=period, interval=interval)
        if df is not None and not df.empty:
            for ts, row in df.iterrows():
                try:
                    vol = row["Volume"]
                    bars.append({
                        "time": int(ts.timestamp()),
                        "open": round(float(row["Open"]), 4),
                        "high": round(float(row["High"]), 4),
                        "low": round(float(row["Low"]), 4),
                        "close": round(float(row["Close"]), 4),
                        "volume": int(vol) if vol == vol else 0,  # NaN != NaN
                    })
                except Exception:  # noqa: BLE001
                    continue
        try:
            pc = tk.fast_info.previous_close
            prev_close = round(float(pc), 4) if pc else None
        except Exception:  # noqa: BLE001
            prev_close = None
    except Exception:  # noqa: BLE001
        bars = []

    payload = {
        "symbol": symbol, "range": range_key, "interval": interval,
        "prev_close": prev_close, "bars": bars,
        "status": None if bars else "no data",
    }
    ttl = _HIST_TTL_INTRADAY if range_key in ("1D", "5D") else _HIST_TTL_DAILY
    if bars:
        _history_cache[cache_key] = (payload, now + ttl)
    return payload


@router.get("/history")
async def get_ticker_history(
    symbol: str = Query(..., description="Single ticker symbol"),
    range: str = Query(default="1M", description="1D | 5D | 1M | 3M | 1Y"),
) -> dict[str, Any]:
    """Candlestick OHLCV bars for one ticker over a range (for the chart view)."""
    sym = symbol.strip().upper()
    range_key = range.upper() if range.upper() in _HISTORY_RANGES else "1M"
    if not sym:
        return {"symbol": "", "range": range_key, "bars": [], "status": "no symbol"}
    return await asyncio.to_thread(_fetch_history_sync, sym, range_key)


# --- sentiment history (news + social, for the chart-alongside-price view) --- #

@router.get("/sentiment-history")
async def get_ticker_sentiment_history(
    request: Request,
    symbol: str = Query(..., description="Single ticker symbol"),
    days: int = Query(default=30, ge=1, le=400),
) -> dict[str, Any]:
    """
    Daily mean sentiment + mention count for a ticker, **split by source** (news =
    rss/sec/fda, social = bluesky etc.), from the stored items. Lets the chart show
    news-only, social-only, the blend, or both lines overlaid. Spans only as far
    back as the corpus has been ingested.
    """
    sym = symbol.strip().upper()
    coll = getattr(request.app.state, "news_collection", None)
    if not sym or coll is None:
        return {"symbol": sym, "days": days, "points": [], "status": "unavailable"}

    start = datetime.now(tz=timezone.utc) - timedelta(days=days)
    query = {"tickers": sym, "published_at": {"$gte": start}, "sentiment.score": {"$exists": True}}
    try:
        docs = await (
            coll.find(query, {"_id": 0, "published_at": 1, "sentiment.score": 1, "source_type": 1})
            .limit(20_000).to_list(length=20_000)
        )
    except Exception:  # noqa: BLE001
        return {"symbol": sym, "days": days, "points": [], "status": "query failed"}

    # day -> {"news": [count, sum], "social": [count, sum]}
    buckets: dict[Any, dict[str, list[float]]] = defaultdict(
        lambda: {"news": [0.0, 0.0], "social": [0.0, 0.0]}
    )
    for d in docs:
        pub = d.get("published_at")
        score = (d.get("sentiment") or {}).get("score")
        if not isinstance(pub, datetime) or score is None:
            continue
        cls = "social" if d.get("source_type") == "social" else "news"
        b = buckets[pub.date()][cls]
        b[0] += 1
        b[1] += float(score)

    points = []
    for day in sorted(buckets):
        b = buckets[day]
        nc, ns = b["news"]
        sc, ss = b["social"]
        points.append({
            "time": int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()),
            "news_sentiment": round(ns / nc, 4) if nc else None,
            "news_count": int(nc),
            "social_sentiment": round(ss / sc, 4) if sc else None,
            "social_count": int(sc),
        })

    return {"symbol": sym, "days": days, "points": points,
            "status": None if points else "no sentiment data"}
