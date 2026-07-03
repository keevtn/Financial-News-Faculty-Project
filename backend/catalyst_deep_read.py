"""
catalyst_deep_read.py
=====================
Per-**story-cluster** LLM deep read for the pre-market catalyst ranker.

The legacy deep read (catalyst_ranker._run_llm) scores the whole shortlist in
one call against a generic rubric. This stage replaces it with an event-typed
grader: the window's documents are clustered into *stories* (near-duplicate
reports of one event, across tickers), the top clusters by backing pre-score
each get ONE temperature-default LLM call, and the model returns a single JSON
object per cluster — event_type, direction (price impact, not tone), intrinsic
magnitude, rumor/forward-looking/priced-in flags, and per-ticker roles for
multi-name events (M&A both-sides handling).

Design rules
------------
* The pre-score stays the recall stage; the deep read is precision — it can
  OVERTURN the prior (``is_material=false`` drops the item from the ranking).
* Cost-bounded: at most ``CATALYST_DEEP_READS`` clusters per run (default 8),
  model from ``CATALYST_MODEL`` unless overridden, calls capped at 4-way
  concurrency.
* Optional Redis grade cache keyed by (model, content-derived cluster id) so
  overlapping runs don't re-pay for an unchanged cluster. Fully graceful: no
  ``REDIS_URI``, a dead endpoint, or a mid-run failure just means uncached
  operation (5-minute soft backoff before the next attempt).
* Everything except the Anthropic call and the cache is a pure function —
  clustering, rendering, parsing, validation, and merge are unit-testable
  offline with no API key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Optional

from catalyst_ranker import SOURCE_TYPE_WEIGHT, _cluster, _credibility_for

log = logging.getLogger("catalyst_deep_read")

# Same env var the ranker uses; one knob controls both stages' cost/quality.
MODEL = os.environ.get("CATALYST_MODEL", "claude-opus-4-8")
MAX_DEEP_READS = int(os.environ.get("CATALYST_DEEP_READS", "8"))
CACHE_TTL_SECONDS = int(os.environ.get("CATALYST_DEEP_READ_TTL", "86400"))
_CONCURRENCY = 4
_MAX_MEMBERS_RENDERED = 5
_BODY_CAP = 500

EVENT_TYPES: frozenset[str] = frozenset({
    "ipo", "direct_listing", "spac_merger", "lockup_expiry", "secondary_offering",
    "atm_program", "shelf_registration", "convertible_issuance", "pipe", "buyback",
    "tender_offer", "dividend_change", "split", "reverse_split", "merger_acquisition",
    "activist_stake", "index_change", "earnings", "guidance_change", "fda_approval",
    "fda_crl", "pdufa_date", "adcom", "clinical_readout", "clinical_hold", "designation",
    "recall", "rating_change", "analyst_action", "short_seller_report", "contract_award",
    "litigation", "cyber_incident", "exec_change", "auditor_change", "bankruptcy",
    "delisting", "going_concern", "halt", "other",
})
_DIRECTIONS: frozenset[str] = frozenset({"bullish", "bearish", "ambiguous"})
_ROLES: frozenset[str] = frozenset({"subject", "acquirer", "target", "peer", "parent"})

# How much of a grade's effective score a non-subject listed name inherits.
_ROLE_WEIGHT: dict[str, float] = {
    "subject": 1.0, "target": 1.0, "acquirer": 0.7, "parent": 0.7, "peer": 0.4,
}

# Discounts applied when turning an intrinsic grade into a ranking score.
_RUMOR_DISCOUNT = 0.75
_PRICED_IN_DISCOUNT = 0.5
_FORWARD_DISCOUNT = 0.85

# --- System prompt (verbatim spec for the deep-read grader) ----------------- #

DEEP_READ_SYSTEM = """\
You are the deep-read stage of a pre-market catalyst ranker. Each call gives you
ONE story cluster (near-duplicate reports of a single event) that a quantitative
pre-score has already ranked into the shortlist. Output exactly one JSON object
grading the catalyst. No prose, no markdown, no text outside the JSON.

CORE PRINCIPLES
- Catalyst materiality and sentiment are DIFFERENT axes. Judge expected PRICE
  IMPACT for the next session, not tone or word choice. A glowingly worded
  dilutive raise is bearish; a dry 424B4 IPO pricing is a major bullish catalyst.
- `direction` = expected direction of the stock's price reaction, NOT the
  emotional valence of the language.
- The PRE-SCORE SIGNALS and structured metadata (form_type, item_codes) are a
  PRIOR from a high-recall / lower-precision stage. Confirm or OVERTURN them. If
  the item is actually immaterial, set is_material=false so ranking drops it.
- The cluster MEMBERS are near-duplicate reports of ONE story. Corroboration
  across independent sources RAISES confidence; it does NOT create extra events.
  Never emit one member as its own catalyst.
