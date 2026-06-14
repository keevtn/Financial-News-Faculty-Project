"""
storage_handlers.py
====================
Redis + MongoDB persistence layer for the financial-news ingestion agent.

It adds two downstream dispatcher handlers that conform to the existing
``Handler`` protocol used by ``DispatchRouter``::

    async def __call__(self, item: NewsItem) -> None

Because they hang off the dispatcher, items reach them only AFTER the agent's
dedup cache and keyword filter have already run — no extractor changes needed.

  • MongoHandler — durable archive of every NewsItem (the full history you
                   query later).  Idempotent upsert on content_hash.
  • RedisHandler — rolling, TTL-bounded recency-sentiment store for "what is
                   the mood about X right now?" style reads.  Each entry now
                   stores a bearish / bullish / neutral label alongside the
                   continuous score.

Connections are opened LAZILY on first dispatch.  This is safe because the
agent's ``_dispatch_loop`` drains the queue sequentially (one await at a time),
so there is no concurrent first-call race.  Call ``close()`` on shutdown.

Dependencies
------------
    pip install motor "redis>=4.2"

Sentiment dependencies (install the one you choose — see sentiment.py):
    pip install transformers torch   # FinBERTAnalyzer   (recommended)
    pip install vaderSentiment       # VaderSentimentAnalyzer (fallback)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from IngestionModule import NewsItem
from sentiment import SentimentAnalyzer, FinBERTAnalyzer, LoughranMcDonaldAnalyzer, SentimentResult

log = logging.getLogger("ingestion_agent.storage")


# ---------------------------------------------------------------------------
# Optional third-party imports (guarded so this module imports even if a
# backend isn't installed yet — the relevant handler simply errors on use).
# ---------------------------------------------------------------------------

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

# ---------------------------------------------------------------------------
# MongoDB — durable, queryable archive of everything ingested
# ---------------------------------------------------------------------------

class MongoHandler:
    """
    Persists each NewsItem as a document in MongoDB.

    The collection is keyed on ``content_hash`` (the same stable hash the agent
    uses for dedup), so writes are idempotent across restarts: the same article
    re-seen later will not create a second document.

    Parameters
    ----------
    uri:
        MongoDB connection string.
    db_name / collection_name:
        Target database and collection.
    enabled:
        Set False to make the handler a no-op without unregistering it.
    """

    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        db_name: str = "financial_news",
        collection_name: str = "news_items",
        enabled: bool = True,
        analyzer: Optional[SentimentAnalyzer] = None,
    ) -> None:
        self._uri = uri
        self._db_name = db_name
        self._collection_name = collection_name
        self.enabled = enabled
        self._analyzer = analyzer
        self._client: Optional[Any] = None
        self._collection: Optional[Any] = None
        # Lazy-loaded fast analyzer for social items — avoids FinBERT latency
        self._fast_analyzer: Optional[LoughranMcDonaldAnalyzer] = None

    async def _connect(self) -> None:
        if AsyncIOMotorClient is None:
            raise RuntimeError("motor is not installed; run: pip install motor")
        # tz_aware so datetimes round-trip as UTC-aware on read.
        self._client = AsyncIOMotorClient(self._uri, tz_aware=True)
        db = self._client[self._db_name]
        self._collection = db[self._collection_name]
        # Indexes: unique dedup key + time/source query patterns.
        await self._collection.create_index("content_hash", unique=True)
        await self._collection.create_index("published_at")
        await self._collection.create_index(
            [("source_type", 1), ("published_at", -1)]
        )
        log.info(
            "MongoHandler connected — %s.%s",
            self._db_name, self._collection_name,
        )

    @staticmethod
    def _to_document(item: NewsItem) -> dict[str, Any]:
        return {
            "content_hash": item.content_hash,
            "source": item.source,
            "source_type": item.source_type,
            "title": item.title,
            # Native datetime -> BSON date, so $gte/$lte range queries work.
            "published_at": item.published_at,
            "description": item.description,
            "url": item.url,
            "topic": item.topic,
            "tickers": list(item.tickers),
            "extra": item.extra,
            "ingested_at": datetime.now(tz=timezone.utc),
        }

    def _score_social(self, item: NewsItem) -> SentimentResult:
        """
        Fast-path sentiment for social items.

        Priority:
          1. StockTwits human label (already crowd-sourced; ~0 ms)
          2. Loughran-McDonald keyword scorer (~1 ms, no GPU needed)

        FinBERT is never used here — social ingestion must stay low-latency.
        """
        st = item.extra.get("st_sentiment")
        if st == "Bullish":
            return SentimentResult(score=0.5, label="bullish", confidence=0.8)
        if st == "Bearish":
            return SentimentResult(score=-0.5, label="bearish", confidence=0.8)
        if self._fast_analyzer is None:
            self._fast_analyzer = LoughranMcDonaldAnalyzer()
        return self._fast_analyzer.analyze(item)

    async def __call__(self, item: NewsItem) -> None:
        if not self.enabled:
            return
        if self._collection is None:
            await self._connect()

        doc = self._to_document(item)
        if item.source_type == "social":
            # Unstructured protocol: fast assumptions only, no blocking inference
            result = self._score_social(item)
            doc["sentiment"] = {
                "score": round(result.score, 4),
                "label": result.label,
                "confidence": round(result.confidence, 4),
            }
        elif self._analyzer is not None:
            result = await asyncio.to_thread(self._analyzer.analyze, item)
            doc["sentiment"] = {
                "score": round(result.score, 4),
                "label": result.label,
                "confidence": round(result.confidence, 4),
            }

        try:
            # $setOnInsert => first write wins; re-seen items are left untouched.
            await self._collection.update_one(
                {"content_hash": doc["content_hash"]},
                {"$setOnInsert": doc},
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("MongoHandler write failed: %s", exc)

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._collection = None


# ---------------------------------------------------------------------------
# Redis — rolling recency-sentiment store
# ---------------------------------------------------------------------------

class RedisHandler:
    """
    Scores each item's sentiment and records it in time-windowed Redis sorted
    sets so the dashboard can ask "what's the recent mood about X?".

    Storage model
    --------------
    For every item we compute a sentiment float and write it into one sorted
    set per *scope*:
        sentiment:z:global
        sentiment:z:type:<source_type>
        sentiment:z:source:<source>
        sentiment:z:kw:<keyword>        (for each tracked keyword that matches)

    In each sorted set:
        score  = unix timestamp  (enables time-range trimming/reads)
        member = JSON {h: content_hash, s: sentiment, t: title, u: url}

    Recency is enforced two ways on every write:
        • ZREMRANGEBYSCORE drops entries older than ``window_seconds``
        • EXPIRE refreshes the key TTL so idle scopes self-clean

    A compact "latest" string per scope and a pub/sub broadcast are also
    maintained for live consumers.

    Parameters
    ----------
    url:
        Redis connection URL.
    analyzer:
        A SentimentAnalyzer.  If None, scoring is skipped (see __call__).
    window_seconds:
        Recency window (default 1 hour).
    track_keywords:
        Lowercased terms to maintain per-topic sentiment for; usually pass the
        agent's FILTER_KEYWORDS.
    namespace:
        Redis key prefix.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        analyzer: Optional[SentimentAnalyzer] = None,
        window_seconds: int = 3600,
        track_keywords: Optional[list[str]] = None,
        namespace: str = "sentiment",
        enabled: bool = True,
    ) -> None:
        self._url = url
        self._analyzer = analyzer
        self._window = int(window_seconds)
        self._track_keywords = [k.lower() for k in (track_keywords or [])]
        self._ns = namespace
        self.enabled = enabled
        self._redis: Optional[Any] = None
        self._warned_no_analyzer = False

    async def _connect(self) -> None:
        if aioredis is None:
            raise RuntimeError("redis is not installed; run: pip install 'redis>=4.2'")
        self._redis = aioredis.from_url(self._url, decode_responses=True)
        await self._redis.ping()
        # Mask any password in the URL before logging
        safe_url = re.sub(r"(:)[^:@]+(@)", r"\1***\2", self._url)
        log.info("RedisHandler connected — %s", safe_url)

    def _scopes(self, item: NewsItem) -> list[str]:
        scopes = ["global", f"type:{item.source_type}", f"source:{item.source}"]
        haystack = (item.title + " " + item.description).lower()
        for kw in self._track_keywords:
            if kw in haystack:
                scopes.append(f"kw:{kw}")
        return scopes

    async def __call__(self, item: NewsItem) -> None:
        if not self.enabled:
            return
        if self._redis is None:
            await self._connect()

        if self._analyzer is None:
            if not self._warned_no_analyzer:
                log.warning("RedisHandler has no analyzer; skipping sentiment writes")
                self._warned_no_analyzer = True
            return

        # Run the (potentially slow) model in a thread so FinBERT inference
        # doesn't block the event loop.  Fast models (VADER, LM) pay only the
        # negligible thread-dispatch overhead.
        result = await asyncio.to_thread(self._analyzer.analyze, item)
        now = time.time()
        cutoff = now - self._window
        member = json.dumps({
            "h": item.content_hash,
            "s": round(result.score, 4),
            "l": result.label,            # "bullish" | "bearish" | "neutral"
            "c": round(result.confidence, 4),
            "t": item.title[:120],
            "u": item.url,
        })

        try:
            pipe = self._redis.pipeline()
            for scope in self._scopes(item):
                zkey = f"{self._ns}:z:{scope}"
                pipe.zadd(zkey, {member: now})            # add this reading
                pipe.zremrangebyscore(zkey, 0, cutoff)    # trim stale readings
                pipe.expire(zkey, self._window + 60)      # refresh TTL
                pipe.set(                                  # quick "latest" read
                    f"{self._ns}:latest:{scope}",
                    json.dumps({
                        "s": round(result.score, 4),
                        "l": result.label,
                        "t": now,
                    }),
                    ex=self._window + 60,
                )
            pipe.publish(f"{self._ns}:events", member)    # live broadcast
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            log.error("RedisHandler write failed: %s", exc)

    async def recent_sentiment(
        self,
        scope: str = "global",
        window_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Aggregate recent sentiment for a scope.

        ``scope`` is one of: "global", "type:<x>", "source:<x>", "kw:<x>".

        Returns
        -------
        count           : total items in the window
        mean/min/max    : continuous score statistics (negative = bearish)
        label_counts    : {"bullish": n, "bearish": n, "neutral": n}
        dominant_label  : whichever of the three has the highest count
        window_seconds  : the window that was queried
        """
        if self._redis is None:
            await self._connect()
        window = window_seconds or self._window
        zkey = f"{self._ns}:z:{scope}"
        cutoff = time.time() - window

        raw = await self._redis.zrangebyscore(zkey, cutoff, "+inf")
        scores: list[float] = []
        label_counts: dict[str, int] = {"bullish": 0, "bearish": 0, "neutral": 0}

        for m in raw:
            try:
                data = json.loads(m)
                scores.append(float(data["s"]))
                label = data.get("l", "neutral")
                label_counts[label] = label_counts.get(label, 0) + 1
            except Exception:  # noqa: BLE001
                continue

        if not scores:
            return {
                "scope": scope,
                "count": 0,
                "mean": None,
                "min": None,
                "max": None,
                "label_counts": label_counts,
                "dominant_label": None,
                "window_seconds": window,
            }
        return {
            "scope": scope,
            "count": len(scores),
            "mean": round(sum(scores) / len(scores), 4),
            "min": min(scores),
            "max": max(scores),
            "label_counts": label_counts,
            "dominant_label": max(label_counts, key=label_counts.__getitem__),
            "window_seconds": window,
        }

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()   # redis-py >= 5
            except AttributeError:
                await self._redis.close()     # older redis-py
            self._redis = None


# ---------------------------------------------------------------------------
# Convenience wiring
# ---------------------------------------------------------------------------

def attach_storage(
    agent: Any,
    *,
    enable_mongo: bool = True,
    mongo_kwargs: Optional[dict[str, Any]] = None,
    enable_redis: bool = False,
    analyzer: Optional[SentimentAnalyzer] = None,
    redis_kwargs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Build, register, and return storage handlers for an IngestionAgent.

    Returns a dict like {"mongo": MongoHandler, "redis": RedisHandler} so the
    caller can call .close() on each at shutdown.
    """
    handlers: dict[str, Any] = {}

    if enable_mongo:
        h = MongoHandler(analyzer=analyzer, **(mongo_kwargs or {}))
        agent.dispatcher.register(h)
        handlers["mongo"] = h

    if enable_redis:
        if analyzer is None:
            analyzer = FinBERTAnalyzer()
        h = RedisHandler(analyzer=analyzer, **(redis_kwargs or {}))
        agent.dispatcher.register(h)
        handlers["redis"] = h

    return handlers


# ---------------------------------------------------------------------------
# Example wiring (adapts your existing runner)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    from IngestionModule import IngestionAgent

    async def main() -> None:
        agent = IngestionAgent(rss_poll_interval=60)

        storage = attach_storage(
            agent,
            enable_mongo=True,
            mongo_kwargs={"uri": os.environ["MONGODB_URI"]},
        )

        await agent.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await agent.stop()
            for h in storage.values():
                await h.close()

    asyncio.run(main())