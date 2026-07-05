"""
backfill_structured_sentiment.py
================================
One-time (repeatable) repair for structured documents stored **without** a
sentiment field.

Why this exists: ``start.ps1`` launched ingestion without ``--sentiment``, so
``MongoHandler`` had no analyzer and wrote rss/sec/fda docs with no sentiment
at all — and because structured writes use ``$setOnInsert``, re-seeing the
item never repairs it. The catalyst ranker aggregates only docs that *have* a
sentiment score, so for much of the corpus the direction signal never reached
the ranker. This script scores the missing docs in place with the same
Loughran-McDonald analyzer live ingestion now uses (start.ps1 passes
``--sentiment`` since this fix).

Run (from the project root, venv Python):
    python backend/backfill_structured_sentiment.py --dry-run   # count only
    python backend/backfill_structured_sentiment.py             # repair
    python backend/backfill_structured_sentiment.py --limit 500 # bounded pass

Idempotent: only docs missing a sentiment score are touched; a second run
reports 0. Writes real scores to real docs — no synthetic data.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

_BACKEND = os.path.dirname(os.path.abspath(__file__))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_STRUCTURED = ("rss", "sec", "fda")
_BATCH = 500


def _load_env() -> None:
    env = os.path.join(os.path.dirname(_BACKEND), ".env")
    if os.path.isfile(env):
        for ln in open(env, encoding="utf-8-sig"):
            if "=" in ln and not ln.strip().startswith("#"):
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main(dry_run: bool, limit: int | None) -> None:
    _load_env()
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        sys.exit("MONGODB_URI is not set (checked environment and .env)")

    from motor.motor_asyncio import AsyncIOMotorClient
    from sentiment import LoughranMcDonaldAnalyzer

    client = AsyncIOMotorClient(uri, tz_aware=True)
    coll = client["financial_news"]["news_items"]
    analyzer = LoughranMcDonaldAnalyzer()

    query = {
        "source_type": {"$in": list(_STRUCTURED)},
        "$or": [
            {"sentiment": None},
            {"sentiment": {"$exists": False}},
            {"sentiment.score": {"$exists": False}},
        ],
    }
    total = await coll.count_documents(query)
    print(f"structured docs missing sentiment: {total}")
    if dry_run or total == 0:
        client.close()
        return

    fixed = 0
    cursor = coll.find(query, {"_id": 0, "content_hash": 1, "title": 1,
                               "description": 1}).limit(limit or 0)
    batch: list[dict] = []
    async for doc in cursor:
        batch.append(doc)
        if len(batch) >= _BATCH:
            fixed += await _score_batch(coll, analyzer, batch)
            batch = []
    if batch:
        fixed += await _score_batch(coll, analyzer, batch)

    print(f"repaired {fixed} docs" + (f" (limit {limit})" if limit else ""))
    remaining = await coll.count_documents(query)
    print(f"still missing after pass: {remaining}")
    client.close()


async def _score_batch(coll, analyzer, docs: list[dict]) -> int:
    results = analyzer.analyze_text_batch(
        [(d.get("title", "") or "", d.get("description", "") or "") for d in docs]
    )
    n = 0
    for d, r in zip(docs, results):
        await coll.update_one(
            {"content_hash": d["content_hash"]},
            {"$set": {"sentiment": {
                "score": round(r.score, 4),
                "label": r.label,
                "confidence": round(r.confidence, 4),
            }}},
        )
        n += 1
    print(f"  … {n} scored (last: {d.get('title', '')[:60]!r} -> {r.label})")
    return n


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    p.add_argument("--limit", type=int, default=None, help="max docs to repair this pass")
    a = p.parse_args()
    asyncio.run(main(a.dry_run, a.limit))
