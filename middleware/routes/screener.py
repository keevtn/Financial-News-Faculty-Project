"""
screener.py
===========
Numeric stock-screener endpoints, backed by Finviz's free Overview screener.

  GET /api/screener/presets   — list selectable screen presets (cheap, static)
  GET /api/screener           — run a screen: market cap / price / %chg / volume

Public reads (the dashboard Screener tab uses them). Each request can trigger an
outbound fetch to Finviz, so results are cached in-process for a short TTL to
stay polite and snappy, and the route is rate-limited.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Query, Request

from market_screener import available_presets, fetch_screener, preset_labels
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.screener")
router = APIRouter()

# Tiny in-process TTL cache: {(preset, filters, limit): (expires_at, payload)}.
# Finviz data is delayed anyway, so a short cache costs nothing and shields
# Finviz from per-request hammering.
_CACHE: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 60.0  # seconds


@router.get("/presets")
@limiter.limit("60/minute")
async def presets(request: Request) -> dict[str, Any]:
    """Selectable screen presets, with human labels for the UI."""
    labels = preset_labels()
    return {"presets": [{"id": p, "label": labels.get(p, p)} for p in available_presets()]}


@router.get("")
@limiter.limit("30/minute")
async def screen(
    request: Request,
    preset: str = Query(default="top_gainers"),
    filters: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    """
    Run a screen and return typed rows. Shape:
      {rows: [...], count, preset, status, cached, fetched_at}
    ``status`` is non-null only when Finviz was unreachable / unparseable
    (rows will be empty) so the UI can explain a blank table.
    """
    key = (preset, filters or "", limit)
    now = time.monotonic()

    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        payload = dict(hit[1])
        payload["cached"] = True
        return payload

    result = await fetch_screener(preset=preset, filters=filters, limit=limit)
    payload = {**result, "cached": False, "fetched_at": time.time()}

    # Only cache successful (non-empty) results so a transient block isn't
    # pinned for the full TTL.
    if result.get("rows"):
        _CACHE[key] = (now + _CACHE_TTL, payload)

    return payload
