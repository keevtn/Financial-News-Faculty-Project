from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from middleware.limiter import limiter

router = APIRouter()


def _doc_to_item(doc: dict) -> dict[str, Any]:
    """Map a MongoDB document to the frontend NewsItem shape."""
    published = doc.get("published_at")
    if isinstance(published, datetime):
        published = published.isoformat()
    return {
        "id": doc.get("content_hash", ""),
        "source": doc.get("source", ""),
        "source_type": doc.get("source_type", "rss"),
        "title": doc.get("title", ""),
        "published_at": published or "",
        "description": doc.get("description", ""),
        "url": doc.get("url", ""),
        "topic": doc.get("topic") or "General",
        "tickers": doc.get("tickers") or [],
        "extra": doc.get("extra") or {},
        "sentiment": doc.get("sentiment") or None,
    }


async def _fetch_guaranteed(
    collection: Any,
    base_query: dict[str, Any],
    source_type_val: str,
    n: int,
) -> list[dict[str, Any]]:
    """
    Fetch the n most-recent items of a specific source_type while preserving
    any topic/search filters from base_query. The source_type key in base_query
    is replaced so a caller-level source_type filter doesn't suppress this fetch.
    """
    q = {k: v for k, v in base_query.items() if k != "source_type"}
    q["source_type"] = source_type_val
    return await (
        collection.find(q, {"_id": 0})
        .sort("published_at", -1)
        .limit(n)
        .to_list(length=n)
    )


@router.get("/")
@limiter.limit("60/minute")
async def list_news(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500, description="Max RSS/social items to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    source_type: Optional[str] = Query(default=None, description="Filter: rss | sec | fda | social | structured (=rss+sec+fda)"),
    topic: Optional[str] = Query(default=None, description="Filter by topic label"),
    search: Optional[str] = Query(default=None, description="Keyword search in title + description"),
    sec_limit: int = Query(default=25, ge=0, le=100, description="Guaranteed minimum SEC items regardless of recency"),
    fda_limit: int = Query(default=25, ge=0, le=100, description="Guaranteed minimum FDA items regardless of recency"),
    social_limit: int = Query(default=50, ge=0, le=200, description="Guaranteed minimum social items regardless of recency"),
    rss_limit: int = Query(default=100, ge=0, le=300, description="Guaranteed minimum RSS items regardless of recency"),
) -> dict[str, Any]:
    """
    Return news items with guaranteed per-source representation, so no single
    high-volume source crowds the others out of the time-sorted recency window.

    The `limit` parameter controls the main query (newest items across all
    types). On top of that, `rss_limit`, `sec_limit`, `fda_limit`, and
    `social_limit` each inject the latest N items of that type not already in the
    main result. This is symmetric on purpose: it keeps the structured feed's
    RSS alive when high-volume social floods recency, *and* keeps SEC/FDA/social
    visible when newswire RSS dominates.

    When an explicit `source_type` filter is provided, guaranteed-minimum fetches
    are skipped because the caller already knows what they want.
    """
    collection = request.app.state.news_collection
    if collection is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    # Base filter shared by all queries. "structured" is a convenience alias for
    # the non-social types so the Structured tab can fetch independently of the
    # high-volume social feed (which otherwise crowds RSS out of a shared window).
    is_structured = source_type == "structured"
    query: dict[str, Any] = {}
    if is_structured:
        query["source_type"] = {"$in": ["rss", "sec", "fda"]}
    elif source_type:
        query["source_type"] = source_type
    if topic:
        query["topic"] = {"$regex": re.escape(topic), "$options": "i"}
    if search:
        safe = re.escape(search)
        query["$or"] = [
            {"title": {"$regex": safe, "$options": "i"}},
            {"description": {"$regex": safe, "$options": "i"}},
        ]

    # Guaranteed-minimum fetches. On the unfiltered combined feed, guarantee every
    # type. On the "structured" feed, still guarantee low-volume SEC/FDA (RSS would
    # otherwise bury them) but not social/rss. Skipped for a single explicit
    # source_type (rss|sec|fda|social) — the caller already knows what it wants.
    run_sec = (not source_type or is_structured) and sec_limit > 0
    run_fda = (not source_type or is_structured) and fda_limit > 0
    run_social = (not source_type) and social_limit > 0
    run_rss = (not source_type) and rss_limit > 0

    # Run main query + guaranteed-type queries in parallel
    coros: list[Any] = [
        collection.find(query, {"_id": 0})
        .sort("published_at", -1)
        .skip(offset)
        .limit(limit)
        .to_list(length=limit),
        collection.count_documents(query),
    ]
    if run_sec:
        coros.append(_fetch_guaranteed(collection, query, "sec", sec_limit))
    if run_fda:
        coros.append(_fetch_guaranteed(collection, query, "fda", fda_limit))
    if run_social:
        coros.append(_fetch_guaranteed(collection, query, "social", social_limit))
    if run_rss:
        coros.append(_fetch_guaranteed(collection, query, "rss", rss_limit))

    results = await asyncio.gather(*coros)
    main_docs: list[dict[str, Any]] = results[0]
    total: int = results[1]

    # Collect guaranteed extras that aren't already in the main result
    extra_docs: list[dict[str, Any]] = []
    idx = 2
    if run_sec:
        extra_docs.extend(results[idx]); idx += 1
    if run_fda:
        extra_docs.extend(results[idx]); idx += 1
    if run_social:
        extra_docs.extend(results[idx]); idx += 1
    if run_rss:
        extra_docs.extend(results[idx]); idx += 1

    if extra_docs:
        seen = {d.get("content_hash") for d in main_docs}
        supplemental = [d for d in extra_docs if d.get("content_hash") not in seen]
        if supplemental:
            _epoch = datetime.fromtimestamp(0, tz=timezone.utc)
            docs = sorted(
                main_docs + supplemental,
                key=lambda d: d.get("published_at") or _epoch,
                reverse=True,
            )
        else:
            docs = main_docs
    else:
        docs = main_docs

    return {
        "items": [_doc_to_item(d) for d in docs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/topics")
@limiter.limit("30/minute")
async def list_topics(request: Request) -> dict[str, Any]:
    collection = request.app.state.news_collection
    if collection is None:
        return {"topics": []}
    pipeline = [
        {"$group": {"_id": "$topic", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    results = await collection.aggregate(pipeline).to_list(length=100)
    return {"topics": [r["_id"] for r in results if r["_id"]]}


@router.get("/sources")
@limiter.limit("30/minute")
async def list_sources(request: Request) -> dict[str, Any]:
    collection = request.app.state.news_collection
    if collection is None:
        return {"sources": []}
    pipeline = [
        {"$group": {"_id": {"source": "$source", "source_type": "$source_type"}}},
        {"$sort": {"_id.source": 1}},
    ]
    results = await collection.aggregate(pipeline).to_list(length=500)
    return {
        "sources": [
            {"source": r["_id"]["source"], "source_type": r["_id"]["source_type"]}
            for r in results
        ]
    }
