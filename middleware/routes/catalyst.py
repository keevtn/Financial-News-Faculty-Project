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
    get_latest_ranking,
    grade_run,
    rank_catalysts,
    save_ranking,
)
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


@router.get("/track-record")
@limiter.limit("60/minute")
async def track_record(
    request: Request,
    limit: int = Query(default=60, ge=1, le=200),
) -> dict[str, Any]:
    """
    The catalyst ranker's measured performance: aggregate grade metrics across
    graded runs, plus the per-run series for charting. Public read.

    - direction_hit_rate: share of non-neutral calls that moved the called way.
    - reaction_separation: avg |move| of the top half minus the bottom half — did
      the ranking actually surface the bigger movers? (>0 = yes.)
    """
    coll = _rankings_collection(request)
    projection = {
        "_id": 0, "run_id": 1, "generated_at": 1, "used_llm": 1,
        "metrics.graded": 1, "metrics.direction_hit_rate": 1,
        "metrics.reaction_separation": 1, "metrics.session_close": 1,
    }
    docs = await (
        coll.find({"metrics": {"$exists": True}}, projection)
        .sort("generated_at", -1).limit(limit).to_list(length=limit)
    )
    graded = [d for d in docs if (d.get("metrics") or {}).get("graded")]
    hit = [d["metrics"]["direction_hit_rate"] for d in graded
           if d["metrics"].get("direction_hit_rate") is not None]
    sep = [d["metrics"]["reaction_separation"] for d in graded
           if d["metrics"].get("reaction_separation") is not None]

    summary = {
        "graded_runs": len(graded),
        "avg_direction_hit_rate": round(sum(hit) / len(hit), 3) if hit else None,
        "avg_reaction_separation": round(sum(sep) / len(sep), 5) if sep else None,
        "positive_separation_rate": round(sum(1 for s in sep if s > 0) / len(sep), 3) if sep else None,
    }
    runs = [{
        "run_id": d.get("run_id"),
        "generated_at": d.get("generated_at"),
        "used_llm": d.get("used_llm"),
        "direction_hit_rate": (d.get("metrics") or {}).get("direction_hit_rate"),
        "reaction_separation": (d.get("metrics") or {}).get("reaction_separation"),
    } for d in docs]
    return {"summary": summary, "runs": runs}


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


@router.post("/grade/{run_id}", dependencies=[Depends(_require_key)])
@limiter.limit("20/hour")
async def grade(request: Request, run_id: str) -> dict[str, Any]:
    """
    Score a past run against the realized open->close move of the session that
    followed it (direction-agnostic reaction check + directional hit-rate).
    Persists the metrics back onto the run document. Shares ``grade_run`` with
    the auto-grade scheduler.
    """
    coll = _rankings_collection(request)
    run_doc = await coll.find_one({"run_id": run_id}, {"_id": 0})
    if run_doc is None:
        raise HTTPException(status_code=404, detail="run_id not found")
    if not isinstance(run_doc.get("generated_at"), datetime):
        raise HTTPException(status_code=400, detail="run has no usable timestamp")

    metrics = await grade_run(coll, run_doc)
    if metrics is None:
        raise HTTPException(
            status_code=409,
            detail="next session has not closed yet — nothing to grade",
        )
    return {"run_id": run_id, "metrics": metrics}
