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
    market_cap: Optional[float] = None  # USD market cap (Finviz); None if unknown
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


def _representative_articles(clusters: list[list[dict[str, Any]]], cap: int = 6) -> list[dict[str, Any]]:
    """
    Pick one representative per story cluster for the LLM, preferring
    higher-materiality source types and longer descriptions, capped at ``cap``.
    """
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
            "description": (rep.get("description") or "")[:500],
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
    for doc in docs:
        if ticker_extractor is not None:
            tickers = list(ticker_extractor.extract(
                doc.get("title", ""), doc.get("description", "")
            ))
        else:
            tickers = list(doc.get("tickers") or ())
        for t in tickers:
            by_ticker[t].append(doc)

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
        mean_sent = sum(sentiments) / len(sentiments) if sentiments else 0.0

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

        composite = (
            w["attention"] * attention
            + w["abnormal"] * abnormal
            + w["sentiment"] * sentiment
            + w["materiality"] * materiality
        ) * credibility_factor * size_factor * confirmation_factor

        c.components = {
            "attention": round(attention, 4),
            "abnormal": round(abnormal, 4),
            "sentiment": round(sentiment, 4),
            "materiality": round(materiality, 4),
            "credibility_factor": round(credibility_factor, 4),
            "size_factor": round(size_factor, 4),
            "confirmation_factor": round(confirmation_factor, 4),
        }
        c.pre_score = round(100.0 * composite, 2)

    qualified.sort(key=lambda c: c.pre_score, reverse=True)
    return qualified


def _direction_from_sentiment(score: float) -> str:
    if score >= 0.05:
        return "bullish"
    if score <= -0.05:
        return "bearish"
    return "neutral"


# --- Mongo I/O ------------------------------------------------------------- #

async def _fetch_window_docs(
    collection: Any, start: datetime, end: datetime, cap: int = 4000
) -> list[dict[str, Any]]:
    """Structured (non-social) docs published within the window."""
    query = {
        "published_at": {"$gte": start, "$lte": end},
        "source_type": {"$in": ["rss", "sec", "fda"]},
    }
    projection = {
        "_id": 0, "content_hash": 1, "source": 1, "source_type": 1,
        "title": 1, "description": 1, "url": 1, "published_at": 1,
        "tickers": 1, "sentiment": 1,
    }
    return await (
        collection.find(query, projection)
        .sort("published_at", -1)
        .limit(cap)
        .to_list(length=cap)
    )


