"""
squeeze_ranker.py
=================
Short-squeeze ranking — the social-driven cousin of ``catalyst_ranker.py``.

Thesis: a short squeeze needs BOTH halves —
  * **fuel**    — heavy short interest on a hard-to-cover, low-float stock, and
  * **ignition**— a surge of *bullish* retail chatter that triggers covering.

So ``squeeze_score = 100 · fuel · (floor + (1-floor)·ignition)``: a loaded-but-
quiet name keeps a floor from fuel alone ("primed"); bullish social chatter
amplifies it toward 100 ("firing"). Direction is inherently bullish (a squeeze
is a forced rally), but only declared bullish when the social read is actually
bullish — otherwise the setup is "primed/neutral".

Sources (all work from datacenter IPs; no token rotation):
  fuel     — yfinance short data (short % float, days-to-cover, float size)
  universe — Yahoo 'most_shorted' predefined screen (+ optional extra tickers)
  ignition — Bluesky cashtag search via social_search.gather_social, scored
             with the Loughran-McDonald lexicon.

Runs on its own scheduled lane (see middleware/squeeze_scheduler.py) as an
on-demand burst over a few dozen names — never a continuous poller, so it can't
starve the news-feed ingestion the way the RSS lane once did.

The scoring functions are pure (no network / Mongo / heavy imports at module
load) and unit-tested; the orchestrator imports the data sources lazily.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("squeeze_ranker")

# --- Tunables -------------------------------------------------------------- #

_SHORT_FLOAT_SAT = 0.30    # 30% of float short -> max short-float term
_DAYS_TO_COVER_SAT = 8.0   # 8 days to cover -> max (hard to unwind)
_LOW_FLOAT_REF = 50e6      # float at/below which the low-float term maxes out
_FOCUS_SAT = 12.0          # spam-robust mention volume at which ignition maxes
_ENGAGEMENT_SAT = 100.0    # likes+replies at which the engagement term maxes

# Fuel = how loaded the short setup is; weights sum to 1.
_FUEL_W = {"short_float": 0.45, "days_to_cover": 0.35, "low_float": 0.20}
# Ignition = how hot the bullish chatter is; weights sum to 1.
_IGNITION_W = {"volume": 0.50, "bullish": 0.30, "engagement": 0.20}
# Fuel-only floor: a primed name with zero ignition still scores this fraction.
_IGNITION_FLOOR = 0.25


def _sat(x: Optional[float], sat: float) -> float:
    """Saturating ramp: 0 at/below 0, 1.0 at/above ``sat``."""
    if not x or x <= 0:
        return 0.0
    return min(x / sat, 1.0)


def _fuel_score(
    short_pct_float: Optional[float],
    short_ratio: Optional[float],
    float_shares: Optional[float],
) -> tuple[float, dict[str, float]]:
    """The squeeze setup, 0..1: heavy short %, high days-to-cover, low float."""
    sf = _sat(short_pct_float, _SHORT_FLOAT_SAT)
    dtc = _sat(short_ratio, _DAYS_TO_COVER_SAT)
    lf = min(1.0, _LOW_FLOAT_REF / float_shares) if float_shares and float_shares > 0 else 0.0
    fuel = (_FUEL_W["short_float"] * sf
            + _FUEL_W["days_to_cover"] * dtc
            + _FUEL_W["low_float"] * lf)
    return fuel, {"short_float": round(sf, 4), "days_to_cover": round(dtc, 4),
                  "low_float": round(lf, 4)}


def _ignition_score(
    focus_score: float, sentiment: float, engagement: float
) -> tuple[float, dict[str, float]]:
    """The trigger, 0..1: bullish chatter volume × lean × amplification.

    Only *bullish* sentiment ignites a squeeze — bearish/neutral chatter on a
    heavily shorted name is the shorts being right, not a squeeze, so the bullish
    term floors at 0."""
    vol = _sat(focus_score, _FOCUS_SAT)
    bull = max(0.0, sentiment)
    eng = min(1.0, math.log1p(max(0.0, engagement)) / math.log1p(_ENGAGEMENT_SAT))
    ign = (_IGNITION_W["volume"] * vol
           + _IGNITION_W["bullish"] * bull
           + _IGNITION_W["engagement"] * eng)
    return ign, {"volume": round(vol, 4), "bullish": round(bull, 4),
                 "engagement": round(eng, 4)}


def _squeeze_score(fuel: float, ignition: float) -> float:
    """Fuel is the base; ignition amplifies from the floor up to full."""
    return round(100.0 * fuel * (_IGNITION_FLOOR + (1.0 - _IGNITION_FLOOR) * ignition), 2)


def _direction(sentiment: float) -> str:
    """A squeeze is a bullish event, but only call it when chatter is bullish."""
    if sentiment >= 0.05:
        return "bullish"
    if sentiment <= -0.05:
        return "bearish"
    return "neutral"  # primed but not firing


# --- Candidate ------------------------------------------------------------- #

@dataclass
class SqueezeCandidate:
    ticker: str
    short_pct_float: Optional[float] = None  # fraction (0.289 = 28.9%)
    short_ratio: Optional[float] = None      # days to cover
    float_shares: Optional[float] = None
    n_posts: int = 0
    focus_score: float = 0.0
    social_sentiment: float = 0.0            # -1..1 (LM)
    engagement: int = 0
    fuel_score: float = 0.0                  # 0..1
    ignition_score: float = 0.0              # 0..1
    squeeze_score: float = 0.0               # 0..100
    direction: str = "neutral"
    sources: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    sample_posts: list[dict[str, Any]] = field(default_factory=list)


def score_candidate(
    ticker: str,
    short: dict[str, Any],
    social: Optional[dict[str, Any]],
    sentiment: float,
) -> SqueezeCandidate:
    """Combine short fuel + social ignition into a scored squeeze candidate."""
    spf = short.get("short_pct_float")
    sr = short.get("short_ratio")
    fl = short.get("float_shares")
    fuel, fuel_c = _fuel_score(spf, sr, fl)

    focus = float(social.get("focus_score", 0.0)) if social else 0.0
    engagement = int(social.get("engagement", 0)) if social else 0
    n_posts = int(social.get("n_posts", 0)) if social else 0
    ign, ign_c = _ignition_score(focus, sentiment, engagement)

    return SqueezeCandidate(
        ticker=ticker,
        short_pct_float=spf, short_ratio=sr, float_shares=fl,
        n_posts=n_posts, focus_score=round(focus, 3),
        social_sentiment=round(sentiment, 4), engagement=engagement,
        fuel_score=round(fuel, 4), ignition_score=round(ign, 4),
        squeeze_score=_squeeze_score(fuel, ign),
        direction=_direction(sentiment),
        sources=list(social.get("sources", [])) if social else [],
        components={**fuel_c, **{f"ign_{k}": v for k, v in ign_c.items()}},
        sample_posts=list(social.get("top_posts", [])) if social else [],
    )


# --- Sentiment helper ------------------------------------------------------ #

def _social_sentiment(analyzer: Any, texts: list[str]) -> float:
    """Mean LM sentiment (-1..1) over a ticker's posts; 0 when none/no analyzer."""
    if not analyzer or not texts:
        return 0.0
    try:
        results = analyzer.analyze_text_batch([(t, "") for t in texts[:50]])
        scores = [r.score for r in results]
        return sum(scores) / len(scores) if scores else 0.0
    except Exception as exc:  # noqa: BLE001
        log.warning("social sentiment scoring failed: %s", type(exc).__name__)
        return 0.0


