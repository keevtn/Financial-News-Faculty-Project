"""
catalyst_ranker.py
==================
Pre-market **catalyst ranking** for structured financial news.

Goal (Branch 1 = catalyst ranking, not price prediction): given the news that
broke while the market was closed, rank the tickers with the strongest, most
*material* news catalyst before the next open — with an inspectable rationale,
a direction, and a confidence, plus a persisted record so every run is
auditable and later gradeable.

Pipeline
--------
1. **Window**         — market-calendar overnight window (prev close -> next open).
2. **Recall repair**  — re-extract tickers for any doc that ingested without them.
3. **Clustering**     — collapse near-duplicate stories so syndication does not
                        inflate "attention"; count *independent sources*.
4. **Features**       — per ticker: independent stories/sources, abnormal
                        attention (today vs trailing baseline), source-type
                        materiality, aggregate sentiment.
5. **Pre-filter**     — volume floor + transparent composite pre-score (cheap,
                        deterministic) -> shortlist top-K candidates.
6. **Deep read**      — one temperature-0 LLM call scores the shortlist against
                        an explicit rubric (materiality / surprise / sentiment /
                        breadth) and writes the rationale. Optional: degrades to
                        the quantitative pre-score when no API key is present.
7. **Persist**        — store run_id, window, params, prompt, raw LLM output,
                        and ranked items for reproducibility + evaluation.

Everything except the Mongo I/O and the LLM call is a pure function, so the
ranking logic is unit-testable without a database or network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Optional

from market_calendar import next_session_bounds, overnight_window

log = logging.getLogger("catalyst_ranker")

# Override with the CATALYST_MODEL env var to trade cost vs quality
# (e.g. claude-sonnet-4-6 or claude-haiku-4-5 are much cheaper than Opus).
MODEL = os.environ.get("CATALYST_MODEL", "claude-opus-4-8")

# Deep-read stage selector:
#   cluster — event-typed per-story-cluster grading (catalyst_deep_read; default)
#   rubric  — legacy one-call whole-shortlist rubric (_run_llm below)
# Read at call time so a redeploy isn't needed to flip modes in tests.
_DEEP_READ_MODE_ENV = "CATALYST_DEEP_READ"

# --- Catalyst profiles ----------------------------------------------------- #
# A "profile" scopes the same ranking pipeline to a subset of structured sources,
# so the dashboard can run more than one catalyst lane off one engine:
#   • combined   — general market news + regulatory filings (the broad lane)
#   • regulatory — SEC + FDA only: pure filings/enforcement catalysts, no wire
#                  recap noise (the "is something *official* happening?" lane)
# Each persisted run records its profile; reads/scheduling are keyed by it.
CATALYST_PROFILES: dict[str, dict[str, Any]] = {
    "combined": {
        "label": "Market + Regulatory",
        "source_types": ["rss", "sec", "fda"],
    },
    "regulatory": {
        "label": "SEC + FDA only",
        "source_types": ["sec", "fda"],
    },
}
DEFAULT_PROFILE = "combined"


def resolve_profile(profile: Optional[str]) -> tuple[str, list[str]]:
    """(name, source_types) for a profile; falls back to the default if unknown."""
    name = profile if profile in CATALYST_PROFILES else DEFAULT_PROFILE
    return name, list(CATALYST_PROFILES[name]["source_types"])

# --- Tunables -------------------------------------------------------------- #

# Materiality by source type: regulatory filings move expectations more than a
# market-recap blog, so they outrank RSS at equal volume. Social is excluded
# from structured catalyst ranking entirely.
SOURCE_TYPE_WEIGHT: dict[str, float] = {"sec": 1.6, "fda": 1.6, "rss": 1.0}

# Light per-source credibility multiplier (wires > aggregators > blogs).
# Matched case-insensitively as a substring of the source label.
_CREDIBILITY: dict[str, float] = {
    "reuters": 1.15, "bloomberg": 1.15, "associated press": 1.12, "ap ": 1.12,
    "wall street journal": 1.12, "wsj": 1.12, "financial times": 1.12,
    "cnbc": 1.05, "marketwatch": 1.03, "barron": 1.05, "sec edgar": 1.1,
    "fda": 1.1, "businesswire": 1.03, "globe newswire": 1.0, "pr newswire": 1.0,
}

# Deep-read breadth: how many shortlist tickers, representative articles per
# ticker, and description characters reach the LLM. Wider = more diverse
# grading feedback per run at proportionally higher API cost. Env-tunable so
# test runs can widen without a redeploy (credits are spent on news analysis
# only). Read at call time.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Title-similarity threshold above which two articles are "the same story".
_DUP_THRESHOLD = 0.72

# Saturation points for the bounded feature transforms.
_ATTENTION_SAT = 8.0     # n_stories at which the attention component maxes out
_ABNORMAL_SAT = 3.0      # 3x trailing baseline -> max abnormal component
_MIN_BASELINE = 0.25     # floor so a zero baseline doesn't divide by zero

# Market-cap "size factor": a multiplier on the composite pre-score that
# down-weights mega-cap names (they dominate raw news volume yet rarely move
# much on a single headline) and modestly favours genuine small/mid-cap
# catalysts — WITHOUT rewarding micro-cap penny-stock noise. Complements the
# abnormal-attention term, which already fights "always in the news" bias.
# Thresholds are USD market cap; first threshold met wins; unknown cap -> 1.0.
_SIZE_TIERS: list[tuple[float, float]] = [
    (200e9, 0.82),   # mega-cap   (>$200B)
    (50e9,  0.90),   # very large ($50B–$200B)
    (10e9,  0.96),   # large      ($10B–$50B)
    (2e9,   1.00),   # mid        ($2B–$10B)   — neutral baseline
    (300e6, 1.10),   # small      ($300M–$2B)  — sweet spot for material catalysts
    (0.0,   1.00),   # micro/nano (<$300M)     — neutral, don't reward pump noise
]


def _size_factor(market_cap: Optional[float]) -> float:
    """Size multiplier for the pre-score; 1.0 when the cap is unknown."""
    if market_cap is None:
        return 1.0
    for threshold, factor in _SIZE_TIERS:
        if market_cap >= threshold:
            return factor
    return 1.0


# Pre-market "confirmation factor": a BOOST-ONLY multiplier on the pre-score.
# The strongest evidence a closed-market catalyst is real is the stock already
# moving on it before the open — *and on heavy volume* (a gap on no volume is a
# thin print, not conviction). It can only raise a candidate's priority for the
# expensive LLM deep-read, never bury a genuine news catalyst that simply hasn't
# started moving yet (many move at the open, not pre-market), so the floor is
# 1.0. Magnitude × volume, each saturating; direction is left to the LLM (which
# also sees the pre-market line) and to grading.
_GAP_SAT = 8.0        # |gap %| at which the magnitude term maxes out
_REL_VOL_SAT = 2.0    # relative volume at which the volume gate maxes out
_CONFIRM_WEIGHT = 0.20  # max boost (+20%) at full magnitude AND full volume


def _confirmation_factor(
    gap_pct: Optional[float], rel_volume: Optional[float]
) -> float:
    """Pre-market boost in [1.0, 1.20]; 1.0 when there's no pre-market data."""
    if gap_pct is None:
        return 1.0
    mag = min(abs(gap_pct) / _GAP_SAT, 1.0)
    # Unknown volume -> half credit (we have a move but can't confirm conviction).
    vol = 0.5 if rel_volume is None else max(0.0, min(rel_volume / _REL_VOL_SAT, 1.0))
    return round(1.0 + _CONFIRM_WEIGHT * mag * vol, 4)

