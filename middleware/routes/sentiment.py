"""
sentiment.py
============
Sentiment aggregation endpoints — skeleton implementation.

Needed Implementation:
    Wire to RedisHandler.recent_sentiment() once a RedisHandler instance
    is available in application state.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from middleware.limiter import limiter

router = APIRouter()


# ---------------------------------------------------------------------------
# Per-item scoring — FinBERT batch endpoint
# ---------------------------------------------------------------------------

class _BatchItem(BaseModel):
    id: str
    title: str
    description: str = ""


class _BatchScoreRequest(BaseModel):
    items: list[_BatchItem]

    @field_validator("items")
    @classmethod
    def cap_items(cls, v: list) -> list:
        if len(v) > 100:
            raise ValueError("Batch size cannot exceed 100 items")
        return v


@router.post("/batch")
@limiter.limit("20/minute")
async def score_batch(req: _BatchScoreRequest, request: Request) -> dict[str, Any]:
    """
    Score a batch of news items and return a label + score for each.

    Uses FinBERT when available (local dev with torch installed); automatically
    falls back to the Loughran-McDonald keyword scorer on deployments where
    torch/transformers are not installed.

    Request body:
        {"items": [{"id": "...", "title": "...", "description": "..."}, ...]}

    Response:
        {"results": {"<id>": {"score": float, "label": str, "confidence": float}, ...}}
    """
    analyzer = request.app.state.sentiment_analyzer or request.app.state.lm_analyzer
    if analyzer is None:
        raise HTTPException(
            status_code=503,
            detail="No sentiment analyzer available — check middleware logs.",
        )
    pairs = [(item.title, item.description) for item in req.items]
    # Run FinBERT inference in a thread so it doesn't block the event loop
    results: list = await asyncio.to_thread(analyzer.analyze_text_batch, pairs)
    return {
        "results": {
            item.id: {
                "score": result.score,
                "label": result.label,
                "confidence": result.confidence,
            }
            for item, result in zip(req.items, results)
        }
    }


@router.post("/social/batch")
@limiter.limit("120/minute")
async def score_social_batch(req: _BatchScoreRequest, request: Request) -> dict[str, Any]:
    """
    Score a batch of social/unstructured items with the Loughran-McDonald keyword
    scorer (~1 ms per item, no GPU required).

    Used by the frontend for social items that arrive without pre-scored sentiment
    (e.g. when MongoDB is not running or the item was ingested without scoring).
    StockTwits items with human labels should be handled client-side before calling
    this endpoint — only pass items that genuinely need keyword scoring.

    Request / response shape is identical to POST /batch.
    """
    analyzer = request.app.state.lm_analyzer
    if analyzer is None:
        raise HTTPException(
            status_code=503,
            detail="LM analyzer unavailable — check middleware logs.",
        )
    pairs = [(item.title, item.description) for item in req.items]
    results: list = await asyncio.to_thread(analyzer.analyze_text_batch, pairs)
    return {
        "results": {
            item.id: {
                "score": result.score,
                "label": result.label,
                "confidence": result.confidence,
            }
            for item, result in zip(req.items, results)
        }
    }


@router.get("/")
@limiter.limit("60/minute")
async def get_sentiment_summary(
    request: Request,
    scope: str = Query(
        default="global",
        description="Scope: global | type:<rss|sec|fda> | source:<name> | kw:<keyword>",
    ),
    window_seconds: int = Query(default=3600, ge=60, description="Recency window in seconds"),
) -> dict[str, Any]:
    """
    Return aggregated sentiment statistics for the given scope + time window.

    Needed Implementation:
        from backend.storage_handlers import RedisHandler
        result = await redis_handler.recent_sentiment(scope, window_seconds)
        return result
    """
    return {
        "scope": scope,
        "count": 0,
        "mean": None,
        "min": None,
        "max": None,
        "label_counts": {"bullish": 0, "bearish": 0, "neutral": 0},
        "dominant_label": None,
        "window_seconds": window_seconds,
        "note": "Needed Implementation: connect RedisHandler",
    }


@router.get("/topics")
@limiter.limit("30/minute")
async def get_topic_sentiment(request: Request) -> dict[str, Any]:
    """
    Return per-topic sentiment breakdown for the current recency window.

    Needed Implementation:
        For each topic in TOPIC_KEYWORDS, call
        redis_handler.recent_sentiment(f"kw:{keyword}") and aggregate.
    """
    return {
        "topics": {},
        "note": "Needed Implementation: connect RedisHandler",
    }
