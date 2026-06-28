"""
trends.py
=========
Google Trends **search-interest velocity** for short squeezes — a fourth
attention channel (mainstream retail search) alongside news, social, and options.

Self-contained async client (no archived `pytrends` dependency): seed cookie →
explore (token) → widgetdata/multiline (timeline). **Best-effort and gated**
(`RUN_TRENDS`, default off): any failure / 429 / disabled → empty, so the squeeze
ignition simply falls back to the signals it already has. Never raises.

Design (grounded in live probing of the API)
--------------------------------------------
- **One reliable tier**: ``now 7-d`` hourly. Rich for any ticker with real search
  interest, and **batchable 5 terms/request** (the comparison query) — which is
  also *correct*, because velocity is a ratio so the per-batch normalization
  cancels. This is the workhorse.
- **No per-minute tier**: live probing showed sub-day windows are near-empty for
  individual tickers except during an actual surge, so a per-minute velocity
  would divide into a zero baseline (garbage). Instead, **fuel decides
  sensitivity** on the hourly tier: fast-clock names (low float / low
  days-to-cover → intraday squeezers) react to smaller search moves; slow-clock
  names (high days-to-cover → multi-day grinders) need a sustained build.

Performance: one shared session, one cookie seed, ≤5 tickers/request, paced
batches, and a 30-min per-ticker velocity cache (Trends hourly data is stable
intraday). For a ~30-name candidate set that's ~6 paced requests, cached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("trends")

_BASE = "https://trends.google.com"
_HL = "en-US"
_TZ = "360"
_TIMEFRAME = "now 7-d"        # hourly, ~169 points
_GEO = "US"
_RECENT_POINTS = 24           # last ~24h vs the trailing 6-day baseline
_BASELINE_FLOOR = 0.5
_BATCH = 5                    # max terms per comparison request
_PACE = 1.0                   # seconds between batch requests (rate-limit safety)
_TIMEOUT = 20
_CACHE_TTL = 1800.0          # 30 min per-ticker velocity cache

# Fuel-adaptive clock thresholds + velocity saturation per clock.
_FAST_FLOAT = 50e6            # float at/below this -> fast (intraday) clock
_FAST_DTC = 3.0              # days-to-cover below this -> fast clock
_SAT_FAST = 1.8              # fast names: a 1.8x search uptick maxes the term
_SAT_SLOW = 2.5              # slow names: need a bigger sustained build

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# {ticker: (expiry_monotonic, build_velocity)}
_CACHE: dict[str, tuple[float, Optional[float]]] = {}


def _env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def has_trends_enabled() -> bool:
    return _env_flag("RUN_TRENDS", False)


def _term(ticker: str) -> str:
    """Disambiguate the ticker as a search term ('GME' -> 'GME stock')."""
    return f"{ticker.strip().upper()} stock"


def _strip(raw: str) -> Any:
    """Trends prefixes its JSON with ``)]}'`` — parse from the first brace."""
    i = raw.find("{")
    return json.loads(raw[i:]) if i >= 0 else None


# --- pure signal math ------------------------------------------------------ #

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def velocity_of(values: list[int], recent_points: int = _RECENT_POINTS,
                floor: float = _BASELINE_FLOOR) -> Optional[float]:
    """Recent-vs-baseline search velocity (a normalization-invariant ratio).
    None when there isn't enough data to be meaningful."""
    if not values or len(values) < recent_points * 2:
        return None
    recent = _mean(values[-recent_points:])
    base = _mean(values[:-recent_points])
    return round(recent / max(base, floor), 3)


def clock_of(short_ratio: Optional[float], float_shares: Optional[float]) -> str:
    """'fast' (intraday squeezer) vs 'slow' (multi-day grinder) from the fuel."""
    if (float_shares is not None and 0 < float_shares < _FAST_FLOAT) or \
       (short_ratio is not None and short_ratio < _FAST_DTC):
        return "fast"
    return "slow"


