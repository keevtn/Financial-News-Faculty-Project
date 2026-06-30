"""
gossip.py
=========
Gossip detection via **rolling-window mention velocity** over the social stream.

"Gossip" = a ticker whose social chatter is *accelerating* — being mentioned far
more in the last few hours than its own trailing baseline. That acceleration is
the early tell the squeeze ranker's one-shot ignition snapshot can't see, and a
useful standalone surface ("what's suddenly being talked about").

Signal
------
For each ticker, over the stored social posts (Bluesky etc. in the news
collection, source_type="social"):

  recent_count  — mentions in the last ``recent_hours``
  baseline_rate — mentions/recent-window over the trailing ``baseline_days``
                  (the period *before* the recent window), floored so a name
                  emerging from zero still scores
  velocity      — recent_count / baseline_rate  (>1 = above normal)
  breadth       — distinct authors in the recent window (network propagation:
                  many people talking, not one account repeating)
  gossip_score  — 100·(0.50·velocity + 0.20·volume + 0.30·breadth), all saturating

Only names with >= ``min_recent`` recent mentions qualify (kills 1-vs-0 noise).

``score_gossip`` is pure (no Mongo) and unit-tested; ``detect_gossip`` adds the
query. Computed live per request (cheap) — no scheduler, no persistence.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("gossip")

_VELOCITY_SAT = 5.0    # 5x baseline -> max acceleration term
_VOLUME_SAT = 15.0     # 15 recent mentions -> max volume term
_BREADTH_SAT = 6.0     # 6 distinct authors -> max propagation term
_BASELINE_FLOOR = 0.5  # min baseline rate; controls how much "from zero" scores


def _direction(score: float) -> str:
    if score >= 0.05:
        return "bullish"
    if score <= -0.05:
        return "bearish"
    return "neutral"


def _velocity_for(
    times: list[datetime], now: datetime, recent_hours: float, baseline_days: float
) -> tuple[int, float, float]:
    """(recent_count, baseline_rate, velocity) for one ticker's mention times.

    baseline_rate = mentions/recent-window over the period *before* the recent
    window, floored so a name emerging from zero still yields a high velocity."""
    recent_cut = now - timedelta(hours=recent_hours)
    baseline_start = now - timedelta(days=baseline_days)
    baseline_windows = max(1.0, (baseline_days * 24.0 - recent_hours) / recent_hours)
    recent_count = sum(1 for t in times if t >= recent_cut)
    prior = sum(1 for t in times if baseline_start <= t < recent_cut)
    baseline_rate = max(prior / baseline_windows, _BASELINE_FLOOR)
    return recent_count, round(baseline_rate, 3), recent_count / baseline_rate


def score_gossip(
    mentions: dict[str, list[tuple[datetime, Optional[float], Optional[str]]]],
    *,
    now: datetime,
    recent_hours: float = 6.0,
    baseline_days: float = 7.0,
    min_recent: int = 3,
    top_k: int = 20,
) -> list[dict[str, Any]]:
    """
    Rank tickers by a mention-velocity + **breadth** gossip score.

    ``mentions`` maps ticker -> list of ``(published_at, sentiment|None, author|None)``.
    Breadth (distinct recent authors) is the network-effect/propagation term: an
    idea reaching many people is spreading; the same count from a few accounts is
    spam. Pure and deterministic; ``now`` is injected so it's testable.
    """
    recent_cut = now - timedelta(hours=recent_hours)

    ranked: list[dict[str, Any]] = []
    for ticker, posts in mentions.items():
        recent_count, baseline_rate, velocity = _velocity_for(
            [p[0] for p in posts], now, recent_hours, baseline_days
        )
        if recent_count < min_recent:
            continue
        recent = [p for p in posts if p[0] >= recent_cut]
        sents = [p[1] for p in recent if p[1] is not None]
        mean_sent = sum(sents) / len(sents) if sents else 0.0
        breadth = len({p[2] for p in recent if len(p) > 2 and p[2]})

        vel_term = min(velocity / _VELOCITY_SAT, 1.0)
        vol_term = min(recent_count / _VOLUME_SAT, 1.0)
        breadth_term = min(breadth / _BREADTH_SAT, 1.0)
        gossip_score = round(100.0 * (0.50 * vel_term + 0.20 * vol_term + 0.30 * breadth_term), 2)

        ranked.append({
            "ticker": ticker,
            "recent_count": recent_count,
            "breadth": breadth,
            "baseline_rate": round(baseline_rate, 3),
            "velocity": round(velocity, 2),
            "mean_sentiment": round(mean_sent, 4),
            "direction": _direction(mean_sent),
            "gossip_score": gossip_score,
        })

    ranked.sort(key=lambda r: r["gossip_score"], reverse=True)
    for rank, r in enumerate(ranked[:top_k], start=1):
        r["rank"] = rank
    return ranked[:top_k]


async def detect_gossip(
    social_collection: Any,
    *,
    now: Optional[datetime] = None,
    recent_hours: float = 6.0,
    baseline_days: float = 7.0,
    min_recent: int = 3,
    top_k: int = 20,
) -> dict[str, Any]:
    """
    Pull social posts over the baseline window, group mentions per ticker, and
    rank by gossip score. Degrades to an empty list if there's no social data.
    """
    now = now or datetime.now(tz=timezone.utc)
    start = now - timedelta(days=baseline_days)
    query = {"published_at": {"$gte": start, "$lte": now}, "source_type": "social"}
    projection = {"_id": 0, "tickers": 1, "published_at": 1, "sentiment": 1,
                  "extra.bsky_handle": 1, "extra.st_user": 1}
    docs = await (
        social_collection.find(query, projection).limit(50_000).to_list(length=50_000)
    )

    mentions: dict[str, list[tuple[datetime, Optional[float], Optional[str]]]] = defaultdict(list)
    for d in docs:
        pub = d.get("published_at")
        if not isinstance(pub, datetime):
            continue
        sent = (d.get("sentiment") or {}).get("score")
        extra = d.get("extra") or {}
        author = extra.get("bsky_handle") or extra.get("st_user")  # who posted it
        for t in (d.get("tickers") or ()):
            mentions[t].append((pub, sent, author))

    items = score_gossip(
        mentions, now=now, recent_hours=recent_hours,
        baseline_days=baseline_days, min_recent=min_recent, top_k=top_k,
    )
    return {
        "generated_at": now,
        "params": {"recent_hours": recent_hours, "baseline_days": baseline_days,
                   "min_recent": min_recent},
        "ticker_count": len(mentions),
        "post_count": len(docs),
        "items": items,
    }


async def fetch_velocities(
    social_collection: Any,
    tickers: list[str],
    *,
    now: Optional[datetime] = None,
    recent_hours: float = 6.0,
    baseline_days: float = 7.0,
) -> dict[str, float]:
    """
    ``{ticker: velocity}`` for specific tickers — the squeeze ranker uses this to
    fold mention *acceleration* into its ignition. 0.0 for tickers with no recent
    social. Never raises (-> {} on failure / no collection).
    """
    syms = [t.strip().upper() for t in dict.fromkeys(tickers) if t and t.strip()]
    if social_collection is None or not syms:
        return {}
    now = now or datetime.now(tz=timezone.utc)
    start = now - timedelta(days=baseline_days)
    query = {
        "published_at": {"$gte": start, "$lte": now},
        "source_type": "social",
        "tickers": {"$in": syms},
    }
    try:
        docs = await (
            social_collection.find(query, {"_id": 0, "tickers": 1, "published_at": 1})
            .limit(50_000).to_list(length=50_000)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("velocity fetch failed: %s", type(exc).__name__)
        return {}

    wanted = set(syms)
    times: dict[str, list[datetime]] = defaultdict(list)
    for d in docs:
        pub = d.get("published_at")
        if not isinstance(pub, datetime):
            continue
        for t in (d.get("tickers") or ()):
            if t in wanted:
                times[t].append(pub)

    out: dict[str, float] = {}
    for t in syms:
        _, _, vel = _velocity_for(times.get(t, []), now, recent_hours, baseline_days)
        out[t] = round(vel, 2)
    return out
