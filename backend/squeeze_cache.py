"""
squeeze_cache.py
================
Redis hot layer for the squeeze lane — three per-ticker read-through TTL
caches, all best-effort:

  short metrics   squeeze:short:{T}    12h    yfinance short data updates
                                              bi-weekly; the serial per-ticker
                                              lookups dominate run latency
  social          squeeze:social:{T}   15min  softens Bluesky rate limits when
                                              runs land close together
  news slice      squeeze:news:{T}     10min  skips re-querying Mongo between
                                              back-to-back runs; decay/veto are
                                              still evaluated *live* per run so
                                              the time windows stay honest

Pattern copied from ``catalyst_deep_read._GradeCache``: lazy ``redis.asyncio``
client from ``REDIS_URI``, tight socket timeouts, a 5-minute down-backoff after
any failure, and every error path degrades to a direct source call. The cache
can only ever make a run faster — never break it, never change its answer
beyond the declared TTL staleness.

Negative caching: a ticker the source did not return is cached as a sentinel
for the same TTL — "Bluesky had nothing on this name" is exactly the result
worth not re-asking for. News docs revive their ``published_at`` to aware
datetimes on read (news_signal's windows need real datetime objects).

Env tunables:
  SQUEEZE_SHORT_TTL_S    default 43200 (12h)
  SQUEEZE_SOCIAL_TTL_S   default   900 (15min)
  SQUEEZE_NEWS_TTL_S     default   600 (10min)

DATABASE SAFETY: this module writes only *real* fetched market/news data to
Redis under the ``squeeze:`` namespace. Scenario modules do not import it, and
the ranker imports it lazily inside the orchestrator, so synthetic runs cannot
touch it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("squeeze_cache")

_NS = "squeeze"
_SENTINEL = "__absent__"          # negative-cache marker (source had no data)

DEFAULT_SHORT_TTL_S = 43200       # 12h
DEFAULT_SOCIAL_TTL_S = 900        # 15min
DEFAULT_NEWS_TTL_S = 600          # 10min


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class SqueezeCache:
    """Best-effort Redis kv with batch get/set; every failure degrades."""

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
            log.info("squeeze cache unavailable (%s) — uncached", type(exc).__name__)
            self._mark_down()
            return None
        return self._client

    def _mark_down(self) -> None:
        self._client = None
        self._down_until = time.monotonic() + self._BACKOFF_SECONDS

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """{key: decoded value} for present keys; {} on any failure."""
        if not keys:
            return {}
        client = await self._get_client()
        if client is None:
            return {}
        try:
            raw = await client.mget(keys)
        except Exception as exc:  # noqa: BLE001
            log.info("squeeze cache read failed (%s) — uncached", type(exc).__name__)
            self._mark_down()
            return {}
        out: dict[str, Any] = {}
        for k, v in zip(keys, raw):
            if v is None:
                continue
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                continue  # unreadable entry == miss
        return out

    async def set_many(self, items: dict[str, Any], ttl: int) -> None:
        """Pipeline SET..EX for every item; silently a no-op on failure."""
        if not items:
            return
        client = await self._get_client()
        if client is None:
            return
        try:
            pipe = client.pipeline()
            for k, v in items.items():
                pipe.set(k, json.dumps(v, default=str), ex=ttl)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            log.info("squeeze cache write failed (%s)", type(exc).__name__)
            self._mark_down()


_cache = SqueezeCache()   # module singleton, env-driven (matches deep-read)


# --- generic read-through ------------------------------------------------------ #

async def _read_through(
    prefix: str,
    tickers: list[str],
    fetch_fn: Callable[[list[str]], Awaitable[dict[str, Any]]],
    ttl: int,
    *,
    revive: Optional[Callable[[Any], Any]] = None,
    cache: Optional[SqueezeCache] = None,
) -> dict[str, Any]:
    """
    Per-ticker read-through: batch-read ``squeeze:{prefix}:{T}``, fetch only
    the misses via ``fetch_fn(missing)``, write fresh values back (absent
    tickers as negative sentinels). Any cache trouble = plain ``fetch_fn``.
    """
    syms = list(dict.fromkeys(
        t.strip().upper() for t in (tickers or []) if t and t.strip()))
    if not syms:
        return {}
    c = cache if cache is not None else _cache

    keys = {t: f"{_NS}:{prefix}:{t}" for t in syms}
    cached = await c.get_many(list(keys.values()))

    out: dict[str, Any] = {}
    missing: list[str] = []
    for t in syms:
        v = cached.get(keys[t], ...)
        if v is ...:
            missing.append(t)
        elif v == _SENTINEL:
            continue                       # known-empty: don't re-ask the source
        else:
            if revive is not None:
                try:
                    v = revive(v)
                except Exception:  # noqa: BLE001 — revival failure == miss
                    missing.append(t)
                    continue
            out[t] = v

    if missing:
        fetched = await fetch_fn(missing)
        fetched = fetched or {}
        out.update({t: v for t, v in fetched.items() if t in set(missing)})
        await c.set_many(
            {keys[t]: (fetched[t] if t in fetched else _SENTINEL) for t in missing},
            ttl,
        )
    return out


# --- datetime revival for news docs -------------------------------------------- #

def _revive_news_docs(docs: Any) -> list[dict[str, Any]]:
    """JSON round-trip turns published_at into a string; news_signal's decay,
    veto and halt windows need aware datetimes back."""
    revived = []
    for d in docs or []:
        pub = d.get("published_at")
        if isinstance(pub, str):
            d = dict(d, published_at=datetime.fromisoformat(pub))
        revived.append(d)
    return revived


# --- public wrappers ------------------------------------------------------------ #

async def cached_short_metrics(
    tickers: list[str],
    fetch_fn: Callable[[list[str]], Awaitable[dict[str, Any]]],
    *,
    ttl: Optional[int] = None,
    cache: Optional[SqueezeCache] = None,
) -> dict[str, Any]:
    """yfinance short metrics with a 12h TTL — the slow serial calls."""
    return await _read_through(
        "short", tickers, fetch_fn,
        ttl if ttl is not None else _env_int("SQUEEZE_SHORT_TTL_S", DEFAULT_SHORT_TTL_S),
        cache=cache,
    )


async def cached_social(
    tickers: list[str],
    fetch_fn: Callable[[list[str]], Awaitable[dict[str, Any]]],
    *,
    ttl: Optional[int] = None,
    cache: Optional[SqueezeCache] = None,
) -> dict[str, Any]:
    """Bluesky social snapshots with a 15min TTL — rate-limit softener."""
    return await _read_through(
        "social", tickers, fetch_fn,
        ttl if ttl is not None else _env_int("SQUEEZE_SOCIAL_TTL_S", DEFAULT_SOCIAL_TTL_S),
        cache=cache,
    )


async def cached_ticker_news(
    tickers: list[str],
    fetch_fn: Callable[[list[str]], Awaitable[dict[str, Any]]],
    *,
    ttl: Optional[int] = None,
    cache: Optional[SqueezeCache] = None,
) -> dict[str, Any]:
    """Per-ticker structured-news slices with a 10min TTL; datetimes revived
    so news_signal's live decay/veto evaluation sees real timestamps."""
    return await _read_through(
        "news", tickers, fetch_fn,
        ttl if ttl is not None else _env_int("SQUEEZE_NEWS_TTL_S", DEFAULT_NEWS_TTL_S),
        revive=_revive_news_docs,
        cache=cache,
    )
