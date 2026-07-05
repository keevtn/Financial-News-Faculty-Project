"""
scenario_llm_grade.py
=====================
LLM grading of scenario shortlists — measures the *deep-read* layer's accuracy
against known ground truth, the part the quantitative harness can't test.

API-credit policy (enforced here, per project policy):
  * Credits are spent ONLY on analyzing news text. No source code, no prompts
    about code, nothing but article digests ever leaves this module.
  * One API call per scenario, ``MAX_TOKENS`` capped, scenario count capped by
    ``max_scenarios``. Default model is ``SCENARIO_GRADE_MODEL`` env var, else
    a small model — smoke tests shouldn't burn Opus-class credits.
  * Nothing is written to Mongo/Redis; results are returned in memory.

Uses the ``anthropic`` package when installed (same as catalyst_deep_read);
otherwise falls back to a stdlib HTTPS call so constrained test environments
can still run the smoke test.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional

from catalyst_ranker import build_candidates, score_candidates
from catalyst_scenarios import Scenario, _apply_live_sentiment

DEFAULT_MODEL = os.environ.get("SCENARIO_GRADE_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 1200
_API_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM = """You are a sell-side analyst grading pre-market news catalysts.
For each ticker, judge ONLY from the articles given. Respond with exactly one
JSON object: {"grades": [{"ticker": str, "direction": "bullish"|"bearish"|"neutral",
"magnitude": float 0.0-1.0, "rationale": str <= 20 words}, ...]}
magnitude = how large an open-to-close move the news plausibly drives
(0.1 routine, 0.5 material, 0.9 extreme). No text outside the JSON."""


def _digest(scenario: Scenario, top_k: int = 6) -> tuple[str, list[str]]:
    """Compact article digest for the scenario's shortlist tickers."""
    _apply_live_sentiment(scenario.docs)
    ranked = score_candidates(
        build_candidates(scenario.docs, scenario.baseline), min_sources=1
    )[:top_k]
    lines: list[str] = []
    tickers: list[str] = []
    for c in ranked:
        tickers.append(c.ticker)
        lines.append(f"## {c.ticker}")
        for a in c.sample_articles[:4]:
            desc = (a.get("description") or "")[:300]
            lines.append(f"- [{a['source']}] {a['title']}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines), tickers


async def _call_api(model: str, user_text: str) -> str:
    """One messages call; anthropic SDK if present, stdlib HTTPS otherwise."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    try:
        import anthropic
    except ImportError:
        anthropic = None
    if anthropic is not None:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def _post() -> str:
        import urllib.request
        req = urllib.request.Request(
            _API_URL,
            data=json.dumps({
                "model": model, "max_tokens": MAX_TOKENS, "system": _SYSTEM,
                "messages": [{"role": "user", "content": user_text}],
            }).encode(),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
        return "".join(
            b.get("text", "") for b in body.get("content", [])
            if b.get("type") == "text"
        )

    return await asyncio.to_thread(_post)


def _parse_grades(raw: str) -> dict[str, dict[str, Any]]:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {
        str(g.get("ticker", "")).upper(): g
        for g in parsed.get("grades", []) if g.get("ticker")
    }


async def grade_scenario_with_llm(
    scenario: Scenario, *, model: Optional[str] = None
) -> dict[str, Any]:
    """Grade one scenario's shortlist; compare LLM reads to ground truth."""
    digest, tickers = _digest(scenario)
    raw = await _call_api(model or DEFAULT_MODEL, digest)
    grades = _parse_grades(raw)
    rows = []
    for ticker, true_move in scenario.truth.items():
        g = grades.get(ticker)
        true_dir = ("bullish" if true_move > 2.0 else
                    "bearish" if true_move < -2.0 else "neutral")
        pred = (g or {}).get("direction", "missing")
        rows.append({
            "ticker": ticker,
            "llm_direction": pred,
            "true_direction": true_dir,
            "direction_hit": pred == true_dir,
            "llm_magnitude": (g or {}).get("magnitude"),
            "true_abs_move": abs(true_move),
            "rationale": (g or {}).get("rationale", ""),
        })
    return {"scenario": scenario.name, "shortlist": tickers, "rows": rows,
            "hits": sum(r["direction_hit"] for r in rows), "total": len(rows)}


async def grade_bank_with_llm(
    scenarios: list[Scenario], *, model: Optional[str] = None,
    max_scenarios: int = 12,
) -> dict[str, Any]:
    """Grade up to ``max_scenarios`` scenarios (cost cap); aggregate accuracy."""
    results = []
    for s in scenarios[:max_scenarios]:
        results.append(await grade_scenario_with_llm(s, model=model))
    hits = sum(r["hits"] for r in results)
    total = sum(r["total"] for r in results)
    # magnitude sanity: Spearman between LLM magnitude and realized |move|
    pairs = [
        (r2["llm_magnitude"], r2["true_abs_move"])
        for r in results for r2 in r["rows"] if r2["llm_magnitude"] is not None
    ]
    corr = None
    if len(pairs) >= 3:
        from catalyst_backtest import _spearman
        corr = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
    return {
        "model": model or DEFAULT_MODEL,
        "n_scenarios": len(results),
        "direction_accuracy": round(hits / total, 4) if total else None,
        "magnitude_rank_corr": round(corr, 4) if corr is not None else None,
        "results": results,
    }
