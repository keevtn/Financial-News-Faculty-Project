"""
catalyst.py
===========
Pre-market catalyst-ranking endpoints.

  GET  /api/catalyst/latest      — most recent persisted ranking (public, cheap)
  GET  /api/catalyst/runs        — list recent run metadata (public, cheap)
  POST /api/catalyst/run         — generate + persist a new ranking (protected)
  POST /api/catalyst/grade/{id}  — score a past run against realized moves (protected)

The read endpoints are public so the dashboard (and a faculty advisor) can view
results. The two endpoints that cost money / compute — running the LLM ranker
and pulling market data — require the X-API-Key header, reusing the same
AGENT_API_KEY the agent route uses.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.security.api_key import APIKeyHeader

from catalyst_ranker import (
    grade_ranking,
    get_latest_ranking,
    rank_catalysts,
    save_ranking,
)
from market_calendar import next_session_bounds
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.catalyst")
router = APIRouter()

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_key(key: str | None = Security(_api_key_header)) -> None:
    """Reject requests without a valid X-API-Key (reuses AGENT_API_KEY)."""
    expected = os.environ.get("AGENT_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Catalyst key not configured")
    if not key or not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def _rankings_collection(request: Request) -> Any:
    coll = getattr(request.app.state, "rankings_collection", None)
    if coll is None:
        raise HTTPException(status_code=503, detail="Rankings store unavailable")
    return coll


@router.get("/latest")
@limiter.limit("60/minute")
async def latest(request: Request) -> dict[str, Any]:
    """Return the most recent persisted catalyst ranking."""
    result = await get_latest_ranking(_rankings_collection(request))
    if result is None:
        return {"ranking": None, "note": "No ranking generated yet — POST /api/catalyst/run"}
    return {"ranking": result}


@router.get("/runs")
@limiter.limit("60/minute")
async def runs(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """List recent run metadata (no heavy article payloads)."""
    coll = _rankings_collection(request)
    projection = {
        "_id": 0, "run_id": 1, "generated_at": 1, "window_start": 1,
        "window_end": 1, "used_llm": 1, "candidate_count": 1, "doc_count": 1,
        "metrics": 1,
    }
    docs = await (
        coll.find({}, projection).sort("generated_at", -1).limit(limit).to_list(length=limit)
    )
    return {"runs": docs}


@router.post("/run", dependencies=[Depends(_require_key)])
@limiter.limit("6/hour")
async def run(
    request: Request,
    top_k: int = Query(default=10, ge=1, le=25),
    min_sources: int = Query(default=2, ge=1, le=10),
    baseline_days: int = Query(default=14, ge=1, le=90),
    use_llm: bool = Query(default=True),
) -> dict[str, Any]:
    """Generate, persist, and return a new catalyst ranking."""
    news = getattr(request.app.state, "news_collection", None)
    if news is None:
        raise HTTPException(status_code=503, detail="News store unavailable")

    result = await rank_catalysts(
        news,
        top_k=top_k,
        min_sources=min_sources,
        baseline_days=baseline_days,
        use_llm=use_llm,
    )
    await save_ranking(_rankings_collection(request), result)
    return {"ranking": result}


def _fetch_session_prices_sync(tickers: list[str], start: datetime, end: datetime) -> dict[str, dict[str, float]]:
    """
    Pull daily open/close for ``tickers`` for the session covering [start, end].
    Synchronous (yfinance) — call via asyncio.to_thread.
    """
    import yfinance as yf

    out: dict[str, dict[str, float]] = {}
    # yfinance end is exclusive; pad a day so the session bar is included.
    hist_start = start.date().isoformat()
    hist_end = (end.date().fromordinal(end.date().toordinal() + 1)).isoformat()
    for sym in tickers:
        try:
            df = yf.Ticker(sym).history(start=hist_start, end=hist_end, interval="1d")
            if df is None or df.empty:
                continue
            row = df.iloc[0]
            out[sym] = {"open": float(row["Open"]), "close": float(row["Close"])}
        except Exception as exc:  # noqa: BLE001
            log.warning("price fetch failed for %s: %s", sym, exc)
    return out


@router.post("/grade/{run_id}", dependencies=[Depends(_require_key)])
@limiter.limit("20/hour")
async def grade(request: Request, run_id: str) -> dict[str, Any]:
    """
    Score a past run against the realized open->close move of the session that
    followed it (direction-agnostic reaction check + directional hit-rate).
    Persists the metrics back onto the run document.
    """
    import asyncio

    coll = _rankings_collection(request)
    run_doc = await coll.find_one({"run_id": run_id}, {"_id": 0})
    if run_doc is None:
        raise HTTPException(status_code=404, detail="run_id not found")

    generated_at = run_doc.get("generated_at")
    if not isinstance(generated_at, datetime):
        raise HTTPException(status_code=400, detail="run has no usable timestamp")

    sess_open, sess_close = next_session_bounds(generated_at)
    now = datetime.now(tz=timezone.utc)
    if now < sess_close:
        raise HTTPException(
            status_code=409,
            detail="next session has not closed yet — nothing to grade",
        )

    tickers = [it["ticker"] for it in run_doc.get("items", [])]
    prices = await asyncio.to_thread(_fetch_session_prices_sync, tickers, sess_open, sess_close)
    metrics = grade_ranking(run_doc, prices)
    metrics["session_open"] = sess_open.isoformat()
    metrics["session_close"] = sess_close.isoformat()

    await coll.update_one({"run_id": run_id}, {"$set": {"metrics": metrics}})
    return {"run_id": run_id, "metrics": metrics}
