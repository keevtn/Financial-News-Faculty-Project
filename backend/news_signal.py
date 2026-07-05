"""
news_signal.py
==============
Structured-news inputs for the squeeze ranker — three reads over the same
per-ticker slice of the news collection:

  ignition — bullish catalyst score from wire coverage (FDA approval,
             beat-and-raise, M&A, buyback …). Fast physics: a 24h query bound
             with a 6h exponential half-life, so a fresh catalyst counts at
             full strength and yesterday morning's at ~10% — no cliff where a
             flat window would zero out day-two momentum.
  veto     — thesis-breaking events (dilutive offering / going concern /
             chapter 11). Slow physics: flat 5-trading-day memory, no decay —
             float expansion doesn't expire because the headline aged. A veto
             zeroes ignition and flags the candidate "thesis broken" so the UI
             shows *why* a name dropped instead of silently down-ranking it.
  halt     — Nasdaq Trade Halts flag (T1 news pending / T12 info requested /
             H11 regulatory …) on a fueled name, parsed from the halts feed
             (item titles are bare symbols; the reason code lives in the
             description table).

The asymmetry is deliberate: bullish ignition decays fast (squeezes ignite and
peak within a session or two), thesis-breaking facts persist flat (an offering
priced three sessions ago still expanded the float).

Env tunables (read at call time, so tests can monkeypatch):
  SQUEEZE_NEWS_HALFLIFE_H   ignition decay half-life, hours   (default 6)
  SQUEEZE_NEWS_WINDOW_H     ignition query bound, hours       (default 24)
  SQUEEZE_VETO_LOOKBACK_TD  veto memory, trading days         (default 5)

DATABASE SAFETY: scoring functions are pure (no network, no storage imports).
``fetch_ticker_news`` takes a caller-supplied collection and performs a single
read-only ``find`` — this module can never write to Mongo or Redis.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("news_signal")

# --- Tunables (defaults; env read at call time) ----------------------------- #

DEFAULT_HALFLIFE_H = 6.0     # ignition half-life — short: decay does the work
DEFAULT_WINDOW_H = 24.0      # ignition query bound (~9% weight at the edge)
DEFAULT_VETO_LOOKBACK_TD = 5  # flat trading-day memory for thesis breakers

_IGNITION_SAT = 1.25   # summed class contributions at which ignition saturates
_BREADTH_STEP = 0.10   # per extra distinct headline within a class …
_BREADTH_CAP = 1.30    # … up to this multiplier (syndication is confirmation,
                       # not 4x the catalyst)
_GENERIC_W = 0.15      # cap for unclassed-but-bullish wire chatter
_GENERIC_MIN_SENT = 0.15  # stored sentiment below this contributes nothing

_STRUCTURED_TYPES = ("rss", "sec", "fda")  # never social — that's the 70% half

HALT_SOURCE = "Nasdaq Trade Halts"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Event lexicon ----------------------------------------------------------- #
# (class, weight, phrases) — matched as lowercase substrings of title+description.
# Weights express squeeze-ignition strength, not generic importance: an FDA
# approval on a loaded small-cap is the canonical squeeze catalyst.

_BULL_EVENTS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("fda_approval", 1.00, (
        "fda approval", "fda approves", "receives fda approval",
        "wins fda approval", "granted fda approval", "fda clearance",
        "fda clears", "breakthrough therapy designation",
        "breakthrough device designation",
    )),
    ("merger_acquisition", 0.90, (
        "to be acquired", "agrees to be acquired", "merger agreement",
        "definitive merger", "definitive agreement to acquire",
        "acquisition proposal", "takeover offer", "buyout offer",
        "receives acquisition offer", "all-cash offer", "tender offer",
        "exploring a sale", "strategic alternatives",
    )),
    ("clinical_win", 0.85, (
        "met primary endpoint", "meets primary endpoint", "met the primary",
        "positive topline", "positive phase 3", "positive phase 2",
        "statistically significant improvement",
    )),
    ("beat_and_raise", 0.70, (
        "beats estimates", "beat estimates", "tops estimates",
        "raises guidance", "raised guidance", "raises full-year",
        "beat and raise", "record quarterly revenue", "record quarter",
    )),
    ("buyback", 0.50, (
        "share repurchase", "buyback program", "repurchase program",
        "buyback expanded", "expands buyback",
    )),
    ("contract_win", 0.50, (
        "wins contract", "awarded contract", "contract award",
        "awarded a contract", "wins order", "supply agreement",
    )),
    ("partnership", 0.45, (
        "strategic partnership", "strategic investment", "partnership with",
        "collaboration agreement", "licensing agreement",
    )),
    ("analyst_upgrade", 0.30, (
        "upgraded to buy", "upgraded to overweight", "upgraded to outperform",
        "price target raised", "initiates coverage with buy",
    )),
)

# Thesis breakers. Phrases all carry a qualifier so "offering customers free
# trials" can't fire; withdrawal/termination language suppresses the veto (a
# cancelled offering removes the dilution).
_VETO_EVENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dilutive_offering", (
        "public offering", "registered direct offering", "direct offering",
        "secondary offering", "equity offering", "stock offering",
        "share offering", "offering of common stock", "offering of shares",
        "offering of units", "unit offering", "at-the-market offering",
        "atm offering", "prices offering", "priced offering",
        "proposed offering", "announces offering", "upsized offering",
        "warrant inducement", "securities purchase agreement",
    )),
    ("going_concern", ("going concern",)),
    ("chapter_11", (
        "chapter 11", "chapter 7", "bankruptcy protection",
        "files for bankruptcy", "bankruptcy filing", "prepackaged bankruptcy",
    )),
)

_VETO_NEGATORS = ("withdraw", "terminat", "cancel")

# Longest alternates first so T12 wins over T1 at the same position.
_HALT_CODE_RE = re.compile(
    r"\b(T12|T1|T2|T5|T6|T8|H4|H9|H10|H11|O1|IPO1|M1|M2|LUDP|MWC[0-4]|R4|R9)\b"
)
_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")


# --- Pure helpers ------------------------------------------------------------ #

def _decay(age_h: float, halflife_h: float) -> float:
    """Exponential half-life weight; future-dated wire stamps clamp to 1.0."""
    if age_h <= 0:
        return 1.0
    return 0.5 ** (age_h / halflife_h)


def _text(doc: dict[str, Any]) -> str:
    return f"{doc.get('title', '')} {doc.get('description', '')}".lower()


def classify_bullish(text: str) -> Optional[tuple[str, float]]:
    """Strongest bullish catalyst class present in ``text`` (lowercased), or None."""
    t = text.lower()
    best: Optional[tuple[str, float]] = None
    for name, weight, phrases in _BULL_EVENTS:
        if any(p in t for p in phrases) and (best is None or weight > best[1]):
            best = (name, weight)
    return best


def classify_veto(text: str) -> Optional[str]:
    """Thesis-breaking class present in ``text``, or None. Withdrawal language
    suppresses the match — a cancelled offering un-breaks the thesis."""
    t = text.lower()
    for name, phrases in _VETO_EVENTS:
        if any(p in t for p in phrases):
            if any(n in t for n in _VETO_NEGATORS):
                return None
            return name
    return None


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").lower()).strip()


def _age_h(doc: dict[str, Any], now: datetime) -> Optional[float]:
    pub = doc.get("published_at")
    if not isinstance(pub, datetime):
        return None
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return (now - pub).total_seconds() / 3600.0


def _is_halt_doc(doc: dict[str, Any]) -> bool:
    return doc.get("source") == HALT_SOURCE


# --- Ignition ---------------------------------------------------------------- #

def news_ignition(
    docs: list[dict[str, Any]],
    *,
    now: datetime,
    halflife_h: Optional[float] = None,
    window_h: Optional[float] = None,
) -> tuple[float, dict[str, Any]]:
    """
    Bullish catalyst score 0..1 from structured news, exponentially decayed.

    Per event class the *best* decayed hit sets the level (capped at the class
    weight); extra distinct headlines add a small confirmation multiplier —
    syndicated reprints must not count a catalyst four times. Unclassed docs
    with clearly bullish stored sentiment add a small capped generic term.
    """
    halflife_h = halflife_h if halflife_h is not None else _env_float(
        "SQUEEZE_NEWS_HALFLIFE_H", DEFAULT_HALFLIFE_H)
    window_h = window_h if window_h is not None else _env_float(
        "SQUEEZE_NEWS_WINDOW_H", DEFAULT_WINDOW_H)

    best: dict[str, float] = {}          # class -> best decayed contribution
    titles: dict[str, set[str]] = {}     # class -> distinct normalized titles
    weights = {name: w for name, w, _ in _BULL_EVENTS}
    generic = 0.0
    evidence: list[dict[str, Any]] = []
    n_used = 0

    for d in docs:
        if _is_halt_doc(d) or d.get("source_type") == "social":
            continue
        age = _age_h(d, now)
        if age is None or age > window_h:
            continue
        n_used += 1
        dec = _decay(age, halflife_h)
        hit = classify_bullish(_text(d))
        if hit:
            name, w = hit
            contrib = w * dec
            if contrib > best.get(name, 0.0):
                best[name] = contrib
            titles.setdefault(name, set()).add(_norm_title(d.get("title", "")))
            evidence.append({
                "title": (d.get("title") or "")[:140],
                "source": d.get("source"),
                "age_h": round(age, 2),
                "event": name,
                "contribution": round(contrib, 4),
            })
        else:
            sent = ((d.get("sentiment") or {}).get("score") or 0.0)
            if sent >= _GENERIC_MIN_SENT:
                generic += _GENERIC_W * sent * dec

    total = 0.0
    classes: dict[str, float] = {}
    for name, level in best.items():
        mult = min(1.0 + _BREADTH_STEP * (len(titles[name]) - 1), _BREADTH_CAP)
        contrib = min(level * mult, weights[name])   # confirmation, never inflation
        classes[name] = round(contrib, 4)
        total += contrib
    total += min(generic, _GENERIC_W)

    score = min(1.0, total / _IGNITION_SAT)
    evidence.sort(key=lambda e: e["contribution"], reverse=True)
    return round(score, 4), {
        "classes": classes,
        "generic": round(min(generic, _GENERIC_W), 4),
        "n_docs": n_used,
        "halflife_h": halflife_h,
        "evidence": evidence[:3],
    }


# --- Veto -------------------------------------------------------------------- #

def _veto_cutoff(now: datetime, lookback_td: int) -> datetime:
    """UTC start of the ET calendar day ``lookback_td`` trading days back —
    flat memory: anywhere inside the window counts fully."""
    from market_calendar import ET, previous_trading_day

    d = now.astimezone(ET).date()
    for _ in range(max(1, lookback_td)):
        d = previous_trading_day(d)
    return datetime.combine(d, time(0, 0), tzinfo=ET).astimezone(timezone.utc)


def fuel_veto(
    docs: list[dict[str, Any]],
    *,
    now: datetime,
    lookback_td: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """
    Most recent thesis-breaking event within the flat trading-day window, or
    None. No decay: a priced offering still caps the squeeze days later.
    """
    lookback_td = lookback_td if lookback_td is not None else _env_int(
        "SQUEEZE_VETO_LOOKBACK_TD", DEFAULT_VETO_LOOKBACK_TD)
    cutoff = _veto_cutoff(now, lookback_td)

    hit: Optional[dict[str, Any]] = None
    for d in docs:
        if _is_halt_doc(d) or d.get("source_type") == "social":
            continue
        pub = d.get("published_at")
        if not isinstance(pub, datetime):
            continue
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if pub < cutoff or pub > now:
            continue
        reason = classify_veto(_text(d))
        if reason and (hit is None or pub > hit["published_at"]):
            hit = {
                "reason": reason,
                "headline": (d.get("title") or "")[:140],
                "source": d.get("source"),
                "published_at": pub,
                "age_days": round((now - pub).total_seconds() / 86400.0, 2),
            }
    return hit


# --- Halt flag ---------------------------------------------------------------- #

def halt_status(
    docs: list[dict[str, Any]],
    ticker: str,
    *,
    now: datetime,
    window_h: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """
    Most recent Nasdaq Trade Halts entry for ``ticker`` inside the window.

    Halt items carry the bare symbol as title and the reason code inside the
    description table; the feed keeps months of stale rows, so the recency
    window matters. ``resumed`` is inferred from a date appearing *after* the
    reason code (the resumption columns trail it in the row).
    """
    window_h = window_h if window_h is not None else _env_float(
        "SQUEEZE_NEWS_WINDOW_H", DEFAULT_WINDOW_H)
    sym = (ticker or "").strip().upper()

    latest: Optional[tuple[datetime, dict[str, Any]]] = None
    for d in docs:
        if not _is_halt_doc(d):
            continue
        title_sym = (d.get("title") or "").strip().upper()
        in_tickers = sym in [t.upper() for t in (d.get("tickers") or [])]
        if title_sym != sym and not in_tickers:
            continue
        age = _age_h(d, now)
        if age is None or age > window_h:
            continue
        pub = d["published_at"]
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if latest is None or pub > latest[0]:
            latest = (pub, d)

    if latest is None:
        return None
    pub, d = latest
    desc = d.get("description") or ""
    m = _HALT_CODE_RE.search(desc)
    code = m.group(1) if m else None
    resumed = bool(m and _DATE_RE.search(desc, m.end()))
    return {
        "code": code,
        "published_at": pub,
        "resumed": resumed,
        "age_h": round(_age_h(d, now) or 0.0, 2),
    }


# --- Aggregate --------------------------------------------------------------- #

def evaluate_ticker_news(
    docs: list[dict[str, Any]],
    ticker: str,
    *,
    now: datetime,
    halflife_h: Optional[float] = None,
    window_h: Optional[float] = None,
    veto_lookback_td: Optional[int] = None,
) -> dict[str, Any]:
    """
    One ticker's full news read: decayed bullish ignition, flat-window veto,
    halt flag. A veto zeroes ignition here (the module owns the semantics);
    the pre-veto value stays visible in ``components.raw_ignition`` so the UI
    can show what the name *would* have scored.
    """
    ign, comp = news_ignition(docs, now=now, halflife_h=halflife_h, window_h=window_h)
    veto = fuel_veto(docs, now=now, lookback_td=veto_lookback_td)
    halt = halt_status(docs, ticker, now=now, window_h=window_h)
    if veto is not None:
        comp["raw_ignition"] = ign
        ign = 0.0
    return {
        "news_ignition": ign,
        "components": comp,
        "veto": veto,
        "halt": halt,
        "n_news": comp.get("n_docs", 0),
    }


# --- Fetch (read-only; caller supplies the collection) ------------------------ #

async def fetch_ticker_news(
    collection: Any,
    tickers: list[str],
    *,
    now: Optional[datetime] = None,
    window_h: Optional[float] = None,
    veto_lookback_td: Optional[int] = None,
) -> dict[str, list[dict[str, Any]]]:
    """
    One read-only query covering ignition + veto + halt needs for all fueled
    tickers: structured docs (rss/sec/fda) since the *veto* cutoff (the wider
    of the two windows), matched by ticker tag — plus halts matched by bare
    title, because halt items (title = symbol) often carry no ticker tags.
    Returns {ticker: [docs]}; never raises (-> {} on failure).
    """
    syms = [t.strip().upper() for t in dict.fromkeys(tickers or []) if t and t.strip()]
    if collection is None or not syms:
        return {}
    now = now or datetime.now(tz=timezone.utc)
    window_h = window_h if window_h is not None else _env_float(
        "SQUEEZE_NEWS_WINDOW_H", DEFAULT_WINDOW_H)
    lookback_td = veto_lookback_td if veto_lookback_td is not None else _env_int(
        "SQUEEZE_VETO_LOOKBACK_TD", DEFAULT_VETO_LOOKBACK_TD)
    start = min(now - timedelta(hours=window_h), _veto_cutoff(now, lookback_td))

    query = {
        "published_at": {"$gte": start, "$lte": now},
        "source_type": {"$in": list(_STRUCTURED_TYPES)},
        "$or": [
            {"tickers": {"$in": syms}},
            {"source": HALT_SOURCE, "title": {"$in": syms}},
        ],
    }
    projection = {"_id": 0, "title": 1, "description": 1, "source": 1,
                  "source_type": 1, "published_at": 1, "tickers": 1,
                  "sentiment": 1, "url": 1}
    try:
        docs = await (
            collection.find(query, projection).limit(20_000).to_list(length=20_000)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("news fetch failed (%s) — squeeze runs social-only", type(exc).__name__)
        return {}

    out: dict[str, list[dict[str, Any]]] = {s: [] for s in syms}
    for d in docs:
        tagged = {t.upper() for t in (d.get("tickers") or [])}
        if _is_halt_doc(d):
            tagged.add((d.get("title") or "").strip().upper())
        for s in syms:
            if s in tagged:
                out[s].append(d)
    return out