def search_term(velocity: Optional[float], clock: str) -> float:
    """Fuel-adaptive [0,1] ignition term: ramp from 0 at 1x baseline to 1.0 at
    the clock's saturation (fast names are more sensitive)."""
    if velocity is None:
        return 0.0
    sat = _SAT_FAST if clock == "fast" else _SAT_SLOW
    return round(max(0.0, min((velocity - 1.0) / (sat - 1.0), 1.0)), 4)


# --- async fetch (best-effort) --------------------------------------------- #

async def _fetch_batch(session: Any, terms: list[str]) -> dict[str, list[int]]:
    """Hourly 7-d series for up to 5 terms in one comparison request. {} on
    any failure (never raises)."""
    try:
        req = {"comparisonItem": [{"keyword": t, "geo": _GEO, "time": _TIMEFRAME} for t in terms],
               "category": 0, "property": ""}
        async with session.get(f"{_BASE}/trends/api/explore",
                               params={"hl": _HL, "tz": _TZ, "req": json.dumps(req)},
                               timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                log.debug("trends explore HTTP %s", resp.status)
                return {}
            data = _strip(await resp.text())
        widget = next((w for w in (data or {}).get("widgets", []) if w.get("id") == "TIMESERIES"), None)
        if not widget:
            return {}
        async with session.get(f"{_BASE}/trends/api/widgetdata/multiline",
                               params={"hl": _HL, "tz": _TZ,
                                       "req": json.dumps(widget["request"]), "token": widget["token"]},
                               timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                log.debug("trends multiline HTTP %s", resp.status)
                return {}
            tl = (_strip(await resp.text()) or {}).get("default", {}).get("timelineData", [])
        out: dict[str, list[int]] = {}
        for i, t in enumerate(terms):
            out[t] = [int(p["value"][i]) for p in tl if i < len(p.get("value", []))]
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("trends batch failed: %s", type(exc).__name__)
        return {}


async def search_signals(
    tickers: list[str],
    fuel: dict[str, dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> dict[str, dict[str, Any]]:
    """
    ``{ticker: {build_velocity, clock, search_term}}`` for the candidate set.
    Gated by ``RUN_TRENDS`` and fully best-effort: returns {} when disabled, and
    silently omits any ticker whose fetch failed. ``fuel`` maps ticker ->
    {short_ratio, float_shares} (drives the fast/slow clock).
    """
    syms = [t.strip().upper() for t in dict.fromkeys(tickers) if t and t.strip()]
    if not has_trends_enabled() or not syms:
        return {}
    try:
        import aiohttp
    except ImportError:
        return {}

    now = now if now is not None else time.monotonic()
    velocities: dict[str, Optional[float]] = {}
    to_fetch: list[str] = []
    for s in syms:
        hit = _CACHE.get(s)
        if hit and hit[0] > now:
            velocities[s] = hit[1]
        else:
            to_fetch.append(s)

    if to_fetch:
        async with aiohttp.ClientSession(headers=_HEADERS) as session:
            try:
                await session.get(f"{_BASE}/?geo={_GEO}", timeout=_TIMEOUT)  # seed cookies
            except Exception:  # noqa: BLE001
                pass
            for i in range(0, len(to_fetch), _BATCH):
                batch = to_fetch[i:i + _BATCH]
                series = await _fetch_batch(session, [_term(s) for s in batch])
                for s in batch:
                    vel = velocity_of(series.get(_term(s), []))
                    velocities[s] = vel
                    _CACHE[s] = (now + _CACHE_TTL, vel)
                if i + _BATCH < len(to_fetch):
                    await asyncio.sleep(_PACE)

    out: dict[str, dict[str, Any]] = {}
    for s in syms:
        vel = velocities.get(s)
        if vel is None:
            continue  # no usable data -> contributes nothing
        f = fuel.get(s, {})
        clk = clock_of(f.get("short_ratio"), f.get("float_shares"))
        out[s] = {"build_velocity": vel, "clock": clk, "search_term": search_term(vel, clk)}
    return out
