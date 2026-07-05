"""
catalyst.py
===========
Pre-market catalyst-ranking endpoints.

  GET  /api/catalyst/latest      — most recent persisted ranking (public, cheap)
  GET  /api/catalyst/runs        — list recent run metadata (public, cheap)
  GET  /api/catalyst/backtest    — replay graded runs under candidate formulas (protected)
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

import catalyst_backtest
from catalyst_ranker import (
    CATALYST_PROFILES,
    DEFAULT_PROFILE,
    get_latest_ranking,
    grade_run,
    rank_catalysts,
    save_ranking,
)
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.catalyst")
router = APIRouter()

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Human-triggered ("manual") runs spend full-Opus credits, so they are capped to
# once per hour globally (cost guard) and forced to Opus regardless of the
# scheduler's CATALYST_MODEL. Both are overridable via env for testing/tuning.
MANUAL_RUN_COOLDOWN_SECONDS = int(os.environ.get("CATALYST_MANUAL_COOLDOWN", "3600"))
MANUAL_RUN_MODEL = os.environ.get("CATALYST_MANUAL_MODEL", "claude-opus-4-8")


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


def _universe_collection(request: Request) -> Any:
    coll = getattr(request.app.state, "universe_collection", None)
    if coll is None:
        raise HTTPException(status_code=503, detail="Candidate universe store unavailable")
    return coll


def _validate_profile(profile: str) -> str:
    """422 on an unknown catalyst profile; echoes back the valid one."""
    if profile not in CATALYST_PROFILES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown profile '{profile}'. Valid: {sorted(CATALYST_PROFILES)}",
        )
    return profile


async def _manual_cooldown_remaining(coll: Any, profile: str) -> int:
    """
    Seconds left before another **manual** run of this profile is allowed
    (0 = allowed now). Per-profile so each lane (combined / regulatory) gets its
    own hourly Opus budget rather than competing for one shared slot.

    Fail closed: if the last-manual-run timestamp can't be read (e.g. MongoDB
    down) we cannot enforce the cost cap, so we block by raising 503 rather than
    silently letting the run through.
    """
    try:
        docs = await (
            coll.find({"trigger": "manual", "profile": profile}, {"_id": 0, "generated_at": 1})
            .sort("generated_at", -1).limit(1).to_list(length=1)
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="Manual-run cooldown can't be verified (store unavailable) — try later.",
        ) from exc
    if not docs:
        return 0
    last = docs[0].get("generated_at")
    if not isinstance(last, datetime):
        return 0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(tz=timezone.utc) - last).total_seconds()
    return max(0, int(MANUAL_RUN_COOLDOWN_SECONDS - elapsed))


async def _load_tuned_weights(request: Request) -> Optional[dict[str, float]]:
    """
    Auto-tuned pre-score weights from catalyst_meta, or None (→ ranker defaults).
    Never raises — a missing store or doc just means "use defaults".
    """
    meta = getattr(request.app.state, "catalyst_meta_collection", None)
    if meta is None:
        return None
    try:
        doc = await meta.find_one({"_id": "weights"}, {"_id": 0, "weights": 1})
    except Exception:  # noqa: BLE001
        return None
    if doc and isinstance(doc.get("weights"), dict):
        return {k: float(v) for k, v in doc["weights"].items()}
    return None


@router.get("/profiles")
@limiter.limit("60/minute")
async def profiles(request: Request) -> dict[str, Any]:
    """The catalyst lanes the dashboard can switch between (name, label, scope)."""
    return {
        "default": DEFAULT_PROFILE,
        "profiles": [
            {"name": name, "label": cfg["label"], "source_types": cfg["source_types"]}
            for name, cfg in CATALYST_PROFILES.items()
        ],
    }


@router.get("/latest")
@limiter.limit("60/minute")
async def latest(
    request: Request,
    profile: str = Query(default=DEFAULT_PROFILE),
) -> dict[str, Any]:
    """Return the most recent persisted catalyst ranking for ``profile``."""
    profile = _validate_profile(profile)
    result = await get_latest_ranking(_rankings_collection(request), profile=profile)
    if result is None:
        return {
            "ranking": None, "profile": profile,
            "note": f"No '{profile}' ranking yet — POST /api/catalyst/run?profile={profile}",
        }
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


@router.get("/backtest", dependencies=[Depends(_require_key)])
@limiter.limit("10/hour")
async def backtest(
    request: Request,
    top: int = Query(default=5, ge=1, le=20),
) -> dict[str, Any]:
    """
    Replay every *graded* run under candidate pre-score formulas (no LLM, no
    price re-fetch — uses the realized moves frozen at grading time) and report:

      - baseline           — current default weights, all factors on.
      - confirmation_ablation — the pre-market boost ON vs OFF (the one clean
        hypothesis test: does it improve the ranking?).
      - weight_sweep       — exploratory grid over component weights (overfits
        with few runs; directional only).

    Protected (reuses AGENT_API_KEY) because it scans full run documents.
    """
    coll = _rankings_collection(request)
    projection = {
        "_id": 0, "run_id": 1, "generated_at": 1,
        "items.ticker": 1, "items.components": 1, "metrics": 1,
    }
    runs = await (
        coll.find({"metrics.graded": {"$gt": 0}}, projection)
        .sort("generated_at", -1).limit(500).to_list(length=500)
    )
    baseline = catalyst_backtest.backtest(runs)
    return {
        "n_graded_runs": baseline["n_runs"],
        "baseline": baseline,
        "confirmation_ablation": catalyst_backtest.confirmation_ablation(runs),
        "weight_sweep": catalyst_backtest.sweep(runs, top=top),
        "note": (
            "Re-ranks the persisted shortlist only (not the selection boundary). "
            "The weight sweep overfits with few runs — trust the confirmation "
            "ablation and watch baseline metrics accumulate before changing weights."
        ),
    }


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
@limiter.limit("10/hour")
async def run(
    request: Request,
    top_k: int = Query(default=10, ge=1, le=25),
    min_sources: int = Query(default=2, ge=1, le=10),
    baseline_days: int = Query(default=14, ge=1, le=90),
    use_llm: bool = Query(default=True),
    trigger: str = Query(default="manual", pattern="^(manual|scheduled|api)$"),
    profile: str = Query(default=DEFAULT_PROFILE),
) -> dict[str, Any]:
    """
    Generate, persist, and return a new catalyst ranking for ``profile``.

    Human-triggered (``trigger=manual``, the frontend "Run now" button via the
    Next.js proxy) runs are capped to once per hour *per profile* (cost guard)
    and forced to full Opus regardless of ``CATALYST_MODEL``. The slowapi limit
    is a loose anti-spam backstop; the real cap is the persisted-timestamp
    cooldown.
    """
    profile = _validate_profile(profile)
    news = getattr(request.app.state, "news_collection", None)
    if news is None:
        raise HTTPException(status_code=503, detail="News store unavailable")
    coll = _rankings_collection(request)

    model: str | None = None
    if trigger == "manual":
        remaining = await _manual_cooldown_remaining(coll, profile)
        if remaining > 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "detail": f"Manual '{profile}' catalyst run is on cooldown.",
                    "retry_after_seconds": remaining,
                },
                headers={"Retry-After": str(remaining)},
            )
        model = MANUAL_RUN_MODEL  # force full Opus for the button

    result = await rank_catalysts(
        news,
        top_k=top_k,
        min_sources=min_sources,
        baseline_days=baseline_days,
        use_llm=use_llm,
        model=model,
        trigger=trigger,
        weights=await _load_tuned_weights(request),
        profile=profile,
        calendar_collection=getattr(request.app.state, "catalyst_calendar_collection", None),
    )
    await save_ranking(coll, result)
    return {"ranking": result}


@router.get("/universe")
@limiter.limit("60/minute")
async def universe(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    promoted_only: bool = Query(default=False),
) -> dict[str, Any]:
    """
    Accumulated sub-threshold candidate tickers from the 12h universe job —
    names that don't (yet) clear the standard model's volume floor but are
    building evidence over time. Public read. Sorted most-recently-seen first.
    """
    coll = _universe_collection(request)
    query: dict[str, Any] = {"promoted": True} if promoted_only else {}
    docs = await (
        coll.find(query, {"_id": 0})
        .sort("last_seen", -1).limit(limit).to_list(length=limit)
    )
    return {"items": docs, "count": len(docs)}


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
