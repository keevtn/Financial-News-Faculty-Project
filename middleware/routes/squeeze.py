"""
squeeze.py
==========
Short-squeeze ranking endpoints (the social-driven cousin of the catalyst route).

  GET  /api/squeeze/latest   — most recent persisted squeeze ranking (public)
  GET  /api/squeeze/runs     — recent run metadata (public, cheap)
  POST /api/squeeze/run      — generate + persist a new ranking (protected)

The read endpoints are public so the dashboard can show them. The run endpoint
costs compute (universe screen + yfinance + Bluesky lookups) so it's gated by the
same X-API-Key (AGENT_API_KEY) as the catalyst route.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.security.api_key import APIKeyHeader

from datetime import datetime

from squeeze_ranker import (
    get_latest_squeeze,
    grade_squeeze_run,
    rank_squeezes,
    save_squeeze_ranking,
)
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.squeeze")
router = APIRouter()

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_key(key: str | None = Security(_api_key_header)) -> None:
    expected = os.environ.get("AGENT_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Squeeze key not configured")
    if not key or not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def _squeeze_collection(request: Request) -> Any:
    coll = getattr(request.app.state, "squeeze_collection", None)
    if coll is None:
        raise HTTPException(status_code=503, detail="Squeeze store unavailable")
    return coll


@router.get("/latest")
@limiter.limit("60/minute")
async def latest(request: Request) -> dict[str, Any]:
    """Most recent persisted squeeze ranking."""
    result = await get_latest_squeeze(_squeeze_collection(request))
    if result is None:
        return {"ranking": None, "note": "No squeeze ranking yet — POST /api/squeeze/run"}
    return {"ranking": result}


@router.get("/runs")
@limiter.limit("60/minute")
async def runs(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """Recent run metadata (no per-post payloads)."""
    coll = _squeeze_collection(request)
    projection = {
        "_id": 0, "run_id": 1, "generated_at": 1,
        "universe_count": 1, "fueled_count": 1, "social_count": 1, "params": 1,
    }
    docs = await (
        coll.find({}, projection).sort("generated_at", -1).limit(limit).to_list(length=limit)
    )
    return {"runs": docs}


@router.get("/track-record")
@limiter.limit("60/minute")
async def track_record(
    request: Request,
    limit: int = Query(default=60, ge=1, le=200),
) -> dict[str, Any]:
    """
    The squeeze ranker's measured performance across graded runs:
      - squeeze_hit_rate: share of ranked names that popped >= the hit threshold.
      - reaction_separation: top-half vs bottom-half avg peak gain (did the ranking
        put the bigger poppers on top? >0 = yes).
    Plus the per-run series for charting. Public read.
    """
    coll = _squeeze_collection(request)
    projection = {
        "_id": 0, "run_id": 1, "generated_at": 1,
        "metrics.graded": 1, "metrics.squeeze_hit_rate": 1,
        "metrics.reaction_separation": 1, "metrics.mean_close_return": 1,
    }
    docs = await (
        coll.find({"metrics.graded": {"$gt": 0}}, projection)
        .sort("generated_at", -1).limit(limit).to_list(length=limit)
    )
    hit = [d["metrics"]["squeeze_hit_rate"] for d in docs
           if (d.get("metrics") or {}).get("squeeze_hit_rate") is not None]
    sep = [d["metrics"]["reaction_separation"] for d in docs
           if (d.get("metrics") or {}).get("reaction_separation") is not None]
    ret = [d["metrics"]["mean_close_return"] for d in docs
           if (d.get("metrics") or {}).get("mean_close_return") is not None]

    summary = {
        "graded_runs": len(docs),
        "avg_squeeze_hit_rate": round(sum(hit) / len(hit), 3) if hit else None,
        "avg_reaction_separation": round(sum(sep) / len(sep), 5) if sep else None,
        "avg_close_return": round(sum(ret) / len(ret), 5) if ret else None,
    }
    runs = [{
        "run_id": d.get("run_id"),
        "generated_at": d.get("generated_at"),
        "squeeze_hit_rate": (d.get("metrics") or {}).get("squeeze_hit_rate"),
        "reaction_separation": (d.get("metrics") or {}).get("reaction_separation"),
    } for d in docs]
    return {"summary": summary, "runs": runs}


@router.post("/run", dependencies=[Depends(_require_key)])
@limiter.limit("12/hour")
async def run(
    request: Request,
    top_k: int = Query(default=15, ge=1, le=40),
    min_short_float: float = Query(default=0.10, ge=0.0, le=1.0),
) -> dict[str, Any]:
    """Generate, persist, and return a new squeeze ranking."""
    result = await rank_squeezes(top_k=top_k, min_short_float=min_short_float)
    await save_squeeze_ranking(_squeeze_collection(request), result)
    return {"ranking": result}


@router.post("/grade/{run_id}", dependencies=[Depends(_require_key)])
@limiter.limit("30/hour")
async def grade(request: Request, run_id: str) -> dict[str, Any]:
    """Grade a past run against the realized post-ranking window (shares
    grade_squeeze_run with the auto-grade scheduler)."""
    coll = _squeeze_collection(request)
    run_doc = await coll.find_one({"run_id": run_id}, {"_id": 0})
    if run_doc is None:
        raise HTTPException(status_code=404, detail="run_id not found")
    if not isinstance(run_doc.get("generated_at"), datetime):
        raise HTTPException(status_code=400, detail="run has no usable timestamp")
    metrics = await grade_squeeze_run(coll, run_doc)
    if metrics is None:
        raise HTTPException(status_code=409, detail="grading window has not closed yet")
    return {"run_id": run_id, "metrics": metrics}