- NEVER invent tickers. Use ONLY the provided TICKER CANDIDATES (re-extracted
  upstream via pattern + company-name dict + EDGAR CIK). If the true subject is
  private or not in the list, primary_ticker=null.
- PROFILE awareness: in "regulatory" the cluster is SEC filings + FDA notices
  only — text is terse and structured, so lean on form_type / item_codes and
  expect fewer narrative cues. In "combined" you also get newswire/social prose.

EVENT TAXONOMY (allowed `event_type` values)
ipo, direct_listing, spac_merger, lockup_expiry, secondary_offering,
atm_program, shelf_registration, convertible_issuance, pipe, buyback,
tender_offer, dividend_change, split, reverse_split, merger_acquisition,
activist_stake, index_change, earnings, guidance_change, fda_approval,
fda_crl, pdufa_date, adcom, clinical_readout, clinical_hold, designation,
recall, rating_change, analyst_action, short_seller_report, contract_award,
litigation, cyber_incident, exec_change, auditor_change, bankruptcy,
delisting, going_concern, halt, other
(Use `other` only when nothing fits; name it in `subtype`.)

DEFAULT DIRECTION GUIDANCE (override when specifics dictate)
- Usually bullish: priced ipo, fda_approval, buyback, tender_offer, dividend
  increase/initiation, activist_stake (13D), contract_award, index inclusion,
  breakthrough/orphan designation.
- Usually bearish: secondary_offering, atm_program, shelf_registration,
  convertible_issuance, pipe, reverse_split, fda_crl, clinical_hold,
  going_concern, auditor_change, bankruptcy, delisting, cyber_incident,
  short_seller_report, guidance cut, lockup_expiry.
- Genuinely ambiguous until read: merger_acquisition (two-sided), earnings,
  exec_change, litigation, rating_change.

OUTPUT SCHEMA (return exactly one JSON object; valid, parseable, no fences)
{
  "is_material": boolean,        // false = pre-score false positive; drop from ranking
  "event_type": string,          // from taxonomy
  "subtype": string,             // short, e.g. "priced", "CRL", "upsized"
  "driver": string,              // <=30 words, trader-facing catalyst line for CatalystView; specific, no hedging fluff
  "primary_ticker": string|null, // main subject; MUST be a candidate or null
  "direction": "bullish"|"bearish"|"ambiguous",
  "magnitude": number,           // 0.0-1.0 INTRINSIC importance, BEFORE size_factor/market-cap scaling (pre-score owns that)
  "confidence": number,          // 0.0-1.0 in this extraction, raised by cross-source corroboration
  "is_rumor": boolean,           // hedged / unconfirmed language present
  "is_forward_looking": boolean, // event is scheduled/future, not yet occurred
  "is_priced_in": boolean,       // event was expected/telegraphed → mute effective magnitude for ranking
  "event_date": string|null,     // ISO 8601 if a specific date is stated
  "deal_value_usd": number|null,
  "premium_pct": number|null,
  "affected_tickers": [          // one entry per impacted LISTED name
    { "ticker": string,
      "role": "subject"|"acquirer"|"target"|"peer"|"parent",
      "direction": "bullish"|"bearish"|"ambiguous" }
  ],
  "additional_catalysts": [],    // rare: same schema for a genuinely distinct 2nd event in the cluster
  "rationale": string            // <=25 words: WHY this grade, for grading/debug transparency
}

RULES
1. Direction reflects price impact. Positive PR wrapping a dilutive raise, or an
   exec "pursuing other opportunities" amid trouble, still resolve to bearish.
2. Negation/failure overrides tone: "did not meet", "failed", "not approved",
   "misses", "withdraws" -> bearish, usually HIGH magnitude for trials/approvals.
3. Rumor/hedge language ("reportedly", "in talks", "exploring", "sources say")
   -> is_rumor=true, lower confidence; keep the directional lean (downstream
   discounts it).
4. is_forward_looking: a set FUTURE date (PDUFA date, scheduled vote, next-year
   guidance) -> true, populate event_date. An event already occurred (incl. a
   deal ANNOUNCEMENT closing later) -> false.
5. is_priced_in: this is a PRE-MARKET ranker. If the event was already scheduled
   or telegraphed (earnings on a known date landing near the whisper, a guided
   number merely met, a pre-announced raise) -> true. Intrinsic magnitude can be
   high while effective ranking impact is muted; say so in rationale.
6. Two-sided M&A: emit BOTH names in affected_tickers with correct roles and
   distinct directions (target usually bullish on premium; acquirer often
   ambiguous/slightly bearish). primary_ticker = the cluster's main subject.
7. magnitude is INTRINSIC importance only: confirmed vs rumored, size
   (deal_value/premium), one-time vs recurring, how decisively it changes the
   thesis. Do NOT re-apply market-cap or float scaling — size_factor already did.
   Reserve >0.85 for company-defining events (approval/rejection, takeover,
   bankruptcy, going-concern).
