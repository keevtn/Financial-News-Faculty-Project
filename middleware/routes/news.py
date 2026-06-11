from __future__ import annotations

import re
from datetime import datetime
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


@router.get("/")
@limiter.limit("60/minute")
async def list_news(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500, description="Max items to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    source_type: Optional[str] = Query(default=None, description="Filter: rss | sec | fda"),
    topic: Optional[str] = Query(default=None, description="Filter by topic label"),
    search: Optional[str] = Query(default=None, description="Keyword search in title + description"),
) -> dict[str, Any]:
    collection = request.app.state.news_collection
    if collection is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    query: dict[str, Any] = {}
    if source_type:
        query["source_type"] = source_type
    if topic:
        # re.escape prevents ReDoS from user-supplied regex metacharacters
        query["topic"] = {"$regex": re.escape(topic), "$options": "i"}
    if search:
        safe_search = re.escape(search)
        query["$or"] = [
            {"title": {"$regex": safe_search, "$options": "i"}},
            {"description": {"$regex": safe_search, "$options": "i"}},
        ]

    cursor = (
        collection.find(query, {"_id": 0})
        .sort("published_at", -1)
        .skip(offset)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    total = await collection.count_documents(query)

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
