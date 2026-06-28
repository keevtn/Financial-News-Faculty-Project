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

import asyncio
import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("squeeze_ranker")

# --- Tunables -------------------------------------------------------------- #

_SHORT_FLOAT_SAT = 0.30    # 30% of float short -> max short-float term
_DAYS_TO_COVER_SAT = 8.0   # 8 days to cover -> max (hard to unwind)
_LOW_FLOAT_REF = 50e6      # float at/below which the low-float term maxes out
_FOCUS_SAT = 12.0          # spam-robust mention volume at which ignition maxes
_ENGAGEMENT_SAT = 100.0    # likes+replies at which the engagement term maxes
_VELOCITY_SAT = 5.0        # 5x trailing baseline (gossip) -> max acceleration term

# Ignition weights (sum to 1 with velocity present). The `velocity` term is the
# rolling-window mention acceleration from gossip; when it's unavailable the
# other three are renormalized, so ignition degrades gracefully to the snapshot.
_FUEL_W = {"short_float": 0.45, "days_to_cover": 0.35, "low_float": 0.20}
# `velocity` = social mention acceleration (gossip); `search` = Google-Trends
# search-interest acceleration. Both optional — weights renormalize over the
# terms actually present, so missing signals degrade gracefully.
_IGNITION_W = {"volume": 0.40, "bullish": 0.25, "engagement": 0.15,
               "velocity": 0.20, "search": 0.15}
# Fuel-only floor: a primed name with zero ignition still scores this fraction.
_IGNITION_FLOOR = 0.25


def _velocity_term(velocity: Optional[float]) -> float:
    """Acceleration term: 0 at/below 1x baseline (normal), 1.0 at _VELOCITY_SAT."""
    if velocity is None:
        return 0.0
    return max(0.0, min((velocity - 1.0) / (_VELOCITY_SAT - 1.0), 1.0))


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
    focus_score: float,
    sentiment: float,
    engagement: float,
    velocity: Optional[float] = None,
    search: Optional[float] = None,
) -> tuple[float, dict[str, float]]:
    """The trigger, 0..1: bullish chatter volume × lean × amplification ×
    acceleration. Only *bullish* sentiment ignites a squeeze (bearish/neutral on a
    heavily shorted name is the shorts being right), so the bullish term floors at
    0. ``velocity`` (gossip mention acceleration) and ``search`` (Google-Trends
    search-interest acceleration, already a fuel-adapted [0,1] term) add the "is it
    taking off NOW" dimensions; when absent the remaining terms are renormalized so
    the score stays comparable (graceful degradation)."""
    terms = {
        "volume": _sat(focus_score, _FOCUS_SAT),
        "bullish": max(0.0, sentiment),
        "engagement": min(1.0, math.log1p(max(0.0, engagement)) / math.log1p(_ENGAGEMENT_SAT)),
    }
    if velocity is not None:
        terms["velocity"] = _velocity_term(velocity)
    if search is not None:
        terms["search"] = max(0.0, min(search, 1.0))
    wsum = sum(_IGNITION_W[k] for k in terms)
    ign = sum(_IGNITION_W[k] * v for k, v in terms.items()) / wsum
    return ign, {k: round(v, 4) for k, v in terms.items()}