8. Only provided tickers. If the true subject is private/unlisted,
   primary_ticker=null but still fill affected_tickers with any listed
   counterparties that ARE candidates (e.g. a public acquirer of a private
   target).
9. If two genuinely distinct material events share the cluster, top-level the
   most important and put the rest in additional_catalysts; else it stays [].
10. Output valid JSON only: no trailing commas, no comments, no markdown fences,
    nothing before or after.

EXAMPLES

INPUT:
PROFILE: combined
PRE-SCORE: pre_score=0.91 abnormal_attention=high source_weight=1.0 pre_market_confirmation=gap_up
SENTIMENT: score=0.05 label=neutral
CANDIDATES: RDDT — Reddit, Inc.
CLUSTER c-8812 (1 report)
[1] source=sec_edgar type=sec form=424B4 cik=0001872796
    HEADLINE: Reddit, Inc. Prospectus (Rule 424(b)(4))
    BODY: 22,000,000 shares of Class A common stock at a public offering price of $34.00 per share...
OUTPUT:
{"is_material":true,"event_type":"ipo","subtype":"priced","driver":"Reddit IPO priced at $34.00 on 22M shares; first-day liquidity event, gap-up confirmed pre-market.","primary_ticker":"RDDT","direction":"bullish","magnitude":0.8,"confidence":0.96,"is_rumor":false,"is_forward_looking":false,"is_priced_in":false,"event_date":null,"deal_value_usd":748000000,"premium_pct":null,"affected_tickers":[{"ticker":"RDDT","role":"subject","direction":"bullish"}],"additional_catalysts":[],"rationale":"Final 424B4 confirms priced IPO, a company-defining event; pre-market gap corroborates."}

INPUT:
PROFILE: combined
PRE-SCORE: pre_score=0.62 abnormal_attention=med source_weight=0.8 pre_market_confirmation=none
SENTIMENT: score=0.55 label=positive
CANDIDATES: XYZ — XYZ Therapeutics
CLUSTER c-9001 (2 reports)
[1] source=businesswire type=newswire
    HEADLINE: XYZ Therapeutics Announces Upsized $150M Public Offering to Accelerate Pipeline
    BODY: ...offering of common stock... net proceeds to fund clinical development and general corporate purposes...
[2] source=globenewswire type=newswire
    HEADLINE: XYZ Prices Upsized Offering
    BODY: ...
OUTPUT:
{"is_material":true,"event_type":"secondary_offering","subtype":"upsized follow-on","driver":"XYZ prices upsized $150M follow-on; dilutive despite upbeat framing, no pre-market confirmation.","primary_ticker":"XYZ","direction":"bearish","magnitude":0.55,"confidence":0.9,"is_rumor":false,"is_forward_looking":false,"is_priced_in":false,"event_date":null,"deal_value_usd":150000000,"premium_pct":null,"affected_tickers":[{"ticker":"XYZ","role":"subject","direction":"bearish"}],"additional_catalysts":[],"rationale":"Equity raise dilutes holders and is bearish; positive LM sentiment reflects PR tone, not price impact."}

INPUT:
PROFILE: combined
PRE-SCORE: pre_score=0.88 abnormal_attention=high source_weight=0.9 pre_market_confirmation=gap_down
SENTIMENT: score=-0.10 label=neutral
CANDIDATES: ABCD — Abcd Biosciences
CLUSTER c-9107 (1 report)
[1] source=businesswire type=newswire
    HEADLINE: Abcd Biosciences Reports Topline Phase 3 Results for Lead Candidate
    BODY: The study did not meet its primary endpoint of overall survival... the company will evaluate next steps...
OUTPUT:
{"is_material":true,"event_type":"clinical_readout","subtype":"Phase 3 primary endpoint miss","driver":"Abcd lead candidate misses Phase 3 primary endpoint; thesis-breaking miss, gap-down confirmed.","primary_ticker":"ABCD","direction":"bearish","magnitude":0.9,"confidence":0.92,"is_rumor":false,"is_forward_looking":false,"is_priced_in":false,"event_date":null,"deal_value_usd":null,"premium_pct":null,"affected_tickers":[{"ticker":"ABCD","role":"subject","direction":"bearish"}],"additional_catalysts":[],"rationale":"Primary endpoint failure is a severe negative catalyst; neutral headline tone overridden by the miss."}

INPUT:
PROFILE: combined
PRE-SCORE: pre_score=0.84 abnormal_attention=high source_weight=0.9 pre_market_confirmation=mixed
SENTIMENT: score=0.30 label=positive
CANDIDATES: BIGCO — BigCo Inc; TGT2 — Target2 Corp
CLUSTER c-9220 (2 reports)
[1] source=prnewswire type=newswire
    HEADLINE: BigCo to Acquire Target2 for $58.00 Per Share in Cash
    BODY: ...premium of approximately 42% to Target2's closing price... transaction valued at $9.2 billion... expected to close in Q4...
