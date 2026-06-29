"""
catalyst_universe.py
====================
Continuously builds a **candidate universe** of potential tickers from the news
stream, plus an incremental **weight auto-tuner** for the catalyst pre-score.

Why this exists
---------------
The standard catalyst ranker (catalyst_ranker.py) is a once-per-trading-day
snapshot that enforces a volume floor (``min_sources >= 2``) and a ``top_k`` cut,
so **marginal tickers — ones that don't (yet) fit the standard model — are
discarded**. This module runs on a slower cadence (default 12h) and instead
*accumulates* those sub-threshold names over time, so the dashboard can surface a
growing watchlist of emerging candidates.

Two design rules from the spec:

1. **Only new data each cycle.** Each run processes only documents not already
   counted — already-seen data is withheld, never re-fed. Robustness comes from
   ``content_hash`` de-duplication (a bounded recent-hash set in the meta doc)
   plus a small lookback overlap, so a strict timestamp boundary can't miss
   late-arriving RSS items or double-count on overlap.
2. **Auto-tune, conservatively.** ``auto_tune`` nudges the pre-score weights
   toward the backtest's suggestion (blend, not replace) and only once enough
   graded runs exist — incremental learning, fully reversible.

Everything except the Mongo I/O is a pure function, so the logic is unit-testable
without a database or network. The pure helpers (``select_new_docs``,
``build_universe_features``, ``merge_candidate``, ``blend_weights``,
``prune_seen``) are reused by both the scheduler and the tests.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import catalyst_backtest
from catalyst_backtest import DEFAULT_WEIGHTS
from catalyst_ranker import (
    _cluster,
    _direction_from_sentiment,
    _fetch_window_docs,
    _representative_articles,
)

log = logging.getLogger("catalyst_universe")

# A ticker "graduates" out of the loose lane once it accumulates standard-model
# evidence: enough independent sources AND enough independent stories.
PROMOTE_MIN_SOURCES = 2
PROMOTE_MIN_STORIES = 2

# How long a processed content_hash is remembered for de-duplication. Bounds the
# meta doc's seen-hash set to ~the docs ingested in this window.
SEEN_RETENTION_HOURS = 72


# --- Pure helpers ---------------------------------------------------------- #

def select_new_docs(
    docs: list[dict[str, Any]], seen_hashes: Any
) -> list[dict[str, Any]]:
    """Keep only docs whose ``content_hash`` hasn't been counted yet.

    ``seen_hashes`` may be a dict (hash -> iso) or any iterable of hashes; a doc
    with no hash is treated as new (can't be deduped).
    """
    seen = set(seen_hashes or ())
    return [d for d in docs if not (d.get("content_hash") and d["content_hash"] in seen)]


def build_universe_features(
    new_docs: list[dict[str, Any]], *, ticker_extractor: Any = None
) -> dict[str, dict[str, Any]]:
    """
    Per-ticker features for *this cycle's* new docs (pure). Mirrors the ranker's
    feature stage but tracks sentiment as a sum+count (so cumulative means stay
    correct across cycles) and carries the contributing ``content_hash``es.
    """
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in new_docs:
        if ticker_extractor is not None:
            tickers = list(
                ticker_extractor.extract(doc.get("title", ""), doc.get("description", ""))
            )
        else:
            tickers = list(doc.get("tickers") or ())
        for t in tickers:
            by_ticker[t].append(doc)

    out: dict[str, dict[str, Any]] = {}
    for ticker, t_docs in by_ticker.items():
        clusters = _cluster(t_docs)
        sources = sorted({d.get("source", "") for d in t_docs if d.get("source")})
        source_types = sorted({d.get("source_type", "rss") for d in t_docs})
        scores = [
            float(d["sentiment"]["score"])
            for d in t_docs
            if isinstance(d.get("sentiment"), dict) and d["sentiment"].get("score") is not None
        ]
        out[ticker] = {
            "n_docs": len(t_docs),
            "n_stories": len(clusters),
            "sources": sources,
            "source_types": source_types,
            "sent_sum": round(sum(scores), 6),
            "sent_n": len(scores),
            "sample_articles": _representative_articles(clusters),
            "hashes": [d.get("content_hash", "") for d in t_docs if d.get("content_hash")],
        }
    return out


def merge_candidate(
    existing: Optional[dict[str, Any]],
    cycle: dict[str, Any],
    *,
    now: datetime,
    promote_min_sources: int = PROMOTE_MIN_SOURCES,
    promote_min_stories: int = PROMOTE_MIN_STORIES,
) -> dict[str, Any]:
    """
    Fold one cycle's features into a ticker's accumulated universe doc (pure).

    Cumulative ``n_sources`` is the union of distinct source labels over all time
    (true independent breadth); ``n_stories``/``n_docs`` are additive (coverage
    volume). ``promoted`` flips on once cumulative breadth crosses the standard bar.
    """
    existing = existing or {}
    n_docs = int(existing.get("n_docs", 0)) + int(cycle["n_docs"])
    n_stories = int(existing.get("n_stories", 0)) + int(cycle["n_stories"])
    sources = sorted(set(existing.get("sources", [])) | set(cycle["sources"]))
    source_types = sorted(set(existing.get("source_types", [])) | set(cycle["source_types"]))
    sent_sum = round(float(existing.get("sent_sum", 0.0)) + float(cycle["sent_sum"]), 6)
    sent_n = int(existing.get("sent_n", 0)) + int(cycle["sent_n"])
    mean_sentiment = round(sent_sum / sent_n, 4) if sent_n else 0.0
    n_sources = len(sources)
    promoted = n_sources >= promote_min_sources and n_stories >= promote_min_stories
    return {
        "n_docs": n_docs,
        "n_stories": n_stories,
        "n_sources": n_sources,
        "sources": sources,
        "source_types": source_types,
        "sent_sum": sent_sum,
        "sent_n": sent_n,
        "mean_sentiment": mean_sentiment,
        "direction": _direction_from_sentiment(mean_sentiment),
        "cycles": int(existing.get("cycles", 0)) + 1,
        "first_seen": existing.get("first_seen") or now,
        "last_seen": now,
        "promoted": promoted,
        # keep the freshest representative articles; fall back to prior ones
        "sample_articles": cycle.get("sample_articles") or existing.get("sample_articles", []),
    }


def prune_seen(
    seen: Any, *, now: datetime, retention_hours: int = SEEN_RETENTION_HOURS
) -> dict[str, str]:
    """Drop processed-hash entries older than the retention window (pure)."""
    cutoff = now - timedelta(hours=retention_hours)
    pruned: dict[str, str] = {}
    for h, iso in (seen or {}).items():
        try:
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001 — undated/garbage entry: keep it this cycle
            ts = now
        if ts >= cutoff:
            pruned[h] = iso
    return pruned


def blend_weights(
    current: dict[str, float], suggested: dict[str, float], *, blend: float = 0.2
) -> dict[str, float]:
    """
    Nudge weights toward ``suggested`` by ``blend`` and renormalize to sum 1.0
    (pure). Conservative by design — a small step, not a destructive re-fit.
    """
    keys = list(DEFAULT_WEIGHTS.keys())
    cur = {k: float(current.get(k, DEFAULT_WEIGHTS[k])) for k in keys}
    sug = {k: float(suggested.get(k, cur[k])) for k in keys}
    mixed = {k: (1.0 - blend) * cur[k] + blend * sug[k] for k in keys}
    total = sum(mixed.values()) or 1.0
    return {k: round(v / total, 6) for k, v in mixed.items()}


# --- Mongo I/O orchestrators ----------------------------------------------- #

async def accumulate(
    news_coll: Any,
    universe_coll: Any,
    meta_coll: Any,
    *,
    now: Optional[datetime] = None,
    lookback_hours: int = 12,
    overlap_hours: int = 2,
    cap: int = 4000,
    ticker_extractor: Any = None,
) -> dict[str, Any]:
    """
    One accumulation cycle: read the watermark, fetch the new-docs window, fold
    sub-threshold tickers into the universe, then advance the watermark. Idempotent
    — re-running with no genuinely-new docs touches nothing.
    """
    now = now or datetime.now(tz=timezone.utc)
    state = await meta_coll.find_one({"_id": "universe_state"}) or {}

    last_end = state.get("last_cycle_end")
    if isinstance(last_end, datetime):
        if last_end.tzinfo is None:
            last_end = last_end.replace(tzinfo=timezone.utc)
        window_start = last_end - timedelta(hours=overlap_hours)
    else:
        window_start = now - timedelta(hours=lookback_hours)
    seen: dict[str, str] = dict(state.get("seen_hashes") or {})

    if ticker_extractor is None:
        try:
            from ticker_extractor import TickerExtractor
            ticker_extractor = TickerExtractor()
        except Exception:  # noqa: BLE001
            ticker_extractor = None

    docs = await _fetch_window_docs(news_coll, window_start, now, cap=cap)
    new_docs = select_new_docs(docs, seen)
    features = build_universe_features(new_docs, ticker_extractor=ticker_extractor)

    promoted = 0
    for ticker, cycle in features.items():
        existing = await universe_coll.find_one({"_id": ticker})
        merged = merge_candidate(existing, cycle, now=now)
        merged["_id"] = ticker
        merged["ticker"] = ticker
        await universe_coll.replace_one({"_id": ticker}, merged, upsert=True)
        if merged["promoted"]:
            promoted += 1

    # Remember the hashes we just counted (with publish time, for pruning).
    for d in new_docs:
        h = d.get("content_hash")
        if not h:
            continue
        pub = d.get("published_at")
        seen[h] = pub.isoformat() if isinstance(pub, datetime) else now.isoformat()
    seen = prune_seen(seen, now=now)

    await meta_coll.replace_one(
        {"_id": "universe_state"},
        {
            "_id": "universe_state",
            "last_cycle_end": now,
            "seen_hashes": seen,
            "updated_at": now,
        },
        upsert=True,
    )

    summary = {
        "window_start": window_start,
        "window_end": now,
        "docs_in_window": len(docs),
        "new_docs": len(new_docs),
        "tickers_touched": len(features),
        "promoted_now": promoted,
    }
    log.info("catalyst universe: %s", summary)
    return summary


async def auto_tune(
    rankings_coll: Any,
    meta_coll: Any,
    *,
    min_graded: int = 10,
    blend: float = 0.2,
) -> dict[str, Any]:
    """
    Incrementally nudge the pre-score weights from the graded track record.

    No-op until ``min_graded`` graded runs exist (the backtest overfits with few
    runs). Reuses ``catalyst_backtest.sweep`` for the suggestion, blends it in,
    and persists to ``catalyst_meta._id == "weights"`` (delete that doc to revert).
    """
    projection = {
        "_id": 0, "run_id": 1, "generated_at": 1,
        "items.ticker": 1, "items.components": 1, "metrics": 1,
    }
    runs = await (
        rankings_coll.find({"metrics.graded": {"$gt": 0}}, projection)
        .sort("generated_at", -1).limit(500).to_list(length=500)
    )
    n = len(runs)
    if n < min_graded:
        return {"tuned": False, "reason": f"only {n} graded runs (need {min_graded})", "n_graded": n}

    sweep = catalyst_backtest.sweep(runs, top=1)
    candidates = sweep.get("candidates") or []
    if not candidates:
        return {"tuned": False, "reason": "no backtest candidates", "n_graded": n}

    suggested = candidates[0]["weights"]
    current_doc = await meta_coll.find_one({"_id": "weights"}) or {}
    current = current_doc.get("weights") or dict(DEFAULT_WEIGHTS)
    new_weights = blend_weights(current, suggested, blend=blend)

    await meta_coll.replace_one(
        {"_id": "weights"},
        {
            "_id": "weights",
            "weights": new_weights,
            "prev": current,
            "suggested": suggested,
            "n_graded": n,
            "updated_at": datetime.now(tz=timezone.utc),
        },
        upsert=True,
    )
    log.info("catalyst auto-tune: weights nudged from %d graded runs -> %s", n, new_weights)
    return {"tuned": True, "weights": new_weights, "n_graded": n}