def _divergence(social_velocity: Optional[float], search_velocity: Optional[float]) -> Optional[str]:
    """Squeeze-stage tell from social vs search acceleration:
      early      — fintwit rising, mainstream search hasn't noticed (earliest)
      mainstream — both rising (the second-leg FOMO broadening)
      search-led — public searching but social quiet
      aligned    — neither notably rising
    None when search interest isn't available to compare."""
    if search_velocity is None:
        return None
    s = social_velocity or 0.0
    g = search_velocity or 0.0
    hot = 2.0  # x baseline considered "rising"
    if s >= hot and g >= hot:
        return "mainstream"
    if s >= hot:
        return "early"
    if g >= hot:
        return "search-led"
    return "aligned"


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
    social_velocity: Optional[float] = None  # gossip mention acceleration (x baseline)
    search_velocity: Optional[float] = None  # Google-Trends search acceleration (x baseline)
    search_clock: Optional[str] = None       # 'fast'/'slow' fuel clock used for Trends sensitivity
    divergence: Optional[str] = None         # early / mainstream / search-led / aligned
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
    velocity: Optional[float] = None,
    search_signal: Optional[dict[str, Any]] = None,
) -> SqueezeCandidate:
    """Combine short fuel + social ignition into a scored squeeze candidate.
    ``velocity`` is the gossip mention-acceleration (x baseline); ``search_signal``
    is the Trends signal ({build_velocity, clock, search_term}). Both optional ->
    ignition falls back to whatever is present."""
    spf = short.get("short_pct_float")
    sr = short.get("short_ratio")
    fl = short.get("float_shares")
    fuel, fuel_c = _fuel_score(spf, sr, fl)

    focus = float(social.get("focus_score", 0.0)) if social else 0.0
    engagement = int(social.get("engagement", 0)) if social else 0
    n_posts = int(social.get("n_posts", 0)) if social else 0

    search_t = search_signal.get("search_term") if search_signal else None
    search_vel = search_signal.get("build_velocity") if search_signal else None
    search_clk = search_signal.get("clock") if search_signal else None
    ign, ign_c = _ignition_score(focus, sentiment, engagement, velocity, search_t)

    return SqueezeCandidate(
        ticker=ticker,
        short_pct_float=spf, short_ratio=sr, float_shares=fl,
        n_posts=n_posts, focus_score=round(focus, 3),
        social_sentiment=round(sentiment, 4),
        social_velocity=round(velocity, 2) if velocity is not None else None,
        search_velocity=round(search_vel, 2) if search_vel is not None else None,
        search_clock=search_clk,
        divergence=_divergence(velocity, search_vel),
        engagement=engagement,
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
    social_collection: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Run the full squeeze pipeline and return a ranking result dict (not persisted).

    universe -> short metrics (fuel) -> filter to genuinely-shorted names ->
    per-ticker social snapshot (ignition) + gossip mention velocity (acceleration,
    from ``social_collection`` if given) -> LM sentiment -> score -> rank.
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

    # 3) Ignition: per-ticker social snapshot + gossip mention velocity (the
    #    acceleration term, from the stored social stream when available).
    social = await gather_social(fueled, bluesky_limit=social_limit) if fueled else {}
    velocities: dict[str, float] = {}
    if fueled and social_collection is not None:
        try:
            from gossip import fetch_velocities
            velocities = await fetch_velocities(social_collection, fueled, now=now)
        except Exception as exc:  # noqa: BLE001
            log.warning("velocity fetch failed (%s) — ignition uses snapshot only", type(exc).__name__)

    # Google-Trends search-interest velocity (gated by RUN_TRENDS; best-effort).
    # fuel-adaptive: short metrics decide each name's fast/slow Trends sensitivity.
    trends_signals: dict[str, dict[str, Any]] = {}
    if fueled:
        try:
            from trends import search_signals
            trends_signals = await search_signals(fueled, shorts, now=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("trends fetch failed (%s) — ignition uses social only", type(exc).__name__)

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
        candidates.append(score_candidate(
            t, shorts.get(t, {}), s, sentiment, velocities.get(t), trends_signals.get(t)))

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


# --- Evaluation ------------------------------------------------------------ #

# Default max-gain within the window that counts a name as having "squeezed".
SQUEEZE_HIT_THRESHOLD = 0.15  # +15% intraday peak vs entry
SQUEEZE_WINDOW_DAYS = 5       # trading days a squeeze is given to play out


def grade_squeeze(
    result: dict[str, Any],
    price_windows: dict[str, dict[str, float]],
    *,
    hit_threshold: float = SQUEEZE_HIT_THRESHOLD,
) -> dict[str, Any]:
    """
    Did the ranked names actually squeeze? Directional (a squeeze is an *up* move),
    unlike the catalyst's direction-agnostic grade.

    ``price_windows`` maps ticker -> {"entry", "max_high", "last_close"} over the
    window that followed the ranking. Per ticker: ``max_gain`` (peak vs entry —
    the squeeze realization) and ``close_return``. Then a hit-rate (share that hit
    ``hit_threshold``) and a top-half-vs-bottom-half max-gain separation (did the
    ranking put the bigger poppers on top?). Ungraded tickers are skipped.
    """
    graded = []
    for it in result.get("items", []):
        pw = price_windows.get(it["ticker"])
        if not pw or not pw.get("entry"):
            continue
        entry = pw["entry"]
        max_gain = (pw["max_high"] - entry) / entry
        close_return = (pw["last_close"] - entry) / entry
        graded.append({
            "ticker": it["ticker"],
            "rank": it["rank"],
            "max_gain": round(max_gain, 5),
            "close_return": round(close_return, 5),
            "squeezed": max_gain >= hit_threshold,
        })

    if not graded:
        return {"graded": 0, "note": "no price data available for graded tickers"}

    graded.sort(key=lambda g: g["rank"])
    mid = max(1, len(graded) // 2)
    top, bottom = graded[:mid], graded[mid:]
    top_gain = sum(g["max_gain"] for g in top) / len(top)
    bottom_gain = sum(g["max_gain"] for g in bottom) / len(bottom) if bottom else 0.0

    return {
        "graded": len(graded),
        "hit_threshold": hit_threshold,
        "squeeze_hit_rate": round(sum(1 for g in graded if g["squeezed"]) / len(graded), 3),
        "avg_max_gain_top": round(top_gain, 5),
        "avg_max_gain_bottom": round(bottom_gain, 5),
        "reaction_separation": round(top_gain - bottom_gain, 5),
        "mean_close_return": round(sum(g["close_return"] for g in graded) / len(graded), 5),
        "per_ticker": graded,
    }


def _fetch_squeeze_window_sync(
    tickers: list[str], start_date: str, end_date: str
) -> dict[str, dict[str, float]]:
    """Daily OHLC over [start, end) per ticker via yfinance. entry = first open,
    max_high = window peak, last_close = final close. Sync — call via to_thread."""
    import yfinance as yf

    out: dict[str, dict[str, float]] = {}
    for sym in tickers:
        try:
            df = yf.Ticker(sym).history(start=start_date, end=end_date, interval="1d")
            if df is None or df.empty:
                continue
            out[sym] = {
                "entry": float(df.iloc[0]["Open"]),
                "max_high": float(df["High"].max()),
                "last_close": float(df.iloc[-1]["Close"]),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("squeeze price fetch failed for %s: %s", sym, exc)
    return out


def _window_close(generated_at: datetime, window_days: int) -> datetime:
    """UTC close of the Nth trading session after ``generated_at`` (the point at
    which a run becomes fully gradeable)."""
    from market_calendar import next_session_bounds, next_trading_day, session_close

    sess_open, _ = next_session_bounds(generated_at)
    d = sess_open.date()
    for _ in range(max(1, window_days) - 1):
        d = next_trading_day(d)
    return session_close(d)


async def grade_squeeze_run(
    collection: Any,
    run_doc: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    window_days: int = SQUEEZE_WINDOW_DAYS,
    hit_threshold: float = SQUEEZE_HIT_THRESHOLD,
) -> Optional[dict[str, Any]]:
    """
    Grade one run against the ``window_days`` sessions that followed it, persisting
    the metrics. Returns the metrics, or ``None`` if it can't be graded yet (no
    usable timestamp, or the window hasn't fully closed).
    """
    now = now or datetime.now(tz=timezone.utc)
    generated_at = run_doc.get("generated_at")
    if not isinstance(generated_at, datetime):
        return None

    from market_calendar import next_session_bounds

    sess_open, _ = next_session_bounds(generated_at)
    window_close = _window_close(generated_at, window_days)
    if now < window_close:
        return None  # window still open

    tickers = [it["ticker"] for it in run_doc.get("items", [])]
    start_date = sess_open.date().isoformat()
    end_date = (window_close.date() + timedelta(days=1)).isoformat()  # yfinance end exclusive
    prices = await asyncio.to_thread(_fetch_squeeze_window_sync, tickers, start_date, end_date)

    metrics = grade_squeeze(run_doc, prices, hit_threshold=hit_threshold)
    metrics["window_start"] = sess_open.isoformat()
    metrics["window_close"] = window_close.isoformat()
    metrics["window_days"] = window_days

    await collection.update_one({"run_id": run_doc["run_id"]}, {"$set": {"metrics": metrics}})
    return metrics
