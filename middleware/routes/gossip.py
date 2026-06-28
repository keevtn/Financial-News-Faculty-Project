"""
gossip.py
=========
Rolling-window mention-velocity ("gossip") endpoint.

  GET /api/gossip  — tickers whose social chatter is accelerating vs their own
                     trailing baseline. Computed live from the social stream
                     (news collection, source_type="social"); short TTL cache.

Public read. Returns [] gracefully when there's no social data ingested.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from gossip import detect_gossip
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.gossip")
router = APIRouter()

_CACHE: dict[tuple[float, float, int], tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 120.0  # seconds


@router.get("")
@limiter.limit("30/minute")
async def gossip(
    request: Request,
    recent_hours: float = Query(default=6.0, ge=0.5, le=72.0),
    baseline_days: float = Query(default=7.0, ge=1.0, le=30.0),
    top_k: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    coll = getattr(request.app.state, "news_collection", None)
    if coll is None:
        raise HTTPException(status_code=503, detail="News store unavailable")

    key = (recent_hours, baseline_days, top_k)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        payload = dict(hit[1])
        payload["cached"] = True
        return payload

    result = await detect_gossip(
        coll, recent_hours=recent_hours, baseline_days=baseline_days, top_k=top_k
    )
    payload = {**result, "cached": False}
    _CACHE[key] = (now + _CACHE_TTL, payload)
    return payload
