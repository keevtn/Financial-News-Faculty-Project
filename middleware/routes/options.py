"""
options.py
==========
  GET /api/options?symbol=TICKER — per-ticker options signal (put/call ratio,
                                    open-interest ratio, ATM implied vol, lean).
                                    Public; on-demand via yfinance; short cache.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from options_flow import fetch_options_signal
from middleware.limiter import limiter

router = APIRouter()

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 300.0  # seconds (chains move slowly intraday)


@router.get("")
@limiter.limit("30/minute")
async def options(request: Request, symbol: str = Query(..., min_length=1, max_length=10)) -> dict[str, Any]:
    sym = symbol.strip().upper()
    if not sym.isalnum():
        raise HTTPException(status_code=400, detail="invalid symbol")

    now = time.monotonic()
    hit = _CACHE.get(sym)
    if hit and hit[0] > now:
        payload = dict(hit[1])
        payload["cached"] = True
        return payload

    signal = await fetch_options_signal(sym)
    payload = {"symbol": sym, "signal": signal, "cached": False,
               "status": None if signal else "no options data"}
    if signal:
        _CACHE[sym] = (now + _CACHE_TTL, payload)
    return payload
