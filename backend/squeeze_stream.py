"""
squeeze_stream.py
=================
Redis pub/sub publisher for the squeeze lane's real-time feed (Phase 4).

One channel, ``squeeze:updates``, carrying two event shapes:

  {"type": "squeeze_run", ...}   published when a squeeze ranking is saved
                                 (scheduler tick or manual POST /api/squeeze/run):
                                 run id, counts, and a compact top slice with the
                                 flags the dashboard popup needs immediately
                                 (thesis_broken, halt code, direction, scores).

  {"type": "doc", ...}           published per ingested ticker-tagged item by a
                                 dispatcher handler riding the ingestion agents —
                                 the live message-density feed (social velocity
                                 tells) plus wire headlines as they land.

The middleware exposes the channel as an SSE endpoint (GET /api/squeeze/stream);
the frontend subscribes and stops polling.

Degrade-gracefully contract (same as squeeze_cache): lazy ``redis.asyncio``
client from ``REDIS_URI``, tight timeouts, 5-minute down-backoff, and every
failure is swallowed after a log line — publishing is strictly best-effort and
can never slow or break ingestion or a ranking run.

Event builders are pure functions (unit-tested without Redis).

DATABASE SAFETY: pub/sub only — this module never persists anything. Scenario
modules do not import it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("squeeze_stream")

CHANNEL = "squeeze:updates"
_TOP_SLICE = 10          # how many ranked names ride the squeeze_run event
_TITLE_CAP = 140


# --- best-effort publisher ------------------------------------------------------ #

class StreamPublisher:
    """Lazy Redis client + backoff; ``publish`` returns True only on success."""

    _BACKOFF_SECONDS = 300.0

    def __init__(self, uri: Optional[str] = None, client: Any = None) -> None:
        self._uri = uri
        self._client = client          # test injection point
        self._down_until = 0.0

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if time.monotonic() < self._down_until:
            return None
        uri = self._uri or os.environ.get("REDIS_URI")
        if not uri:
            self._down_until = time.monotonic() + self._BACKOFF_SECONDS
            return None
        try:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(
                uri, decode_responses=True,
                socket_timeout=2.0, socket_connect_timeout=2.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("squeeze stream unavailable (%s) — not publishing", type(exc).__name__)
            self._mark_down()
            return None
        return self._client

    def _mark_down(self) -> None:
        self._client = None
        self._down_until = time.monotonic() + self._BACKOFF_SECONDS

    async def publish(self, event: dict[str, Any]) -> bool:
        client = await self._get_client()
        if client is None:
            return False
        try:
            await client.publish(CHANNEL, json.dumps(event, default=str))
            return True
        except Exception as exc:  # noqa: BLE001
            log.info("squeeze stream publish failed (%s)", type(exc).__name__)
            self._mark_down()
            return False


_publisher = StreamPublisher()   # module singleton, env-driven


# --- pure event builders ---------------------------------------------------------- #

def run_summary_event(result: dict[str, Any]) -> dict[str, Any]:
    """Compact ``squeeze_run`` event from a full ranking result — just what the
    popup needs to update instantly; the dashboard fetches the full doc after."""
    top = []
    for it in (result.get("items") or [])[:_TOP_SLICE]:
        top.append({
            "ticker": it.get("ticker"),
            "rank": it.get("rank"),
            "squeeze_score": it.get("squeeze_score"),
            "ignition_score": it.get("ignition_score"),
            "news_ignition": it.get("news_ignition"),
            "direction": it.get("direction"),
            "thesis_broken": bool(it.get("thesis_broken")),
            "halt_code": (it.get("halted") or {}).get("code"),
        })
    gen = result.get("generated_at")
    return {
        "type": "squeeze_run",
        "run_id": result.get("run_id"),
        "generated_at": gen.isoformat() if isinstance(gen, datetime) else gen,
        "fueled_count": result.get("fueled_count"),
        "universe_count": result.get("universe_count"),
        "top": top,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }


def doc_event(
    tickers: tuple[str, ...] | list[str],
    source: str,
    source_type: str,
    title: str,
    published_at: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Per-item ``doc`` event (message-density feed), or None for untagged items
    — an item with no tickers can't move a per-ticker density needle."""
    syms = [t.strip().upper() for t in (tickers or []) if t and t.strip()]
    if not syms:
        return None
    return {
        "type": "doc",
        "tickers": syms,
        "source": source,
        "source_type": source_type,
        "title": (title or "")[:_TITLE_CAP],
        "published_at": published_at.isoformat() if isinstance(published_at, datetime) else None,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }


# --- publish helpers ---------------------------------------------------------- #

async def publish_squeeze_run(
    result: dict[str, Any], *, publisher: Optional[StreamPublisher] = None
) -> bool:
    """Fire a ``squeeze_run`` event; best-effort, never raises."""
    try:
        return await (publisher or _publisher).publish(run_summary_event(result))
    except Exception as exc:  # noqa: BLE001 — belt over the publisher's braces
        log.info("publish_squeeze_run failed (%s)", type(exc).__name__)
        return False


class IngestionStreamHandler:
    """
    Dispatcher-compatible handler (same contract as the storage handlers:
    ``await handler(item)``): publishes a ``doc`` event per ticker-tagged item.
    Register it next to MongoHandler on either ingestion agent. Inert without
    Redis; adds one non-blocking best-effort publish per stored item.
    """

    def __init__(self, publisher: Optional[StreamPublisher] = None, enabled: bool = True) -> None:
        self._publisher = publisher or _publisher
        self.enabled = enabled

    async def __call__(self, item: Any) -> None:
        if not self.enabled:
            return
        try:
            event = doc_event(
                getattr(item, "tickers", ()) or (),
                getattr(item, "source", ""),
                getattr(item, "source_type", ""),
                getattr(item, "title", ""),
                getattr(item, "published_at", None),
            )
            if event is not None:
                await self._publisher.publish(event)
        except Exception as exc:  # noqa: BLE001 — never disturb ingestion
            log.info("ingestion stream publish failed (%s)", type(exc).__name__)

    async def close(self) -> None:   # matches the storage-handler lifecycle
        return None
