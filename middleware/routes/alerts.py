"""
alerts.py
=========
  GET /api/alerts  — current alerts: tickers that crossed a signal threshold,
                     one row each, ranked by confluence (squeeze + gossip +
                     catalyst). Derived live from the latest squeeze ranking, the
                     latest catalyst ranking, and live gossip. Public, short TTL.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Query, Request

import alerts as alerts_engine
from gossip import detect_gossip
from catalyst_ranker import get_latest_ranking
from squeeze_ranker import get_latest_squeeze
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.alerts")
router = APIRouter()

_CACHE: tuple[float, dict[str, Any]] | None = None
_CACHE_TTL = 90.0  # seconds


@router.get("")
@limiter.limit("60/minute")
async def get_alerts(request: Request, nocache: bool = Query(default=False)) -> dict[str, Any]:
    global _CACHE
    now = time.monotonic()
    if not nocache and _CACHE and _CACHE[0] > now:
        payload = dict(_CACHE[1])
        payload["cached"] = True
        return payload

    state = request.app.state

    async def _items(coro, key="items"):
        try:
            res = await coro
            return (res or {}).get(key, []) if res else []
        except Exception as exc:  # noqa: BLE001
            log.warning("alerts: source failed: %s", type(exc).__name__)
            return []

    squeeze_items: list[Any] = []
    catalyst_items: list[Any] = []
    gossip_items: list[Any] = []

    if getattr(state, "squeeze_collection", None) is not None:
        squeeze_items = await _items(get_latest_squeeze(state.squeeze_collection))
    if getattr(state, "rankings_collection", None) is not None:
        catalyst_items = await _items(get_latest_ranking(state.rankings_collection))
    if getattr(state, "news_collection", None) is not None:
        gossip_items = await _items(detect_gossip(state.news_collection))

    fired = alerts_engine.evaluate_alerts(
        squeeze=squeeze_items, catalyst=catalyst_items, gossip=gossip_items
    )
    payload = {
        "alerts": fired,
        "counts": alerts_engine.severity_counts(fired),
        "total": len(fired),
        "generated_at": time.time(),
        "cached": False,
    }
    _CACHE = (now + _CACHE_TTL, payload)
    return payload