[2] source=reuters type=newswire
    HEADLINE: BigCo strikes $9.2B cash deal for Target2
    BODY: ...
OUTPUT:
{"is_material":true,"event_type":"merger_acquisition","subtype":"all-cash acquisition","driver":"BigCo acquires Target2 at $58 cash, 42% premium; target re-rates to deal price, acquirer mixed.","primary_ticker":"TGT2","direction":"bullish","magnitude":0.9,"confidence":0.94,"is_rumor":false,"is_forward_looking":false,"is_priced_in":false,"event_date":null,"deal_value_usd":9200000000,"premium_pct":42.0,"affected_tickers":[{"ticker":"TGT2","role":"target","direction":"bullish"},{"ticker":"BIGCO","role":"acquirer","direction":"ambiguous"}],"additional_catalysts":[],"rationale":"Definitive all-cash takeover at 42% premium lifts target; acquirer reaction depends on price/financing. Two independent wires corroborate."}

INPUT:
PROFILE: combined
PRE-SCORE: pre_score=0.58 abnormal_attention=med source_weight=0.7 pre_market_confirmation=none
SENTIMENT: score=0.20 label=positive
CANDIDATES: QRS — QRS Holdings
CLUSTER c-9301 (1 report)
[1] source=bloomberg type=newswire
    HEADLINE: QRS Holdings Reportedly Explores Strategic Options Including a Sale — Sources
    BODY: ...according to people familiar with the matter... no final decision has been made... QRS declined to comment...
OUTPUT:
{"is_material":true,"event_type":"merger_acquisition","subtype":"unconfirmed sale exploration","driver":"QRS reportedly exploring a sale per unnamed sources; unconfirmed, no company comment.","primary_ticker":"QRS","direction":"bullish","magnitude":0.5,"confidence":0.55,"is_rumor":true,"is_forward_looking":false,"is_priced_in":false,"event_date":null,"deal_value_usd":null,"premium_pct":null,"affected_tickers":[{"ticker":"QRS","role":"subject","direction":"bullish"}],"additional_catalysts":[],"rationale":"Single-sourced sale rumor, hedged language, no confirmation; bullish lean but low confidence."}

INPUT:
PROFILE: regulatory
PRE-SCORE: pre_score=0.44 abnormal_attention=low source_weight=1.0 pre_market_confirmation=none
SENTIMENT: score=0.00 label=neutral
CANDIDATES: MNOP — Mnop Corp
CLUSTER c-9410 (1 report)
[1] source=sec_edgar type=sec form=8-K items=["5.02"] cik=0000111222
    HEADLINE: Mnop Corp 8-K — Departure/Appointment of Officers
    BODY: ...the Board appointed Jane Roe as Assistant Corporate Secretary, effective immediately...