# --- Orchestrator ---------------------------------------------------------- #

async def rank_squeezes(
    *,
    universe: Optional[list[str]] = None,
    extra_tickers: Optional[list[str]] = None,
    top_k: int = 15,
    min_short_float: float = 0.10,
    max_fueled: int = 30,
    social_limit: int = 60,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Run the full squeeze pipeline and return a ranking result dict (not persisted).

    universe -> short metrics (fuel) -> filter to genuinely-shorted names ->
    per-ticker social (ignition) -> LM sentiment -> score -> rank.
    All data sources degrade gracefully (empty -> lower scores), never raise.
    """
    now = now or datetime.now(tz=timezone.utc)
    from market_screener import fetch_screener, fetch_short_metrics
    from social_search import gather_social

    # 1) Universe: Yahoo most-shorted screen, plus any caller-supplied names.
    if universe is None:
        screen = await fetch_screener(preset="most_shorted", limit=40)
        universe = [r["ticker"] for r in screen.get("rows", [])]
    universe = [t for t in dict.fromkeys((universe or []) + list(extra_tickers or [])) if t]

    # 2) Fuel: short metrics; keep only genuinely shorted names, cap the set so
    #    the social burst stays small.
    shorts = await fetch_short_metrics(universe)
    fueled = [t for t in universe if (shorts.get(t, {}).get("short_pct_float") or 0) >= min_short_float]
    fueled.sort(key=lambda t: shorts[t].get("short_pct_float") or 0.0, reverse=True)
    fueled = fueled[:max_fueled]

    # 3) Ignition: per-ticker social, only for the fueled set.
    social = await gather_social(fueled, bluesky_limit=social_limit) if fueled else {}

    # 4) Sentiment + score.
    try:
        from sentiment import LoughranMcDonaldAnalyzer
        analyzer: Any = LoughranMcDonaldAnalyzer()
    except Exception as exc:  # noqa: BLE001
        log.warning("LM analyzer unavailable (%s) — social sentiment = 0", type(exc).__name__)
        analyzer = None

    candidates: list[SqueezeCandidate] = []
    for t in fueled:
        s = social.get(t)
        sentiment = _social_sentiment(analyzer, s.get("texts", [])) if s else 0.0
        candidates.append(score_candidate(t, shorts.get(t, {}), s, sentiment))

    candidates.sort(key=lambda c: c.squeeze_score, reverse=True)
    items = [asdict(c) for c in candidates[:top_k]]
    for c in items:
        c.pop("texts", None)  # not stored on the dataclass, but be safe
    for rank, it in enumerate(items, start=1):
        it["rank"] = rank

    return {
        "run_id": uuid.uuid4().hex,
        "generated_at": now,
        "params": {
            "top_k": top_k, "min_short_float": min_short_float,
            "max_fueled": max_fueled, "social_limit": social_limit,
        },
        "universe_count": len(universe),
        "fueled_count": len(fueled),
        "social_count": len(social),
        "items": items,
    }


# --- Persistence ----------------------------------------------------------- #

async def save_squeeze_ranking(collection: Any, result: dict[str, Any]) -> None:
    """Persist a squeeze run (idempotent on run_id)."""
    await collection.update_one(
        {"run_id": result["run_id"]}, {"$set": result}, upsert=True
    )


async def get_latest_squeeze(collection: Any) -> Optional[dict[str, Any]]:
    """Most recent persisted squeeze ranking, or None."""
    docs = await (
        collection.find({}, {"_id": 0})
        .sort("generated_at", -1).limit(1).to_list(length=1)
    )
    return docs[0] if docs else None
