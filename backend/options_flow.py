"""
options_flow.py
===============
Per-ticker **options signal** via yfinance option chains (free, works from
datacenter IPs). The headline read is the **put/call ratio** — a low ratio on
heavy call volume is bullish options flow that corroborates a squeeze setup;
plus at-the-money implied volatility (how much premium the options market is
pricing in).

On-demand, single ticker (option chains are a per-symbol fetch). The pure
``compute_options_signal`` is unit-tested; ``fetch_options_signal`` wraps the
yfinance call.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("options_flow")


def _f0(x: Any) -> float:
    """Float, with None/NaN -> 0.0 (yfinance returns NaN for illiquid strikes)."""
    try:
        v = float(x)
        return v if v == v else 0.0  # NaN != NaN
    except (TypeError, ValueError):
        return 0.0


def _lean(put_call_ratio: Optional[float]) -> str:
    """Directional read from the put/call volume ratio."""
    if put_call_ratio is None:
        return "neutral"
    if put_call_ratio < 0.7:
        return "bullish"   # calls dominating
    if put_call_ratio > 1.0:
        return "bearish"   # puts dominating
    return "neutral"


def _atm_iv(
    calls: list[dict[str, Any]], puts: list[dict[str, Any]], spot: Optional[float]
) -> Optional[float]:
    """Average implied vol of the call+put nearest the spot price."""
    if not spot:
        return None
    ivs: list[float] = []
    for side in (calls, puts):
        candidates = [o for o in side
                      if o.get("strike") is not None and _f0(o.get("impliedVolatility")) > 0]
        if not candidates:
            continue
        near = min(candidates, key=lambda o: abs(_f0(o["strike"]) - spot))
        ivs.append(_f0(near["impliedVolatility"]))
    return round(sum(ivs) / len(ivs), 4) if ivs else None


def compute_options_signal(
    calls: list[dict[str, Any]], puts: list[dict[str, Any]], spot: Optional[float]
) -> dict[str, Any]:
    """
    Aggregate a ticker's option chain into a signal. ``calls``/``puts`` are lists
    of ``{strike, volume, openInterest, impliedVolatility}``. Pure.
    """
    cv = int(sum(_f0(c.get("volume")) for c in calls))
    pv = int(sum(_f0(p.get("volume")) for p in puts))
    coi = int(sum(_f0(c.get("openInterest")) for c in calls))
    poi = int(sum(_f0(p.get("openInterest")) for p in puts))
    pcr_vol = round(pv / cv, 3) if cv else None
    pcr_oi = round(poi / coi, 3) if coi else None
    return {
        "call_volume": cv,
        "put_volume": pv,
        "call_oi": coi,
        "put_oi": poi,
        "put_call_ratio": pcr_vol,        # by volume (the daily flow read)
        "put_call_oi_ratio": pcr_oi,      # by open interest (positioning)
        "atm_iv": _atm_iv(calls, puts, spot),
        "lean": _lean(pcr_vol),
    }


def _fetch_options_sync(ticker: str, max_expiries: int = 2) -> Optional[dict[str, Any]]:
    import yfinance as yf

    sym = ticker.strip().upper()
    tk = yf.Ticker(sym)
    expiries = list(tk.options or [])[:max_expiries]
    if not expiries:
        return None
    try:
        spot = float(tk.fast_info["last_price"])
    except Exception:  # noqa: BLE001
        spot = None

    cols = ["strike", "volume", "openInterest", "impliedVolatility"]
    calls: list[dict[str, Any]] = []
    puts: list[dict[str, Any]] = []
    for exp in expiries:
        try:
            chain = tk.option_chain(exp)
        except Exception as exc:  # noqa: BLE001
            log.warning("option_chain failed for %s %s: %s", sym, exp, type(exc).__name__)
            continue
        calls += chain.calls[cols].to_dict("records")
        puts += chain.puts[cols].to_dict("records")

    if not calls and not puts:
        return None
    signal = compute_options_signal(calls, puts, spot)
    signal["ticker"] = sym
    signal["spot"] = round(spot, 2) if spot else None
    signal["expiries"] = expiries
    return signal


async def fetch_options_signal(ticker: str) -> Optional[dict[str, Any]]:
    """Options signal for one ticker; None on failure / no chain. Never raises."""
    import asyncio
    if not ticker or not ticker.strip():
        return None
    try:
        import yfinance  # noqa: F401
    except ImportError:
        return None
    try:
        return await asyncio.to_thread(_fetch_options_sync, ticker)
    except Exception as exc:  # noqa: BLE001
        log.warning("options fetch failed for %s: %s", ticker, type(exc).__name__)
        return None
