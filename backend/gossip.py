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
  gossip_score  — 100·(0.65·velocity_term + 0.35·volume_term), both saturating

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
_BASELINE_FLOOR = 0.5  # min baseline rate; controls how much "from zero" scores


def _direction(score: float) -> str:
    if score >= 0.05:
        return "bullish"
    if score <= -0.05:
        return "bearish"
    return "neutral"


def score_gossip(
    mentions: dict[str, list[tuple[datetime, Optional[float]]]],
    *,
    now: datetime,
    recent_hours: float = 6.0,
    baseline_days: float = 7.0,
    min_recent: int = 3,
    top_k: int = 20,
) -> list[dict[str, Any]]:
    """
    Rank tickers by mention-velocity gossip score.

    ``mentions`` maps ticker -> list of ``(published_at, sentiment|None)``. Pure
    and deterministic; ``now`` is injected so it's testable.
    """
    recent_cut = now - timedelta(hours=recent_hours)
    baseline_start = now - timedelta(days=baseline_days)
    # Length of the baseline period (everything before the recent window), in
    # units of recent-window-lengths, to scale its count to a comparable rate.
    baseline_windows = max(1.0, (baseline_days * 24.0 - recent_hours) / recent_hours)

    ranked: list[dict[str, Any]] = []
    for ticker, posts in mentions.items():
        recent = [(t, s) for t, s in posts if t >= recent_cut]
        recent_count = len(recent)
        if recent_count < min_recent:
            continue
        prior = [t for t, _ in posts if baseline_start <= t < recent_cut]
        baseline_rate = max(len(prior) / baseline_windows, _BASELINE_FLOOR)
        velocity = recent_count / baseline_rate

        sents = [s for _, s in recent if s is not None]
        mean_sent = sum(sents) / len(sents) if sents else 0.0

        vel_term = min(velocity / _VELOCITY_SAT, 1.0)
        vol_term = min(recent_count / _VOLUME_SAT, 1.0)
        gossip_score = round(100.0 * (0.65 * vel_term + 0.35 * vol_term), 2)

        ranked.append({
            "ticker": ticker,
            "recent_count": recent_count,
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
    projection = {"_id": 0, "tickers": 1, "published_at": 1, "sentiment": 1}
    docs = await (
        social_collection.find(query, projection).limit(50_000).to_list(length=50_000)
    )

    mentions: dict[str, list[tuple[datetime, Optional[float]]]] = defaultdict(list)
    for d in docs:
        pub = d.get("published_at")
        if not isinstance(pub, datetime):
            continue
        sent = (d.get("sentiment") or {}).get("score")
        for t in (d.get("tickers") or ()):
            mentions[t].append((pub, sent))

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