# Prominence: a ticker named only in article *bodies* (never a headline) is
# usually an incidental mention — a competitor cited in someone else's story —
# not the subject. Scale the pre-score by [_PROMINENCE_FLOOR, 1.0] on the share
# of docs that name the ticker in the title, so incidental names stop wasting
# deep-read gate slots without being hard-dropped (the LLM can still overturn).
_PROMINENCE_FLOOR = 0.7


def _prominence_factor(title_mention_share: Optional[float]) -> float:
    """Pre-score multiplier in [0.7, 1.0]; 1.0 when share is unknown."""
    if title_mention_share is None:
        return 1.0
    share = max(0.0, min(1.0, title_mention_share))
    return round(_PROMINENCE_FLOOR + (1.0 - _PROMINENCE_FLOOR) * share, 4)


_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "be", "after", "amid", "over",
    "into", "its", "it", "new", "says", "say", "will", "what", "how", "why",
    "this", "that", "their", "has", "have", "up", "down", "inc", "corp", "co",
})


# --- Data shapes ----------------------------------------------------------- #

@dataclass
class CandidateFeatures:
    """Per-ticker quantitative features computed before the LLM stage."""

    ticker: str
    n_docs: int = 0                  # raw mentions in the window
    n_stories: int = 0               # distinct near-duplicate clusters
    n_sources: int = 0               # distinct source labels (independent breadth)
    source_types: list[str] = field(default_factory=list)
    mean_sentiment: float = 0.0      # aggregate score in [-1, 1]
    abnormal_attention: float = 1.0  # today's mentions / trailing daily baseline
    best_source_weight: float = 1.0  # max SOURCE_TYPE_WEIGHT seen
    credibility: float = 1.0         # max per-source credibility seen
    title_mention_share: Optional[float] = None  # docs naming the ticker in the TITLE / n_docs; None = unknown
    market_cap: Optional[float] = None  # USD market cap (Yahoo/yfinance); None if unknown
    size_factor: float = 1.0         # size multiplier applied to the pre-score
    premarket: Optional[dict[str, Any]] = None  # {gap_pct, rel_volume, ...} or None
    confirmation_factor: float = 1.0  # pre-market boost applied to the pre-score
    pre_score: float = 0.0           # composite, 0..100
    components: dict[str, float] = field(default_factory=dict)
    sample_articles: list[dict[str, Any]] = field(default_factory=list)
    article_hashes: list[str] = field(default_factory=list)


# --- Text / clustering helpers (pure) -------------------------------------- #

def _title_tokens(title: str) -> frozenset[str]:
    toks = re.findall(r"[a-z0-9]+", (title or "").lower())
    return frozenset(t for t in toks if t not in _STOPWORDS and len(t) > 1)