OUTPUT:
{"is_material":false,"event_type":"exec_change","subtype":"minor administrative appointment","driver":"Mnop names a new Assistant Corporate Secretary; routine administrative change, not market-moving.","primary_ticker":"MNOP","direction":"ambiguous","magnitude":0.05,"confidence":0.9,"is_rumor":false,"is_forward_looking":false,"is_priced_in":false,"event_date":null,"deal_value_usd":null,"premium_pct":null,"affected_tickers":[{"ticker":"MNOP","role":"subject","direction":"ambiguous"}],"additional_catalysts":[],"rationale":"Item 5.02 fired the pre-score, but an assistant-secretary appointment is immaterial; drop from ranking."}"""


# --- Cluster construction (pure) -------------------------------------------- #

def _cluster_id(members: list[dict[str, Any]]) -> str:
    """Content-derived id, stable across runs (cache key + audit handle)."""
    keys = sorted(m.get("content_hash") or m.get("title", "") for m in members)
    return "c-" + hashlib.sha1("|".join(keys).encode()).hexdigest()[:10]


def build_story_clusters(
    docs: list[dict[str, Any]],
    shortlist_tickers: set[str],
    *,
    ticker_extractor: Any = None,
) -> list[dict[str, Any]]:
    """
    Cluster window docs into cross-ticker *stories* and keep the ones that touch
    at least one shortlisted ticker.

    Unlike the ranker's per-ticker clustering, this runs globally so a two-sided
    M&A story is ONE cluster with both names as candidates, not two duplicate
    clusters. Candidates = union of extracted tickers over members (the "never
    invent tickers" list handed to the LLM), which may include non-shortlist
    counterparties.
    """
    relevant: list[dict[str, Any]] = []
    for doc in docs:
        if ticker_extractor is not None:
            tickers = tuple(ticker_extractor.extract(
                doc.get("title", ""), doc.get("description", "")
            ))
        else:
            tickers = tuple(doc.get("tickers") or ())
        if any(t in shortlist_tickers for t in tickers):
            relevant.append({**doc, "_tickers": tickers})

    out: list[dict[str, Any]] = []
    for members in _cluster(relevant):
        candidates = sorted({t for m in members for t in m["_tickers"]})
        out.append({
            "cluster_id": _cluster_id(members),
            "members": members,
            "candidates": candidates,
        })
    return out


# --- Input rendering (pure) -------------------------------------------------- #

_ITEM_CODE_RE = re.compile(r"\bItem[s]?\s+(\d+\.\d{2})", re.IGNORECASE)
_CIK_RE = re.compile(r"\((\d{10})\)")
_FORM_FROM_TITLE_RE = re.compile(r"^\s*([0-9A-Z][0-9A-Z ./\-]{0,11}?)\s+-\s")


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_") or "unknown"


def _sec_form(doc: dict[str, Any]) -> Optional[str]:
    """Filing type from stored extra, the source label, or the EDGAR title."""
    extra = doc.get("extra") or {}
    if extra.get("filing_type"):
        return str(extra["filing_type"])
    source = doc.get("source", "")
    if "—" in source:
        tail = source.rsplit("—", 1)[1].strip()
        if tail:
            return tail
    m = _FORM_FROM_TITLE_RE.match(doc.get("title", ""))
    return m.group(1).strip() if m else None


def _item_codes(doc: dict[str, Any]) -> list[str]:
    text = f"{doc.get('title', '')} {doc.get('description', '')}"
    seen: list[str] = []
    for m in _ITEM_CODE_RE.finditer(text):
        code = m.group(1)
        if code not in seen:
            seen.append(code)
    return seen


def _cik_of(doc: dict[str, Any]) -> Optional[str]:
    m = _CIK_RE.search(f"{doc.get('title', '')} {doc.get('description', '')}")
    return m.group(1) if m else None


def _abnormal_bucket(x: float) -> str:
    if x >= 3.0:
        return "high"
    if x >= 1.5:
        return "med"
    return "low"


def _sentiment_of(members: list[dict[str, Any]]) -> tuple[float, str]:
    scores = [
        float(m["sentiment"]["score"])
        for m in members
        if isinstance(m.get("sentiment"), dict) and m["sentiment"].get("score") is not None
    ]
    mean = sum(scores) / len(scores) if scores else 0.0
    label = "positive" if mean > 0.15 else "negative" if mean < -0.15 else "neutral"
    return round(mean, 2), label


def _premarket_confirmation(
    candidates: list[str], features_by_ticker: dict[str, Any]
) -> str:
    """gap_up / gap_down / mixed / none from the shortlisted candidates' gaps."""
    gaps: list[float] = []
    for t in candidates:
        feat = features_by_ticker.get(t)
        pm = getattr(feat, "premarket", None) if feat is not None else None
        if pm and pm.get("gap_pct") is not None:
            gaps.append(float(pm["gap_pct"]))
    if not gaps:
        return "none"
    up = any(g >= 1.0 for g in gaps)
    down = any(g <= -1.0 for g in gaps)
    if up and down:
        return "mixed"
    if up:
        return "gap_up"
    if down:
        return "gap_down"
    return "none"


def _order_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strongest members first, preferring distinct sources (breadth over echo)."""
    ranked = sorted(
        members,
        key=lambda d: (
            SOURCE_TYPE_WEIGHT.get(d.get("source_type", "rss"), 1.0),
            _credibility_for(d.get("source", "")),
            len(d.get("description") or ""),
        ),
        reverse=True,
    )
    picked: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for d in ranked:  # one per distinct source label first
        src = d.get("source", "")
        if src not in seen_sources:
            picked.append(d)
            seen_sources.add(src)
    for d in ranked:  # then fill with repeats if room remains
        if d not in picked:
            picked.append(d)
    return picked[:_MAX_MEMBERS_RENDERED]


def render_cluster_input(
    cluster: dict[str, Any],
    *,
    profile: str,
    features_by_ticker: dict[str, Any],
    name_map: Optional[dict[str, str]] = None,
) -> str:
    """One INPUT block in exactly the format the system prompt's examples use."""
    members = cluster["members"]
    candidates = cluster["candidates"]
    names = name_map or {}

    backing = [features_by_ticker[t] for t in candidates if t in features_by_ticker]
    pre = max((getattr(f, "pre_score", 0.0) for f in backing), default=0.0)
    abnormal = max((getattr(f, "abnormal_attention", 1.0) for f in backing), default=1.0)
    weight = max((getattr(f, "best_source_weight", 1.0) for f in backing), default=1.0)
    weight_norm = weight / max(SOURCE_TYPE_WEIGHT.values())
    sent_score, sent_label = _sentiment_of(members)

    cand_bits = []
    for t in candidates:
        name = names.get(t)
        cand_bits.append(f"{t} — {name}" if name else t)

    lines = [
        f"PROFILE: {profile}",
        (
            f"PRE-SCORE: pre_score={min(1.0, pre / 100.0):.2f} "
            f"abnormal_attention={_abnormal_bucket(abnormal)} "
            f"source_weight={weight_norm:.2f} "
            f"pre_market_confirmation={_premarket_confirmation(candidates, features_by_ticker)}"
        ),
        f"SENTIMENT: score={sent_score:.2f} label={sent_label}",
        f"CANDIDATES: {'; '.join(cand_bits) if cand_bits else '(none)'}",
        f"CLUSTER {cluster['cluster_id']} ({len(members)} report{'s' if len(members) != 1 else ''})",
    ]
    for i, doc in enumerate(_order_members(members), start=1):
        stype = doc.get("source_type", "rss")
        if stype == "sec":
            head = f"[{i}] source=sec_edgar type=sec"
            form = _sec_form(doc)
            if form:
                head += f" form={form}"
            items = _item_codes(doc)
            if items:
                head += f" items={json.dumps(items)}"
            cik = _cik_of(doc)
            if cik:
                head += f" cik={cik}"
        elif stype == "fda":
            head = f"[{i}] source={_slug(doc.get('source', 'fda'))} type=fda"
        else:
            head = f"[{i}] source={_slug(doc.get('source', ''))} type=newswire"
        lines.append(head)
        lines.append(f"    HEADLINE: {doc.get('title', '')}")
        body = (doc.get("description") or "").strip()
        if body:
            lines.append(f"    BODY: {body[:_BODY_CAP]}")
    return "\n".join(lines)


# --- Output parsing + validation (pure) -------------------------------------- #

def parse_grade(text: Optional[str]) -> Optional[dict[str, Any]]:
    """
    Extract the one JSON object from the model's reply. Tolerates fence/prose
    disobedience: strips ``` fences, then falls back to the outermost {...}.
    """
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    for attempt in (t,):
        try:
            obj = json.loads(attempt)
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(t[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _clamp01(v: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def _num_or_none(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def validate_grade(
    raw: Any, candidates: list[str], *, _depth: int = 0
) -> Optional[dict[str, Any]]:
    """
    Normalise a parsed grade to the schema: enums enforced, numbers clamped,
    tickers restricted to the candidate list (the "never invent tickers" rule is
    enforced here too, not just in the prompt). Returns None if ``raw`` isn't a
    dict at all.
    """
    if not isinstance(raw, dict):
        return None
    cand = {c.upper() for c in candidates}

    event_type = str(raw.get("event_type", "other")).strip().lower()
    subtype = str(raw.get("subtype") or "")[:80]
    if event_type not in EVENT_TYPES:
        subtype = subtype or event_type
        event_type = "other"

    primary = raw.get("primary_ticker")
    primary = str(primary).strip().upper() if primary else None
    if primary not in cand:
        primary = None

    direction = str(raw.get("direction", "ambiguous")).strip().lower()
    if direction not in _DIRECTIONS:
        direction = "ambiguous"

    affected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw.get("affected_tickers") or []:
        if not isinstance(entry, dict):
            continue
        t = str(entry.get("ticker", "")).strip().upper()
        if t not in cand or t in seen:
            continue
        role = str(entry.get("role", "subject")).strip().lower()
        d = str(entry.get("direction", direction)).strip().lower()
        affected.append({
            "ticker": t,
            "role": role if role in _ROLES else "subject",
            "direction": d if d in _DIRECTIONS else "ambiguous",
        })
        seen.add(t)
    if not affected and primary:
        affected = [{"ticker": primary, "role": "subject", "direction": direction}]

    additional: list[dict[str, Any]] = []
    if _depth == 0:
        for extra in (raw.get("additional_catalysts") or [])[:3]:
            cleaned = validate_grade(extra, candidates, _depth=1)
            if cleaned:
                additional.append(cleaned)

    event_date = raw.get("event_date")
    event_date = str(event_date)[:32] if event_date else None

    return {
        "is_material": bool(raw.get("is_material", False)),
        "event_type": event_type,
        "subtype": subtype,
        "driver": str(raw.get("driver") or "")[:240],
        "primary_ticker": primary,
        "direction": direction,
        "magnitude": round(_clamp01(raw.get("magnitude")), 4),
        "confidence": round(_clamp01(raw.get("confidence"), default=0.5), 4),
        "is_rumor": bool(raw.get("is_rumor", False)),
        "is_forward_looking": bool(raw.get("is_forward_looking", False)),
        "is_priced_in": bool(raw.get("is_priced_in", False)),
        "event_date": event_date,
        "deal_value_usd": _num_or_none(raw.get("deal_value_usd")),
        "premium_pct": _num_or_none(raw.get("premium_pct")),
        "affected_tickers": affected,
        "additional_catalysts": additional,
        "rationale": str(raw.get("rationale") or "")[:300],
    }


def effective_score(
    grade: dict[str, Any], *, size_factor: float = 1.0, role: str = "subject"
) -> float:
    """
    Turn an intrinsic grade into a 0-100 ranking score. Magnitude is intrinsic
    (rule 7), so market-cap scaling is re-applied here via the pre-stage's
    ``size_factor``; rumor / priced-in / forward-looking discounts implement the
    prompt's "downstream discounts it" contract.
    """
    if not grade.get("is_material"):
        return 0.0
    base = grade["magnitude"] * grade["confidence"]
    if grade.get("is_rumor"):
        base *= _RUMOR_DISCOUNT
    if grade.get("is_priced_in"):
        base *= _PRICED_IN_DISCOUNT
    if grade.get("is_forward_looking"):
        base *= _FORWARD_DISCOUNT
    return round(100.0 * base * _ROLE_WEIGHT.get(role, 0.5) * size_factor, 2)


# --- Merge into ranking items (pure) ----------------------------------------- #

_DIRECTION_TO_ITEM = {"bullish": "bullish", "bearish": "bearish", "ambiguous": "neutral"}


def apply_grades_to_items(
    items: list[dict[str, Any]], graded: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Overlay cluster grades onto the quantitative shortlist items.

    Each item takes its strongest applicable grade (as primary or affected
    ticker, additional_catalysts included). Items whose every applicable grade
    is immaterial are dropped from the ranking (returned separately for the run
    audit trail); items no grade touched keep their quantitative fields.
    """
    options: dict[str, list[tuple[float, dict[str, Any], dict[str, Any], str]]] = {}
    for g in graded:
        grade = g.get("grade")
        if not grade:
            continue
        for sub in [grade, *grade.get("additional_catalysts", [])]:
            for entry in sub.get("affected_tickers", []):
                options.setdefault(entry["ticker"], []).append(
                    (0.0, sub, entry, g["cluster_id"])  # eff filled per-item below
                )

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in items:
        opts = options.get(item["ticker"])
        if not opts:
            kept.append(item)
            continue
        size = float(item.get("size_factor") or 1.0)
        scored = [
            (effective_score(sub, size_factor=size, role=entry["role"]), sub, entry, cid)
            for _, sub, entry, cid in opts
        ]
        material = [s for s in scored if s[1]["is_material"]]
        if not material:
            _, sub, entry, cid = max(scored, key=lambda s: s[1]["confidence"])
            dropped.append({
                "ticker": item["ticker"],
                "cluster_id": cid,
                "pre_score": item.get("pre_score"),
                "event_type": sub["event_type"],
                "driver": sub["driver"],
                "rationale": sub["rationale"],
            })
            continue
        eff, sub, entry, cid = max(material, key=lambda s: s[0])
        item.update({
            "catalyst_score": eff,
            "direction": _DIRECTION_TO_ITEM[entry["direction"]],
            "confidence": sub["confidence"],
            "rationale": sub["rationale"],
            "deep_read": {
                "cluster_id": cid,
                "event_type": sub["event_type"],
                "subtype": sub["subtype"],
                "driver": sub["driver"],
                "role": entry["role"],
                "direction": entry["direction"],
                "magnitude": sub["magnitude"],
                "is_rumor": sub["is_rumor"],
                "is_forward_looking": sub["is_forward_looking"],
                "is_priced_in": sub["is_priced_in"],
                "event_date": sub["event_date"],
                "deal_value_usd": sub["deal_value_usd"],
                "premium_pct": sub["premium_pct"],
            },
        })
        kept.append(item)
    return kept, dropped


# --- Optional Redis grade cache ---------------------------------------------- #

class _GradeCache:
    """
    Best-effort Redis cache; every failure degrades to uncached operation with a
    5-minute backoff before retrying the connection (REDIS_URI may be flaky —
    see .env notes — so never let it take the ranking down).
    """

    _BACKOFF_SECONDS = 300.0

    def __init__(self) -> None:
        self._client: Any = None
        self._down_until = 0.0

    async def _get_client(self) -> Any:
        if time.monotonic() < self._down_until:
            return None
        uri = os.environ.get("REDIS_URI")
        if not uri:
            self._down_until = time.monotonic() + self._BACKOFF_SECONDS
            return None
        if self._client is None:
            try:
                import redis.asyncio as aioredis
                self._client = aioredis.from_url(
                    uri, decode_responses=True,
                    socket_timeout=2.0, socket_connect_timeout=2.0,
                )
            except Exception as exc:  # noqa: BLE001
                log.info("deep-read cache unavailable (%s) — uncached", type(exc).__name__)
                self._mark_down()
                return None
        return self._client

    def _mark_down(self) -> None:
        self._client = None
        self._down_until = time.monotonic() + self._BACKOFF_SECONDS

    async def get(self, key: str) -> Optional[dict[str, Any]]:
        client = await self._get_client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            log.info("deep-read cache read failed (%s) — uncached", type(exc).__name__)
            self._mark_down()
            return None

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        client = await self._get_client()
        if client is None:
            return
        try:
            await client.set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:  # noqa: BLE001
            log.info("deep-read cache write failed (%s)", type(exc).__name__)
            self._mark_down()


_cache = _GradeCache()


def _cache_key(model: str, cluster_id: str) -> str:
    return f"catalyst:deep_read:{model}:{cluster_id}"


# --- LLM orchestration -------------------------------------------------------- #

async def _call_llm(client: Any, model: str, input_text: str) -> str:
    """One deep-read call. No sampling params (rejected with 400 on Opus 4.7/4.8)."""
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=DEEP_READ_SYSTEM,
        messages=[{"role": "user", "content": input_text}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


async def deep_read(
    docs: list[dict[str, Any]],
    shortlist: list[Any],
    *,
    profile: str,
    model: Optional[str] = None,
    ticker_extractor: Any = None,
    name_map: Optional[dict[str, str]] = None,
    max_reads: Optional[int] = None,
) -> dict[str, Any]:
    """
    Grade the shortlist's top story clusters, one LLM call per cluster.

    Returns ``{"grades": [...], "status": None|reason, "model": str,
    "clusters_considered": int, "cached": int}``. Each grades entry carries the
    cluster id/candidates, the rendered ``input``, the ``raw`` model output, and
    the validated ``grade`` (None with an ``error`` on a per-cluster failure).
    An empty ``grades`` list + status means the caller should fall back to the
    quantitative pre-score, mirroring the legacy deep read's degradation.
    """
    model = model or MODEL
    features_by_ticker = {c.ticker: c for c in shortlist}
    clusters = build_story_clusters(
        docs, set(features_by_ticker), ticker_extractor=ticker_extractor
    )
    if not clusters:
        return {"grades": [], "status": "no story clusters for shortlist", "model": model,
                "clusters_considered": 0, "cached": 0}

    def _backing(cluster: dict[str, Any]) -> float:
        return max(
            (getattr(features_by_ticker[t], "pre_score", 0.0)
             for t in cluster["candidates"] if t in features_by_ticker),
            default=0.0,
        )

    clusters.sort(key=_backing, reverse=True)
    considered = len(clusters)
    clusters = clusters[: max_reads if max_reads is not None else MAX_DEEP_READS]

    if name_map is None:
        try:
            from edgar_tickers import load_company_names
            name_map = await load_company_names()
        except Exception:  # noqa: BLE001
            name_map = {}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"grades": [], "status": "ANTHROPIC_API_KEY not set", "model": model,
                "clusters_considered": considered, "cached": 0}
    try:
        import anthropic
    except ImportError:
        return {"grades": [], "status": "anthropic package not installed", "model": model,
                "clusters_considered": considered, "cached": 0}

    client = anthropic.AsyncAnthropic(api_key=api_key)
    sem = asyncio.Semaphore(_CONCURRENCY)
    n_cached = 0

    async def _grade_one(cluster: dict[str, Any]) -> dict[str, Any]:
        nonlocal n_cached
        entry: dict[str, Any] = {
            "cluster_id": cluster["cluster_id"],
            "candidates": cluster["candidates"],
            "n_members": len(cluster["members"]),
            "input": render_cluster_input(
                cluster, profile=profile,
                features_by_ticker=features_by_ticker, name_map=name_map,
            ),
            "raw": None, "grade": None, "cached": False, "error": None,
        }
        cached = await _cache.get(_cache_key(model, cluster["cluster_id"]))
        if cached is not None:
            grade = validate_grade(cached, cluster["candidates"])
            if grade:
                entry.update({"grade": grade, "cached": True})
                n_cached += 1
                return entry
        try:
            async with sem:
                raw = await _call_llm(client, model, entry["input"])
            entry["raw"] = raw
            grade = validate_grade(parse_grade(raw), cluster["candidates"])
            if grade is None:
                entry["error"] = "unparseable model output"
            else:
                entry["grade"] = grade
                await _cache.set(
                    _cache_key(model, cluster["cluster_id"]), grade, CACHE_TTL_SECONDS
                )
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("deep read failed for %s: %s", cluster["cluster_id"], entry["error"])
        return entry

    grades = list(await asyncio.gather(*(_grade_one(c) for c in clusters)))
    ok = sum(1 for g in grades if g["grade"])
    status = None if ok else (
        grades[0]["error"] if grades and grades[0]["error"] else "all deep reads failed"
    )
    log.info(
        "deep read: %d/%d clusters graded (%d cached, %d considered, model=%s)",
        ok, len(grades), n_cached, considered, model,
    )
    return {"grades": grades, "status": status, "model": model,
            "clusters_considered": considered, "cached": n_cached}