async def _compute_baseline(
    collection: Any, before: datetime, baseline_days: int = 14
) -> dict[str, float]:
    """
    Average daily mention count per ticker over the trailing ``baseline_days``
    (ending at ``before``). Divided by the number of *days actually present* in
    the corpus so a young database is not unfairly penalised.
    """
    start = before - timedelta(days=baseline_days)
    query = {
        "published_at": {"$gte": start, "$lt": before},
        "source_type": {"$in": ["rss", "sec", "fda"]},
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
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str], Optional[str]]:
    """
    Score the shortlist with one forced tool-use call.

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
            model=MODEL,
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


async def rank_catalysts(
    collection: Any,
    *,
    now: Optional[datetime] = None,
    top_k: int = 10,
    min_sources: int = 2,
    baseline_days: int = 14,
    use_llm: bool = True,
    ticker_extractor: Any = None,
) -> dict[str, Any]:
    """
    Run the full pipeline and return a ranking result dict (not yet persisted).
    """
    now = now or datetime.now(tz=timezone.utc)
    start, end = overnight_window(now)

    docs = await _fetch_window_docs(collection, start, end)
    baseline = await _compute_baseline(collection, start, baseline_days)

    if ticker_extractor is None:
        try:
            from ticker_extractor import TickerExtractor
            ticker_extractor = TickerExtractor()
        except Exception:  # noqa: BLE001
            ticker_extractor = None

    candidates = build_candidates(docs, baseline, ticker_extractor=ticker_extractor)
    # Size-adjust scoring with market caps (Finviz). Fetch only for the
    # volume-qualified tickers to keep the lookup small; degrade to size-neutral
    # scoring if the screener is unreachable.
    qualified_tickers = [c.ticker for c in candidates if c.n_sources >= min_sources]
    # Resolve size + pre-market signals concurrently (Yahoo caps, Finviz Elite
    # pre-market). Both degrade to neutral on failure / when Elite isn't set up.
    market_caps, premarket = await asyncio.gather(
        _fetch_market_caps_safe(qualified_tickers),
        _fetch_premarket_safe(qualified_tickers),
    )
    ranked = score_candidates(
        candidates, min_sources=min_sources,
        market_caps=market_caps, premarket=premarket,
    )
    shortlist = ranked[:top_k]

    llm_by_ticker: Optional[dict[str, Any]] = None
    prompt = raw_llm = llm_status = None
    if use_llm and shortlist:
        llm_by_ticker, prompt, raw_llm, llm_status = await _run_llm(shortlist)
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
            "market_cap": c.market_cap,
            "size_factor": c.size_factor,
            "premarket": c.premarket,
            "confirmation_factor": c.confirmation_factor,
            "pre_score": c.pre_score,
            "components": c.components,
            "sample_articles": c.sample_articles,
            "article_hashes": c.article_hashes,
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
        else:
            # Quantitative fallback: pre_score is the catalyst score.
            item.update({
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
            })
        items.append(item)

    used_llm = llm_by_ticker is not None
    # Final ordering by catalyst_score (LLM if present, else pre_score).
    items.sort(key=lambda it: it["catalyst_score"], reverse=True)
    for rank, it in enumerate(items, start=1):
        it["rank"] = rank

    return {
        "run_id": uuid.uuid4().hex,
        "generated_at": now,
        "window_start": start,
        "window_end": end,
        "model": MODEL if used_llm else None,
        "used_llm": used_llm,
        "llm_status": llm_status,  # None on success; reason string on fallback
        "params": {
            "top_k": top_k,
            "min_sources": min_sources,
            "baseline_days": baseline_days,
        },
        "candidate_count": len(ranked),
        "doc_count": len(docs),
        "items": items,
        "prompt": prompt,
        "raw_llm": raw_llm,
    }


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
_READ_EXCLUDE = {"_id": 0, "prompt": 0, "raw_llm": 0, "items.article_hashes": 0}


async def get_latest_ranking(rankings_collection: Any) -> Optional[dict[str, Any]]:
    """Return the most recent persisted ranking (minus heavy internals), or None."""
    docs = await (
        rankings_collection.find({}, _READ_EXCLUDE)
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

    ``price_history`` maps ticker -> {"open": float, "close": float} for the
    session that followed the ranking. Computes, per ticker, the absolute
    open->close move, then checks whether the top half of the ranking moved more
    than the bottom half (did we find the real catalysts?) and a directional
    hit-rate (bonus signal toward future price prediction).

    Returns a metrics dict; ungraded tickers (no price data) are skipped.
    """
    graded = []
    for it in result.get("items", []):
        ph = price_history.get(it["ticker"])
        if not ph or not ph.get("open"):
            continue
        ret = (ph["close"] - ph["open"]) / ph["open"]
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

    return {
        "graded": len(graded),
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
    Pull daily open/close for ``tickers`` for the session covering [start, end].
    Synchronous (yfinance) — call via ``asyncio.to_thread``.
    """
    import yfinance as yf

    out: dict[str, dict[str, float]] = {}
    hist_start = start.date().isoformat()
    hist_end = (end.date() + timedelta(days=1)).isoformat()  # yfinance end is exclusive
    for sym in tickers:
        try:
            df = yf.Ticker(sym).history(start=hist_start, end=hist_end, interval="1d")
            if df is None or df.empty:
                continue
            row = df.iloc[0]
            out[sym] = {"open": float(row["Open"]), "close": float(row["Close"])}
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
    Grade one persisted run against the realized open->close move of the session
    that followed it, and persist the metrics onto the run document.

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
