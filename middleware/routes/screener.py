"""
screener.py
===========
Numeric stock-screener endpoints.

  GET /api/screener/presets   — list selectable screen presets (cheap, static)
  GET /api/screener           — run a screen: market cap / price / %chg / volume

Source is chosen at request time: **Finviz Elite** (authorized CSV export) when
``FINVIZ_AUTH_TOKEN`` is configured, otherwise the **Yahoo** source
(``market_screener``). If Elite is configured but fails (e.g. a rotated/stale
token 401s), the request transparently falls back to Yahoo so the screener never
blanks — see ``screener_fallback``. Public reads (the dashboard Screener tab uses
them); results are cached in-process for a short TTL and the route is rate-limited.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Query, Request

import finviz_elite
import market_screener
from screener_fallback import run_with_fallback
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.screener")
router = APIRouter()

# Tiny in-process TTL cache keyed by (source, preset, filters, limit).
_CACHE: dict[tuple[str, str, str, int], tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 60.0  # seconds


def _source():
    """(name, presets_fn, labels_fn, fetch_fn) — Finviz Elite when its token is
    set, else the Yahoo source. Selected per request so setting the env var
    flips it without a code change."""
    if finviz_elite.has_token():
        return ("finviz_elite", finviz_elite.available_presets,
                finviz_elite.preset_labels, finviz_elite.fetch_screener)
    return ("yahoo", market_screener.available_presets,
            market_screener.preset_labels, market_screener.fetch_screener)


@router.get("/presets")
@limiter.limit("60/minute")
async def presets(request: Request) -> dict[str, Any]:
    """Selectable screen presets (of the active source), with human labels."""
    name, presets_fn, labels_fn, _fetch = _source()
    labels = labels_fn()
    return {
        "source": name,
        "presets": [{"id": p, "label": labels.get(p, p)} for p in presets_fn()],
    }


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
      {rows, count, preset, status, source, cached, fetched_at}
    ``status`` is non-null only when the source was unreachable / unparseable
    (rows will be empty) so the UI can explain a blank table.
    """
    primary_name, _p, _l, fetch_fn = _source()
    key = (primary_name, preset, filters or "", limit)
    now = time.monotonic()

    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        payload = dict(hit[1])
        payload["cached"] = True
        return payload

    # Run the primary source; if Finviz Elite is configured but failing (stale
    # token / block / no rows), drop to the Yahoo source so a dead token degrades
    # to delayed data instead of blanking the screener.
    source_name, result = await run_with_fallback(
        preset=preset, filters=filters, limit=limit,
        primary_name=primary_name, primary_fetch=fetch_fn,
        fallback_fetch=market_screener.fetch_screener,
    )
    if source_name != primary_name:
        log.warning("screener: %s failed, served %s fallback", primary_name, source_name)

    # source_name last so it reflects what actually served (Yahoo on fallback).
    payload = {**result, "source": source_name, "cached": False, "fetched_at": time.time()}

    # Only cache successful (non-empty) results so a transient failure isn't
    # pinned for the full TTL.
    if result.get("rows"):
        _CACHE[key] = (now + _CACHE_TTL, payload)

    return payload
