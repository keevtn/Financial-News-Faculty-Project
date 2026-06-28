"""
social_search.py
================
On-demand, *per-ticker* social search for the squeeze ranker.

This is deliberately separate from the continuous social ingestion in
``UnstructuredModule.py``. The squeeze ranker calls ``gather_social`` for a small
set of candidate tickers during its scheduled run — a short burst, not a
firehose — so it never competes with the news-feed pollers (the interference
that plagued the RSS lane). No background tasks, no shared queue.

Sources
-------
Bluesky   — public AppView cashtag search (``$TICKER``). Works from any IP,
            including the hosted deployment. Primary source.
StockTwits— symbol stream with human Bullish/Bearish labels; purpose-built for
            tickers, but Cloudflare-blocks datacenter IPs, so it returns data
            only from a residential IP (local dev). Best-effort: empty in prod,
            never raises. (We do NOT impersonate to bypass the block.)
Twitter   — no free read API; intentionally unimplemented. ``fetch_twitter_cashtag``
            is a stub seam so a Bearer-token adapter can drop in later.

Each source returns plain dicts; the ranker scores sentiment/velocity itself, so
this module stays free of the sentiment/Mongo deps and is easy to test.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("social_search")

# Cashtags in a post ($GME, $TSLA). Used to (a) require the ticker actually
# appears as a cashtag — cutting brand-name collisions ($AMC matched a post about
# an "AMC Matador" car) — and (b) down-weight multi-cashtag spam roundups that
# list 20 tickers at once ("$NNBR $MNTK $JANX ..."), which otherwise inflate the
# mention count of every name simultaneously.
_CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")

_BLUESKY_SEARCH = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
_STOCKTWITS_STREAM = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_HEADERS = {
    "User-Agent": "FinancialNewsDashboard/1.0 (research/non-commercial)",
    "Accept": "application/json",
}
_TIMEOUT = 15


def _parse_dt(raw: Optional[str]) -> datetime:
    """ISO 8601 -> UTC-aware datetime; fall back to now."""
    if not raw:
        return datetime.now(tz=timezone.utc)
    try:
        from dateutil import parser as dp
        dt = dp.parse(raw)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.now(tz=timezone.utc)


def _clean_ticker(ticker: str) -> str:
    return ticker.strip().upper().lstrip("$").replace(".X", "")


# --- Bluesky (primary; works in prod) -------------------------------------- #

async def search_bluesky_cashtag(
    ticker: str, *, limit: int = 50, require_cashtag: bool = True, session: Any = None
) -> list[dict[str, Any]]:
    """
    Recent Bluesky posts mentioning ``$TICKER``. Returns a list of post dicts:
    ``{text, likes, replies, created_at(datetime), handle, n_cashtags}``.

    With ``require_cashtag`` (default), only posts whose text actually contains
    the literal ``$TICKER`` are kept — Bluesky's search matches loosely, so this
    drops brand-name collisions. ``n_cashtags`` is the count of distinct cashtags
    in the post so the caller can down-weight multi-ticker spam. Empty (never
    raises) on any failure, so one bad ticker can't sink a run.
    """
    sym = _clean_ticker(ticker)
    if not sym:
        return []
    try:
        import aiohttp
    except ImportError:
        return []
    owns = session is None
    if owns:
        session = aiohttp.ClientSession(headers=_HEADERS)
    posts: list[dict[str, Any]] = []
    try:
        params = {"q": f"${sym}", "limit": max(1, min(limit, 100))}
        async with session.get(_BLUESKY_SEARCH, params=params, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                log.debug("bluesky cashtag %s HTTP %s", sym, resp.status)
                return []
            data = await resp.json(content_type=None)
        for p in data.get("posts", []):
            rec = p.get("record", {})
            text = (rec.get("text") or "").strip()
            if not text:
                continue
            cashtags = {c.upper() for c in _CASHTAG_RE.findall(text)}
            if require_cashtag and f"${sym}" not in cashtags:
                continue
            posts.append({
                "text": text,
                "likes": p.get("likeCount") or 0,
                "replies": p.get("replyCount") or 0,
                "created_at": _parse_dt(rec.get("createdAt")),
                "handle": (p.get("author") or {}).get("handle", ""),
                "n_cashtags": max(1, len(cashtags)),
            })
    except Exception as exc:  # noqa: BLE001
        log.debug("bluesky cashtag %s failed: %s", sym, type(exc).__name__)
    finally:
        if owns:
            await session.close()
    return posts


# --- StockTwits (best-effort; residential only) ---------------------------- #

async def fetch_stocktwits_symbol(
    ticker: str, *, session: Any = None
) -> list[dict[str, Any]]:
    """
    Recent StockTwits messages for ``TICKER`` with human sentiment labels.
    Returns ``{text, sentiment("Bullish"/"Bearish"/None), likes, created_at}``.
    Cloudflare-blocked from datacenter IPs → empty in prod; never raises.
    """
    sym = _clean_ticker(ticker)
    if not sym:
        return []
    try:
        import aiohttp
    except ImportError:
        return []
    owns = session is None
    if owns:
        session = aiohttp.ClientSession(headers=_HEADERS)
    out: list[dict[str, Any]] = []
    try:
        url = _STOCKTWITS_STREAM.format(ticker=sym)
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                log.debug("stocktwits %s HTTP %s", sym, resp.status)
                return []
            data = await resp.json(content_type=None)
        for msg in data.get("messages", []):
            body = (msg.get("body") or "").strip()
            if not body:
                continue
            entities = msg.get("entities") or {}
            sentiment = (entities.get("sentiment") or {}).get("basic")  # Bullish/Bearish/None
            out.append({
                "text": body,
                "sentiment": sentiment,
                "likes": (msg.get("likes") or {}).get("total", 0),
                "created_at": _parse_dt(msg.get("created_at")),
            })
    except Exception as exc:  # noqa: BLE001
        log.debug("stocktwits %s failed: %s", sym, type(exc).__name__)
    finally:
        if owns:
            await session.close()
    return out


# --- Twitter (stub seam; no free read API) --------------------------------- #

async def fetch_twitter_cashtag(ticker: str, *, session: Any = None) -> list[dict[str, Any]]:
    """
    Placeholder. X has no free read API; per-ticker cashtag search needs a paid
    Bearer token. Wire the v2 recent-search endpoint here when one is available
    (read TWITTER_BEARER_TOKEN, GET /2/tweets/search/recent?query=$TICKER).
    Returns [] so callers stay source-agnostic.
    """
    return []


# --- Aggregate per ticker -------------------------------------------------- #

async def gather_social(
    tickers: list[str],
    *,
    bluesky_limit: int = 50,
    use_stocktwits: bool = False,
    pace: float = 0.4,
) -> dict[str, dict[str, Any]]:
    """
    Per-ticker social snapshot for the squeeze ranker. Sequential with light
    pacing (``pace`` s between tickers) to stay polite to the public APIs — a
    background scheduled burst over a few dozen names, so latency is fine and
    rate-limit safety matters more.

    ``use_stocktwits`` defaults OFF: StockTwits Cloudflare-blocks our (clean,
    non-impersonating) aiohttp requests by TLS fingerprint even from residential
    IPs, so it contributes nothing — the seam stays for a future sanctioned path.

    Returns ``{TICKER: {n_posts, focus_score, engagement, texts, created_ats,
    st_bullish, st_bearish, sources}}`` for tickers with relevant posts.
    ``focus_score`` = Σ 1/n_cashtags per post: a single-ticker post counts ~1.0,
    a 20-ticker spam roundup ~0.05 — the spam-robust attention measure.
    """
    syms = [s for s in dict.fromkeys(_clean_ticker(t) for t in tickers) if s]
    out: dict[str, dict[str, Any]] = {}
    try:
        import aiohttp
    except ImportError:
        return out
    async with aiohttp.ClientSession(headers=_HEADERS) as session:
        for i, sym in enumerate(syms):
            bsky = await search_bluesky_cashtag(sym, limit=bluesky_limit, session=session)
            st = await fetch_stocktwits_symbol(sym, session=session) if use_stocktwits else []

            texts = [p["text"] for p in bsky] + [m["text"] for m in st]
            if not texts:
                if i < len(syms) - 1:
                    await asyncio.sleep(pace)
                continue
            focus = sum(1.0 / p["n_cashtags"] for p in bsky) + float(len(st))  # st has no cashtag noise
            engagement = sum(p["likes"] + p["replies"] for p in bsky) + sum(m["likes"] for m in st)
            created_ats = [p["created_at"] for p in bsky] + [m["created_at"] for m in st]
            sources = (["bluesky"] if bsky else []) + (["stocktwits"] if st else [])
            # A few representative posts (most-engaged first) for display, JSON-safe.
            top_posts = [
                {"text": p["text"][:280], "likes": p["likes"], "replies": p["replies"],
                 "handle": p["handle"], "created_at": p["created_at"].isoformat()}
                for p in sorted(bsky, key=lambda p: p["likes"] + p["replies"], reverse=True)[:3]
            ]
            out[sym] = {
                "ticker": sym,
                "n_posts": len(texts),
                "focus_score": round(focus, 3),
                "engagement": engagement,
                "texts": texts,
                "created_ats": created_ats,
                "top_posts": top_posts,
                "st_bullish": sum(1 for m in st if m["sentiment"] == "Bullish"),
                "st_bearish": sum(1 for m in st if m["sentiment"] == "Bearish"),
                "sources": sources,
            }
            if i < len(syms) - 1:
                await asyncio.sleep(pace)
    return out