def _same_story(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """
    Decide whether two articles describe the same underlying story.

    Cheap token-overlap (Jaccard) gate first; fall back to a sequence ratio on
    the raw titles only when the gate is borderline. This collapses "Reuters
    story reprinted by 9 outlets" into one story without an embedding model.
    """
    ta, tb = _title_tokens(a["title"]), _title_tokens(b["title"])
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    union = len(ta | tb)
    jaccard = inter / union if union else 0.0
    if jaccard >= _DUP_THRESHOLD:
        return True
    # Borderline: confirm with a character-level ratio (catches reworded heads).
    if jaccard >= 0.45:
        ratio = SequenceMatcher(None, a["title"].lower(), b["title"].lower()).ratio()
        return ratio >= 0.80
    return False


def _cluster(docs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """
    Greedy single-pass near-duplicate clustering for one ticker's articles.

    O(n·k) where k = number of clusters; fine for the few-dozen articles a
    single ticker accumulates overnight. Each returned cluster is one "story".
    """
    clusters: list[list[dict[str, Any]]] = []
    for doc in docs:
        for cluster in clusters:
            if _same_story(doc, cluster[0]):
                cluster.append(doc)
                break
        else:
            clusters.append([doc])
    return clusters


def _credibility_for(source: str) -> float:
    s = (source or "").lower()
    best = 1.0
    for key, val in _CREDIBILITY.items():
        if key in s:
            best = max(best, val)
    return best


def _representative_articles(
    clusters: list[list[dict[str, Any]]], cap: Optional[int] = None
) -> list[dict[str, Any]]:
    """
    Pick one representative per story cluster for the LLM, preferring
    higher-materiality source types and longer descriptions, capped at ``cap``
    (default: CATALYST_ARTICLE_CAP env var, else 8).
    """
    if cap is None:
        cap = _env_int("CATALYST_ARTICLE_CAP", 8)
    reps: list[dict[str, Any]] = []
    # Largest (most-syndicated) stories first — those are the loudest catalysts.
    for cluster in sorted(clusters, key=len, reverse=True):
        rep = max(
            cluster,
            key=lambda d: (
                SOURCE_TYPE_WEIGHT.get(d.get("source_type", "rss"), 1.0),
                _credibility_for(d.get("source", "")),
                len(d.get("description") or ""),
            ),
        )
        reps.append({
            "source": rep.get("source", ""),
            "source_type": rep.get("source_type", "rss"),
            "title": rep.get("title", ""),
            "description": (rep.get("description") or "")[:_env_int("CATALYST_ARTICLE_CHARS", 700)],
            "url": rep.get("url", ""),
            "published_at": _iso(rep.get("published_at")),
            "reprints": len(cluster),
        })
        if len(reps) >= cap:
            break
    return reps


def _iso(dt: Any) -> str:
    return dt.isoformat() if isinstance(dt, datetime) else (dt or "")


# --- Feature engineering (pure) -------------------------------------------- #

def build_candidates(
    docs: list[dict[str, Any]],
    baseline_daily: dict[str, float],
    *,
    ticker_extractor: Any = None,
) -> list[CandidateFeatures]:
    """
    Turn raw window documents into per-ticker ``CandidateFeatures``.

    ``baseline_daily`` maps ticker -> average daily mention count over the
    trailing baseline period (used for abnormal-attention).

    When a ``ticker_extractor`` is supplied it is treated as authoritative and
    re-extracts tickers from every doc, rather than trusting ``doc["tickers"]``
    that may have been stored at ingestion under an older/looser extractor map.
    This keeps recall *and* precision fixes effective on already-stored data
    without a backfill. Falls back to stored tickers only when no extractor is
    given (e.g. unit tests).
    """
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # Which docs name each ticker in the TITLE (vs body-only, an incidental
    # mention). Only knowable when the extractor re-extracts; None otherwise.
    title_hits: dict[str, int] = defaultdict(int)
    for doc in docs:
        if ticker_extractor is not None:
            tickers = list(ticker_extractor.extract(
                doc.get("title", ""), doc.get("description", "")
            ))
            in_title = set(ticker_extractor.extract(doc.get("title", ""), ""))
        else:
            tickers = list(doc.get("tickers") or ())
            in_title = set()
        for t in tickers:
            by_ticker[t].append(doc)
            if t in in_title:
                title_hits[t] += 1

    candidates: list[CandidateFeatures] = []
    for ticker, t_docs in by_ticker.items():
        clusters = _cluster(t_docs)
        sources = {d.get("source", "") for d in t_docs}
        source_types = sorted({d.get("source_type", "rss") for d in t_docs})

        sentiments = [
            float(d["sentiment"]["score"])
            for d in t_docs
            if isinstance(d.get("sentiment"), dict) and d["sentiment"].get("score") is not None
        ]
        # Conviction-weighted mean with a routine floor: a plain mean let five
        # neutral recaps (0.0) dilute one -0.8 CRL to -0.13 — under the +/-0.15
        # direction floor, neutralizing exactly the best-covered catalysts.
        # Weighting each doc by max(|score|, conviction band) keeps strong docs
        # dominant over routine recaps (5x0.0 + -0.8 -> -0.41, bearish) while
        # routine docs still vote neutral with the band's weight, so a lone
        # mild-positive dividend line among quiet tape stays under the floor
        # (+0.20 & 0.0 -> +0.11, neutral). Opposing strong docs cancel honestly.
        w_total = sum(max(abs(s), _DIRECTION_CONVICTION) for s in sentiments)
        mean_sent = (sum(max(abs(s), _DIRECTION_CONVICTION) * s for s in sentiments)
                     / w_total) if w_total else 0.0

        base = max(baseline_daily.get(ticker, 0.0), _MIN_BASELINE)
        abnormal = len(t_docs) / base

        best_weight = max(
            (SOURCE_TYPE_WEIGHT.get(st, 1.0) for st in source_types), default=1.0
        )
        credibility = max((_credibility_for(s) for s in sources), default=1.0)

        cand = CandidateFeatures(
            ticker=ticker,
            n_docs=len(t_docs),
            n_stories=len(clusters),
            n_sources=len(sources),
            source_types=source_types,
            mean_sentiment=round(mean_sent, 4),
            abnormal_attention=round(abnormal, 3),
            best_source_weight=best_weight,
            credibility=round(credibility, 3),
            title_mention_share=(
                round(title_hits[ticker] / len(t_docs), 4)
                if ticker_extractor is not None else None
            ),
            sample_articles=_representative_articles(clusters),
            article_hashes=[d.get("content_hash", "") for d in t_docs],
        )
        candidates.append(cand)
    return candidates


def score_candidates(
    candidates: list[CandidateFeatures],
    *,
    min_sources: int = 2,
    weights: Optional[dict[str, float]] = None,
    market_caps: Optional[dict[str, float]] = None,
    premarket: Optional[dict[str, dict[str, Any]]] = None,
) -> list[CandidateFeatures]:
    """
    Apply the volume floor and compute the transparent composite pre-score.

    The score is a weighted sum of four bounded components, each in [0, 1], so
    a single-candidate run still scores sensibly (no min-max needed):

      attention   — independent story volume (log-saturating)
      abnormal    — today's mentions vs trailing baseline (catalyst novelty)
      sentiment   — magnitude of aggregate sentiment (direction-agnostic)
      materiality — best source-type weight (SEC/FDA > RSS)

    then multiplied by a per-source credibility factor, a market-cap size factor
    (``market_caps`` maps ticker -> USD market cap; absent/None -> 1.0), and a
    pre-market confirmation factor (``premarket`` maps ticker -> {gap_pct,
    rel_volume, ...}; absent/None -> 1.0). Omitting either map preserves the
    original behaviour, so the function stays pure and unit-testable offline.
    """
    w = {"attention": 0.30, "abnormal": 0.25, "sentiment": 0.25, "materiality": 0.20}
    if weights:
        w.update(weights)
    caps = market_caps or {}
    pm = premarket or {}

    qualified = [c for c in candidates if c.n_sources >= min_sources]
    for c in qualified:
        attention = min(1.0, math.log1p(c.n_stories) / math.log1p(_ATTENTION_SAT))
        abnormal = min(1.0, c.abnormal_attention / _ABNORMAL_SAT)
        sentiment = min(1.0, abs(c.mean_sentiment))
        materiality = min(1.0, c.best_source_weight / max(SOURCE_TYPE_WEIGHT.values()))
        credibility_factor = min(1.2, c.credibility)

        c.market_cap = caps.get(c.ticker)
        size_factor = _size_factor(c.market_cap)
        c.size_factor = size_factor

        c.premarket = pm.get(c.ticker)
        gap_pct = c.premarket.get("gap_pct") if c.premarket else None
        rel_volume = c.premarket.get("rel_volume") if c.premarket else None
        confirmation_factor = _confirmation_factor(gap_pct, rel_volume)
        c.confirmation_factor = confirmation_factor

        prominence_factor = _prominence_factor(c.title_mention_share)

        composite = (
            w["attention"] * attention
            + w["abnormal"] * abnormal
            + w["sentiment"] * sentiment
            + w["materiality"] * materiality
        ) * credibility_factor * size_factor * confirmation_factor * prominence_factor

        c.components = {
            "attention": round(attention, 4),
            "abnormal": round(abnormal, 4),
            "sentiment": round(sentiment, 4),
            "materiality": round(materiality, 4),
            "credibility_factor": round(credibility_factor, 4),
            "size_factor": round(size_factor, 4),
            "confirmation_factor": round(confirmation_factor, 4),
            "prominence_factor": prominence_factor,
        }
        c.pre_score = round(100.0 * composite, 2)

    qualified.sort(key=lambda c: c.pre_score, reverse=True)
    return qualified


# Conviction floor for the aggregate direction call: mean sentiment within
# (-0.15, 0.15) is routine-coverage territory (a lone dividend declaration or
# mild wire recap), not a directional catalyst read. Item-level labels keep
# their own thresholds in sentiment.py; this band only governs the per-ticker
# direction the ranker reports.
_DIRECTION_CONVICTION = 0.15


def _direction_from_sentiment(score: float) -> str:
    if score >= _DIRECTION_CONVICTION:
        return "bullish"
    if score <= -_DIRECTION_CONVICTION:
        return "bearish"
    return "neutral"


# --- Mongo I/O ------------------------------------------------------------- #

async def _fetch_window_docs(
    collection: Any, start: datetime, end: datetime, cap: int = 4000,
    source_types: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Structured (non-social) docs published within the window."""
    query = {
        "published_at": {"$gte": start, "$lte": end},
        "source_type": {"$in": source_types or ["rss", "sec", "fda"]},
    }
    projection = {
        "_id": 0, "content_hash": 1, "source": 1, "source_type": 1,
        "title": 1, "description": 1, "url": 1, "published_at": 1,
        "tickers": 1, "sentiment": 1, "extra": 1,
    }
    return await (
        collection.find(query, projection)
        .sort("published_at", -1)
        .limit(cap)
        .to_list(length=cap)
    )


async def _compute_baseline(
    collection: Any, before: datetime, baseline_days: int = 14,
    source_types: Optional[list[str]] = None,
) -> dict[str, float]:
    """
    Average daily mention count per ticker over the trailing ``baseline_days``
    (ending at ``before``). Divided by the number of *days actually present* in
    the corpus so a young database is not unfairly penalised. The baseline is
    scoped to the same ``source_types`` as the window so abnormal-attention is
    measured within the profile (regulatory volume vs its own regulatory norm).
    """
    start = before - timedelta(days=baseline_days)
    query = {
        "published_at": {"$gte": start, "$lt": before},
        "source_type": {"$in": source_types or ["rss", "sec", "fda"]},
    }
    projection = {"_id": 0, "tickers": 1, "published_at": 1}
    docs = await (
        collection.find(query, projection).limit(50_000).to_list(length=50_000)
    )
    totals: dict[str, int] = defaultdict(int)
    days: set[Any] = set()
    for d in docs:
        pub = d.get("published_at")
        if isinstance(pub, datetime):
            days.add(pub.date())
        for t in (d.get("tickers") or ()):
            totals[t] += 1
    n_days = max(1, len(days))
    return {t: count / n_days for t, count in totals.items()}


# --- LLM deep read --------------------------------------------------------- #

_RUBRIC = """\
You are a sell-side analyst ranking pre-market news catalysts. You are NOT
predicting prices — you are judging how strong and market-moving each ticker's
news is RIGHT NOW, grounded only in the articles provided.

For each ticker, score four sub-criteria from 0.0 to 1.0:
  - materiality: does this news change the company's expected cash flows or risk?
    (regulatory filings, FDA decisions, M&A, guidance changes = high; recaps,
    opinion, reiterated ratings = low)
  - surprise: is this genuinely NEW information versus already-known/expected?
  - sentiment_strength: how strongly positive OR negative the news is.
  - breadth: corroboration across independent sources (reprints are not breadth).

Then give:
  - direction: "bullish" | "bearish" | "neutral"
  - catalyst_score: 0-100 overall strength of the catalyst
  - confidence: 0.0-1.0 in your own assessment given the evidence
  - rationale: ONE sentence, specific, citing the actual development.

Weigh materiality RELATIVE TO COMPANY SIZE: the same development moves a small-
or mid-cap far more than a mega-cap, so a genuine catalyst at a smaller company
should outrank routine news at a giant. Each ticker's market cap is provided.

When a PRE-MARKET line is shown, treat it as corroborating market evidence: a
large move on heavy relative volume confirms the catalyst is already being
priced in (raise surprise/materiality and let it inform direction). Do NOT chase
a big move the provided news doesn't justify, and do not penalise a strong
catalyst that simply hasn't started moving pre-market yet.

Rank by catalyst_score. Be skeptical: thin or purely promotional coverage
should score low even if there is a lot of it."""

_SUBMIT_TOOL = {
    "name": "submit_rankings",
    "description": "Submit the catalyst ranking for every candidate ticker.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rankings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "materiality": {"type": "number"},
                        "surprise": {"type": "number"},
                        "sentiment_strength": {"type": "number"},
                        "breadth": {"type": "number"},
                        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
                        "catalyst_score": {"type": "number"},
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "ticker", "materiality", "surprise", "sentiment_strength",
                        "breadth", "direction", "catalyst_score", "confidence", "rationale",
                    ],
                },
            }
        },
        "required": ["rankings"],
    },
}


def _build_llm_prompt(candidates: list[CandidateFeatures]) -> str:
    """Render the shortlist + their representative articles as the user turn."""
    blocks: list[str] = []
    for c in candidates:
        cap = c.market_cap
        cap_str = (
            f"${cap / 1e9:.1f}B" if cap and cap >= 1e9
            else f"${cap / 1e6:.0f}M" if cap else "n/a"
        )
        lines = [
            f"### {c.ticker}  (market cap {cap_str})",
            f"(independent stories={c.n_stories}, distinct sources={c.n_sources}, "
            f"abnormal_attention={c.abnormal_attention}x, source_types={','.join(c.source_types)})",
        ]
        pm = c.premarket
        if pm and pm.get("gap_pct") is not None:
            rv = pm.get("rel_volume")
            rv_str = f"{rv:.1f}x relative volume" if rv is not None else "unknown volume"
            lines.append(f"PRE-MARKET: {pm['gap_pct']:+.1f}% vs prev close on {rv_str}")
        for a in c.sample_articles:
            tag = a["source_type"].upper()
            reprint = f" [+{a['reprints'] - 1} reprints]" if a["reprints"] > 1 else ""
            lines.append(f"- [{tag}] {a['source']}{reprint}: {a['title']}")
            if a["description"]:
                lines.append(f"    {a['description']}")
        blocks.append("\n".join(lines))
    return (
        "Rank these candidate tickers by catalyst strength. Call submit_rankings "
        "with an entry for EVERY ticker below.\n\n" + "\n\n".join(blocks)
    )


async def _run_llm(
    candidates: list[CandidateFeatures],
    *,
    model: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], Optional[str]]:
    """
    Score the shortlist with one forced tool-use call.

    ``model`` overrides the ``CATALYST_MODEL`` env default for this call (the
    manual "Run now" button passes ``claude-opus-4-8`` so it is always full Opus
    regardless of how the scheduler's model is configured).

    Returns (parsed_by_ticker, prompt, raw_json, status). On success status is
    None; on any fallback it is a short human-readable reason (surfaced on the
    run as ``llm_status``) so you can tell *why* the LLM didn't run without
    digging through server logs. The caller falls back to the quantitative
    pre-score whenever parsed_by_ticker is None.

    Note: no sampling params (temperature/top_p/top_k) — those are rejected with
    a 400 on Opus 4.7/4.8. Reproducibility comes from persisting the prompt +
    raw output, not from a fixed temperature.
    """
    prompt = _build_llm_prompt(candidates)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        msg = "ANTHROPIC_API_KEY not set"
        log.info("%s — using quantitative pre-score only", msg)
        return None, prompt, None, msg
    try:
        import anthropic
    except ImportError:
        msg = "anthropic package not installed"
        log.warning("%s — using quantitative pre-score only", msg)
        return None, prompt, None, msg

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        resp = await client.messages.create(
            model=model or MODEL,
            max_tokens=4096,
            system=_RUBRIC,
            tools=[_SUBMIT_TOOL],
            tool_choice={"type": "tool", "name": "submit_rankings"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_rankings":
                rankings = block.input.get("rankings", [])
                by_ticker = {r["ticker"]: r for r in rankings if r.get("ticker")}
                return by_ticker, prompt, json.dumps(block.input), None
        msg = "model returned no submit_rankings tool call"
        log.warning(msg)
        return None, prompt, None, msg
    except Exception as exc:  # noqa: BLE001
        msg = f"LLM call failed: {type(exc).__name__}: {exc}"[:300]
        log.error(msg)
        return None, prompt, None, msg


# --- Orchestrator ---------------------------------------------------------- #

async def _fetch_market_caps_safe(tickers: list[str]) -> dict[str, float]:
    """
    Resolve market caps via Yahoo (yfinance), never raising. Works from
    datacenter IPs (e.g. Render), so the size factor stays live in production.
    Returns {} on any failure -> size-neutral scoring.
    """
    if not tickers:
        return {}
    try:
        from market_screener import fetch_market_caps
        caps = await fetch_market_caps(tickers)
        log.info("market caps resolved: %d/%d tickers (yahoo)", len(caps), len(tickers))
        return caps
    except Exception as exc:  # noqa: BLE001
        log.warning("market-cap fetch failed (%s) — size-neutral scoring", exc)
        return {}


async def _fetch_premarket_safe(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """
    Resolve pre-market gap + relative volume via Finviz Elite, never raising.
    Returns {} when no Elite token is configured or on any failure, so the
    confirmation factor degrades to 1.0 (no behaviour change without Elite).
    """
    if not tickers:
        return {}
    try:
        import finviz_elite
        if not finviz_elite.has_token():
            return {}
        pm = await finviz_elite.fetch_premarket(tickers)
        log.info("pre-market resolved: %d/%d tickers (finviz elite)", len(pm), len(tickers))
        return pm
    except Exception as exc:  # noqa: BLE001
        log.warning("pre-market fetch failed (%s) — no confirmation signal", exc)
        return {}


async def _resolve_market_caps(
    tickers: list[str], premarket: dict[str, dict[str, Any]]
) -> dict[str, float]:
    """
    Market caps, **Finviz Elite preferred, Yahoo fallback**. The pre-market call
    already returns each ticker's cap, so those come free (no extra request);
    Yahoo only fills the names Finviz didn't cover (or all of them when there's no
    Elite token). Degrades to size-neutral scoring if both are empty.
    """
    if not tickers:
        return {}
    finviz_caps = {
        t: pm["market_cap"] for t, pm in premarket.items()
        if pm.get("market_cap") is not None
    }
    missing = [t for t in tickers if t not in finviz_caps]
    yahoo_caps = await _fetch_market_caps_safe(missing) if missing else {}
    if finviz_caps:
        log.info("market caps: %d finviz elite + %d yahoo fallback",
                 len(finviz_caps), len(yahoo_caps))
    return {**yahoo_caps, **finviz_caps}  # finviz wins on any overlap


async def rank_catalysts(
    collection: Any,
    *,
    now: Optional[datetime] = None,
    top_k: Optional[int] = None,
    min_sources: int = 2,
    baseline_days: int = 14,
    use_llm: bool = True,
    ticker_extractor: Any = None,
    model: Optional[str] = None,
    trigger: str = "api",
    weights: Optional[dict[str, float]] = None,
    profile: str = DEFAULT_PROFILE,
    calendar_collection: Any = None,
) -> dict[str, Any]:
    """
    Run the full pipeline and return a ranking result dict (not yet persisted).

    ``top_k`` defaults to the CATALYST_TOP_K env var (else 12): the number of
    shortlist tickers offered to the LLM deep read.

    ``model`` forces a specific LLM for the deep read (else ``CATALYST_MODEL``);
    ``trigger`` records how the run was initiated ("manual" | "scheduled" |
    "api") for cooldown accounting; ``weights`` overrides the composite
    pre-score weights (the auto-tuner passes the tuned vector, else defaults);
    ``profile`` scopes which source types are ranked (see ``CATALYST_PROFILES``);
    ``calendar_collection`` (optional) enables the forward calendar — known
    scheduled events corroborate the deep read's priced-in judgment, and this
    run's forward-looking grades are recorded back for future runs.
    """
    now = now or datetime.now(tz=timezone.utc)
    start, end = overnight_window(now)
    profile, source_types = resolve_profile(profile)

    docs = await _fetch_window_docs(collection, start, end, source_types=source_types)
    baseline = await _compute_baseline(
        collection, start, baseline_days, source_types=source_types
    )

    if ticker_extractor is None:
        try:
            from ticker_extractor import TickerExtractor
            from edgar_tickers import load_cik_map
            # CIK map resolves EDGAR filings (which carry a CIK, not a ticker) to
            # symbols — essential for the regulatory lane. Cached + never raises;
            # {} on failure just falls back to text-only extraction.
            ticker_extractor = TickerExtractor(cik_map=await load_cik_map())
        except Exception:  # noqa: BLE001
            ticker_extractor = None

    candidates = build_candidates(docs, baseline, ticker_extractor=ticker_extractor)
    # Fetch only for the volume-qualified tickers to keep the lookup small.
    qualified_tickers = [c.ticker for c in candidates if c.n_sources >= min_sources]
    # Pre-market signal from Finviz Elite — which also carries market cap, so
    # those caps are preferred and Yahoo only fills the rest (see _resolve_market_caps).
    premarket = await _fetch_premarket_safe(qualified_tickers)
    market_caps = await _resolve_market_caps(qualified_tickers, premarket)
    ranked = score_candidates(
        candidates, min_sources=min_sources, weights=weights,
        market_caps=market_caps, premarket=premarket,
    )
    if top_k is None:
        top_k = _env_int("CATALYST_TOP_K", 12)
    shortlist = ranked[:top_k]

    llm_by_ticker: Optional[dict[str, Any]] = None
    prompt = raw_llm = llm_status = None
    deep: Optional[dict[str, Any]] = None
    deep_read_mod: Any = None
    scheduled: Optional[dict[str, Any]] = None
    mode = os.environ.get(_DEEP_READ_MODE_ENV, "cluster")
    if use_llm and shortlist:
        if mode == "cluster":
            try:
                import catalyst_deep_read as deep_read_mod
                if calendar_collection is not None:
                    import catalyst_calendar
                    scheduled = await catalyst_calendar.lookup_scheduled(
                        calendar_collection, [c.ticker for c in shortlist],
                        start=start, end=end,
                    )
                deep = await deep_read_mod.deep_read(
                    docs, shortlist, profile=profile, model=model,
                    ticker_extractor=ticker_extractor, scheduled=scheduled or None,
                )
                llm_status = deep["status"]
            except Exception as exc:  # noqa: BLE001
                llm_status = f"cluster deep read failed: {type(exc).__name__}: {exc}"[:300]
                log.error(llm_status)
                deep = None
        else:
            llm_by_ticker, prompt, raw_llm, llm_status = await _run_llm(shortlist, model=model)
    elif not use_llm:
        llm_status = "use_llm=false (quantitative-only run requested)"

    items: list[dict[str, Any]] = []
    for c in shortlist:
        item: dict[str, Any] = {
            "ticker": c.ticker,
            "n_docs": c.n_docs,
            "n_stories": c.n_stories,
            "n_sources": c.n_sources,
            "source_types": c.source_types,
            "mean_sentiment": c.mean_sentiment,
            "abnormal_attention": c.abnormal_attention,
            "title_mention_share": c.title_mention_share,
            "market_cap": c.market_cap,
            "size_factor": c.size_factor,
            "premarket": c.premarket,
            "confirmation_factor": c.confirmation_factor,
            "pre_score": c.pre_score,
            "components": c.components,
            "sample_articles": c.sample_articles,
            "article_hashes": c.article_hashes,
            # Quantitative defaults; the deep read (either mode) overlays these.
            "catalyst_score": c.pre_score,
            "direction": _direction_from_sentiment(c.mean_sentiment),
            "confidence": round(min(1.0, c.n_sources / 5.0), 3),
            "rationale": (
                f"{c.n_stories} independent stor"
                f"{'y' if c.n_stories == 1 else 'ies'} across {c.n_sources} "
                f"sources ({'/'.join(c.source_types)}), "
                f"{c.abnormal_attention}x normal attention."
            ),
            "llm_subscores": None,
        }
        llm = (llm_by_ticker or {}).get(c.ticker)
        if llm:
            item.update({
                "catalyst_score": round(float(llm.get("catalyst_score", c.pre_score)), 2),
                "direction": llm.get("direction", _direction_from_sentiment(c.mean_sentiment)),
                "confidence": round(float(llm.get("confidence", 0.5)), 3),
                "rationale": llm.get("rationale", ""),
                "llm_subscores": {
                    "materiality": llm.get("materiality"),
                    "surprise": llm.get("surprise"),
                    "sentiment_strength": llm.get("sentiment_strength"),
                    "breadth": llm.get("breadth"),
                },
            })
        items.append(item)

    deep_graded = bool(deep and any(g["grade"] for g in deep["grades"]))
    dropped_items: list[dict[str, Any]] = []
    if deep_graded:
        items, dropped_items = deep_read_mod.apply_grades_to_items(items, deep["grades"])

    used_llm = llm_by_ticker is not None or deep_graded
    # Final ordering by catalyst_score (LLM if present, else pre_score).
    items.sort(key=lambda it: it["catalyst_score"], reverse=True)
    for rank, it in enumerate(items, start=1):
        it["rank"] = rank

    result = {
        "run_id": uuid.uuid4().hex,
        "generated_at": now,
        "window_start": start,
        "window_end": end,
        "trigger": trigger,
        "profile": profile,
        "profile_label": CATALYST_PROFILES[profile]["label"],
        "source_types_scanned": source_types,
        "model": (model or MODEL) if used_llm else None,
        "used_llm": used_llm,
        "llm_status": llm_status,  # None on success; reason string on fallback
        "params": {
            "top_k": top_k,
            "min_sources": min_sources,
            "baseline_days": baseline_days,
            "profile": profile,
        },
        "candidate_count": len(ranked),
        "doc_count": len(docs),
        "items": items,
        "prompt": prompt,
        "raw_llm": raw_llm,
        # Forward-calendar entries that matched this window's shortlist (fed to
        # the deep read as SCHEDULED context for the priced-in judgment).
        "scheduled_matches": scheduled or None,
        # Cluster deep-read audit trail: per-cluster inputs/raw outputs persisted
        # for reproducibility (projected out of reads), plus the items the grader
        # overturned as immaterial (is_material=false -> dropped from ranking).
        "deep_read": ({
            "mode": "cluster",
            "model": deep["model"],
            "clusters_considered": deep["clusters_considered"],
            "graded": sum(1 for g in deep["grades"] if g["grade"]),
            "cached": deep["cached"],
            "dropped_items": dropped_items,
            "grades": deep["grades"],
        } if deep else None),
    }

    # Bank this run's forward-looking grades (PDUFA dates, projected lockups…)
    # for future runs' SCHEDULED context. Never fatal to the run itself.
    if calendar_collection is not None and result["deep_read"]:
        try:
            import catalyst_calendar
            n = await catalyst_calendar.record_run(calendar_collection, result, now=now)
            if n:
                log.info("calendar: recorded %d forward entries", n)
        except Exception as exc:  # noqa: BLE001
            log.warning("calendar record failed (%s)", type(exc).__name__)

    return result


# --- Persistence ----------------------------------------------------------- #

async def save_ranking(rankings_collection: Any, result: dict[str, Any]) -> None:
    """Persist a ranking run (idempotent on run_id)."""
    await rankings_collection.update_one(
        {"run_id": result["run_id"]},
        {"$set": result},
        upsert=True,
    )


# Heavy internal-only fields persisted for reproducibility but never read by any
# client — projected out of read responses so the dashboard isn't downloading the
# full LLM prompt + raw output + every doc hash on each Catalyst-tab load.
_READ_EXCLUDE = {
    "_id": 0, "prompt": 0, "raw_llm": 0, "items.article_hashes": 0,
    "deep_read.grades.input": 0, "deep_read.grades.raw": 0,
}


async def get_latest_ranking(
    rankings_collection: Any, profile: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """
    Return the most recent persisted ranking (minus heavy internals), or None.

    ``profile`` filters to one catalyst lane; ``None`` returns the most recent of
    any profile (back-compat for callers that don't distinguish lanes).
    """
    query = {"profile": profile} if profile else {}
    docs = await (
        rankings_collection.find(query, _READ_EXCLUDE)
        .sort("generated_at", -1)
        .limit(1)
        .to_list(length=1)
    )
    return docs[0] if docs else None


# --- Evaluation scaffolding ------------------------------------------------ #

def grade_ranking(
    result: dict[str, Any],
    price_history: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """
    Direction-agnostic reaction check (Branch 1 honest evaluation).

    ``price_history`` maps ticker -> {"open", "close", "prev_close"} for the
    session that followed the ranking. The realized move is measured from
    **prev_close** (the last pre-catalyst price) to the session close, so the
    overnight gap — where overnight catalysts express most of their move — is
    included. Falls back to open->close (basis "open") only when no prior
    close exists. ``gap_return`` / ``drift_return`` are also recorded per
    ticker so gap-capture vs post-open drift stay separately observable.
    Then: did the top half of the ranking move more than the bottom half, and
    a directional hit-rate on the same gap-inclusive return.

    Returns a metrics dict; ungraded tickers (no price data) are skipped.
    """
    graded = []
    for it in result.get("items", []):
        ph = price_history.get(it["ticker"])
        if not ph or not ph.get("open"):
            continue
        prev_close = ph.get("prev_close")
        entry = prev_close if prev_close else ph["open"]
        basis = "prev_close" if prev_close else "open"
        ret = (ph["close"] - entry) / entry
        predicted = it.get("direction", "neutral")
        hit = (
            (predicted == "bullish" and ret > 0)
            or (predicted == "bearish" and ret < 0)
        )
        graded.append({
            "ticker": it["ticker"],
            "rank": it["rank"],
            "abs_move": abs(ret),
            "return": ret,
            "entry_basis": basis,
            "gap_return": (round((ph["open"] - prev_close) / prev_close, 5)
                           if prev_close else None),
            "drift_return": round((ph["close"] - ph["open"]) / ph["open"], 5),
            "direction": predicted,
            "direction_hit": hit if predicted != "neutral" else None,
        })

    if not graded:
        return {"graded": 0, "note": "no price data available for graded tickers"}

    graded.sort(key=lambda g: g["rank"])
    mid = max(1, len(graded) // 2)
    top, bottom = graded[:mid], graded[mid:]
    top_move = sum(g["abs_move"] for g in top) / len(top)
    bottom_move = sum(g["abs_move"] for g in bottom) / len(bottom) if bottom else 0.0

    directional = [g for g in graded if g["direction_hit"] is not None]
    hit_rate = (
        sum(1 for g in directional if g["direction_hit"]) / len(directional)
        if directional else None
    )

    bases = {g["entry_basis"] for g in graded}
    return {
        "graded": len(graded),
        "entry_basis": ("prev_close" if bases == {"prev_close"}
                        else "open" if bases == {"open"} else "mixed"),
        "top_half_avg_abs_move": round(top_move, 5),
        "bottom_half_avg_abs_move": round(bottom_move, 5),
        "reaction_separation": round(top_move - bottom_move, 5),
        "direction_hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
        "per_ticker": graded,
    }


def _fetch_session_prices_sync(
    tickers: list[str], start: datetime, end: datetime
) -> dict[str, dict[str, float]]:
    """
    Pull daily prices for the session covering [start, end] **plus the prior
    close**. Returns per ticker ``{"open", "close", "prev_close"}`` where
    ``prev_close`` is the final close strictly before the graded session — the
    last pre-catalyst price, so grading can include the overnight gap the
    catalysts actually express in (``prev_close`` is None when yfinance has no
    prior row, e.g. day-one listings). Synchronous — call via ``to_thread``.
    """
    import yfinance as yf

    out: dict[str, dict[str, float]] = {}
    hist_start = (start.date() - timedelta(days=10)).isoformat()  # room for holidays
    hist_end = (end.date() + timedelta(days=1)).isoformat()  # yfinance end is exclusive
    target = start.date()
    for sym in tickers:
        try:
            df = yf.Ticker(sym).history(start=hist_start, end=hist_end, interval="1d")
            if df is None or df.empty:
                continue
            idx = next((i for i, ts in enumerate(df.index) if ts.date() >= target), None)
            if idx is None:
                continue
            row = df.iloc[idx]
            prev_close = float(df.iloc[idx - 1]["Close"]) if idx >= 1 else None
            out[sym] = {"open": float(row["Open"]), "close": float(row["Close"]),
                        "prev_close": prev_close}
        except Exception as exc:  # noqa: BLE001
            log.warning("price fetch failed for %s: %s", sym, exc)
    return out


async def grade_run(
    rankings_collection: Any,
    run_doc: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """
    Grade one persisted run against the realized **gap-inclusive** move of the
    session that followed it (prev_close -> close; the overnight gap is the
    return overnight catalysts express in), and persist the metrics onto the
    run document.

    Returns the metrics dict, or ``None`` if the run can't be graded yet (no
    usable timestamp, or the next session hasn't closed). Shared by the manual
    ``POST /grade`` endpoint and the auto-grade scheduler.
    """
    now = now or datetime.now(tz=timezone.utc)
    generated_at = run_doc.get("generated_at")
    if not isinstance(generated_at, datetime):
        return None

    sess_open, sess_close = next_session_bounds(generated_at)
    if now < sess_close:
        return None  # the session being graded hasn't closed yet

    tickers = [it["ticker"] for it in run_doc.get("items", [])]
    prices = await asyncio.to_thread(_fetch_session_prices_sync, tickers, sess_open, sess_close)
    metrics = grade_ranking(run_doc, prices)
    metrics["session_open"] = sess_open.isoformat()
    metrics["session_close"] = sess_close.isoformat()

    await rankings_collection.update_one(
        {"run_id": run_doc["run_id"]}, {"$set": {"metrics": metrics}}
    )
    return metrics
