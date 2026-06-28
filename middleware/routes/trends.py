"""
trends.py
=========
  GET /api/trends?symbol=TICKER — Google-Trends search-interest velocity for one
                                  ticker (inspection/transparency). Gated by
                                  RUN_TRENDS; best-effort. Public.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from trends import has_trends_enabled, search_signals
from middleware.limiter import limiter

router = APIRouter()


@router.get("")
@limiter.limit("20/minute")
async def trends(request: Request, symbol: str = Query(..., min_length=1, max_length=10)) -> dict[str, Any]:
    sym = symbol.strip().upper()
    if not sym.isalnum():
        raise HTTPException(status_code=400, detail="invalid symbol")
    if not has_trends_enabled():
        return {"symbol": sym, "signal": None, "status": "trends disabled (set RUN_TRENDS)"}
    sig = await search_signals([sym], {sym: {}})
    return {"symbol": sym, "signal": sig.get(sym),
            "status": None if sig.get(sym) else "no trends data (low search volume or rate-limited)"}
