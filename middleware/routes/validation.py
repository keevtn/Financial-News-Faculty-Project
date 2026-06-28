"""
validation.py
=============
  GET /api/validation — per-signal predictive value across graded runs. Answers
                        "which signals actually predict the move?" for both the
                        squeeze and catalyst rankers. Public read.

Pure analysis over already-persisted graded runs (no recompute, no network).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

import signal_eval as se
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.validation")
router = APIRouter()

_PROJECTION = {"_id": 0, "run_id": 1, "items": 1, "metrics": 1}


async def _graded(coll: Any) -> list[dict[str, Any]]:
    if coll is None:
        return []
    try:
        return await (
            coll.find({"metrics.per_ticker": {"$exists": True}}, _PROJECTION)
            .sort("generated_at", -1).limit(500).to_list(length=500)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("validation load failed: %s", type(exc).__name__)
        return []


@router.get("")
@limiter.limit("30/minute")
async def validation(request: Request) -> dict[str, Any]:
    state = request.app.state
    squeeze_runs = await _graded(getattr(state, "squeeze_collection", None))
    catalyst_runs = await _graded(getattr(state, "rankings_collection", None))

    return {
        "squeeze": {
            "n_runs": len(squeeze_runs),
            "outcome": "max_gain — 5-session peak vs entry",
            "signals": se.evaluate_signals(squeeze_runs, se.SQUEEZE_SIGNALS, se.SQUEEZE_OUTCOME),
        },
        "catalyst": {
            "n_runs": len(catalyst_runs),
            "outcome": "abs_move — |open→close| of the graded session",
            "signals": se.evaluate_signals(catalyst_runs, se.CATALYST_SIGNALS, se.CATALYST_OUTCOME),
        },
        "note": (
            "Spearman correlation between each signal and the realized forward move "
            "across graded tickers. Reads 'insufficient data' until grades accumulate; "
            "the sign matters (a strong negative correlation means the signal is backwards)."
        ),
    }
