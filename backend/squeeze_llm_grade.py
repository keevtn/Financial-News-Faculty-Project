"""
squeeze_llm_grade.py
====================
LLM judge for the squeeze scenario bank — an independent read of the same
synthetic tapes the machine scores, so lexicon/veto/halt logic is graded
against a model that has never seen our rules.

For every scenario candidate the model receives what a desk analyst would:
short-interest stats, the social-chatter summary, and the raw headlines
(including the Nasdaq halt row text). It answers four questions per ticker —
is the covering-fuel thesis intact, how much do the headlines add to ignition
right now, which thesis-breaker (if any) is present, and is the name halted.
Agreement with the machine's read is the metric:

  veto_agreement   LLM thesis-breaker presence == machine ``thesis_broken``
  halt_agreement   LLM halted == machine halt flag
  boost_corr       Spearman(LLM news_boost, machine news_ignition) — reported
  gate             veto_agreement == 1.0 and halt_agreement == 1.0

API-credit policy (enforced here, per project policy):
  * Credits are spent ONLY on analyzing news/scenario text. No source code,
    no prompts about code, nothing but market-item digests leave this module.
  * One API call per scenario, ``MAX_TOKENS`` capped, scenario count capped.
  * Default model is ``SCENARIO_GRADE_MODEL`` env var, else a small model.
  * Nothing is written to Mongo/Redis; results live in memory.

``call_fn`` is injectable so unit tests (and constrained environments) never
touch the network: async ``(model, system, user_text) -> str``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from news_signal import evaluate_ticker_news
from squeeze_ranker import score_candidate
from squeeze_scenarios import SqueezeScenario, _T0, scenario_bank

DEFAULT_MODEL = os.environ.get("SCENARIO_GRADE_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 1000
_API_URL = "https://api.anthropic.com/v1/messages"
_DESC_CAP = 280

_SYSTEM = """You are a short-squeeze desk analyst. For each ticker you get
short-interest stats, a social-chatter summary, and recent items (headlines,
press releases, exchange notices) with ages. Judge ONLY from what is given.
Respond with exactly one JSON object:
{"reads": [{"ticker": str,
  "squeeze_viable": true or false,
  "news_boost": float 0.0-1.0,
  "thesis_breaker": "dilutive_offering"|"going_concern"|"chapter_11"|null,
  "halted": true or false,
  "rationale": str, at most 20 words}, ...]}
Requirements:
- Return exactly one read for EVERY ticker that appears as a "## TICKER"
  heading, even when its items are sparse or absent.
- news_boost measures BULLISH ignition only — how much the items add to an
  upward squeeze RIGHT NOW. Bearish or neutral items score 0.0 regardless of
  importance, and a day-old headline adds far less than a 2-hour-old one.
- thesis_breaker must be exactly one of: a dilutive share offering (announced
  or priced), explicit going-concern doubt, or a bankruptcy filing. Earnings
  misses, restructurings, layoffs or cost cuts are NOT thesis breakers.
- House convention: a thesis-breaking event older than 5 trading days
  (roughly a calendar week) is treated as absorbed by the market — set
  thesis_breaker to null in that case.
- Exchange notices count as items: a trade-halt row (e.g. from the Nasdaq
  Trade Halts feed) with a reason code and no resumption times means
  halted=true for that symbol.
No text outside the JSON."""


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def scenario_digest(sc: SqueezeScenario, *, now: Optional[datetime] = None) -> str:
    """Analyst-style digest of one scenario — market text only, never code."""
    now = now or _T0
    lines: list[str] = []
    for ticker, (short, social, sentiment, docs) in sc.candidates.items():
        lines.append(f"## {ticker}")
        spf = short.get("short_pct_float")
        lines.append(
            f"- short interest: {spf * 100:.0f}% of float short, "
            f"{short.get('short_ratio', '?')} days to cover, "
            f"float {int((short.get('float_shares') or 0) / 1e6)}M shares"
            if spf is not None else "- short interest: unknown"
        )
        if social:
            lines.append(
                f"- social (Bluesky): focus {social.get('focus_score', 0)}, "
                f"{social.get('breadth', 0)} distinct voices, engagement "
                f"{social.get('engagement', 0)}, sentiment {sentiment:+.2f}"
            )
        else:
            lines.append("- social (Bluesky): quiet, no notable chatter")
        if docs:
            lines.append("- recent items:")
            for d in docs:
                pub = d.get("published_at")
                age_h = ((now - pub).total_seconds() / 3600.0
                         if isinstance(pub, datetime) else None)
                if age_h is None:
                    age = "age unknown"
                elif age_h >= 48.0:
                    age = f"{age_h / 24.0:.1f} days ago"   # readable veto-memory ages
                else:
                    age = f"{age_h:.1f}h ago"
                desc = _strip_html(d.get("description") or "")[:_DESC_CAP]
                lines.append(
                    f"  * ({age}) [{d.get('source')}] {d.get('title')}"
                    + (f" — {desc}" if desc else "")
                )
        else:
            lines.append("- recent items: none on the wires")
    return "\n".join(lines)


async def _default_call(model: str, system: str, user_text: str) -> str:
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
            model=model, max_tokens=MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def _post() -> str:
        import urllib.request
        req = urllib.request.Request(
            _API_URL,
            data=json.dumps({
                "model": model, "max_tokens": MAX_TOKENS, "system": system,
                "messages": [{"role": "user", "content": user_text}],
            }).encode(),
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
        return "".join(b.get("text", "") for b in body.get("content", [])
                       if b.get("type") == "text")

    return await asyncio.to_thread(_post)


def _parse_reads(raw: str) -> dict[str, dict[str, Any]]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {str(r.get("ticker", "")).upper(): r
            for r in parsed.get("reads", []) if r.get("ticker")}


def _machine_reads(sc: SqueezeScenario) -> dict[str, Any]:
    """The production pipeline's read of the same tape (no network)."""
    out = {}
    for ticker, (short, social, sentiment, docs) in sc.candidates.items():
        news = evaluate_ticker_news(docs, ticker, now=_T0)
        c = score_candidate(ticker, short, social, sentiment, news=news)
        out[ticker] = c
    return out


async def grade_scenario_with_llm(
    sc: SqueezeScenario,
    *,
    model: Optional[str] = None,
    call_fn: Optional[Callable[[str, str, str], Awaitable[str]]] = None,
) -> dict[str, Any]:
    """One scenario: LLM read vs machine read, per candidate ticker."""
    raw = await (call_fn or _default_call)(
        model or DEFAULT_MODEL, _SYSTEM, scenario_digest(sc))
    reads = _parse_reads(raw)
    machine = _machine_reads(sc)

    rows = []
    for ticker, cand in machine.items():
        r = reads.get(ticker) or {}
        llm_broken = r.get("thesis_breaker") is not None
        llm_halted = bool(r.get("halted"))
        mach_halted = cand.halted is not None and not (cand.halted or {}).get("resumed")
        rows.append({
            "ticker": ticker,
            "llm_thesis_breaker": r.get("thesis_breaker"),
            "machine_thesis_broken": cand.thesis_broken,
            "veto_agree": llm_broken == cand.thesis_broken,
            "llm_halted": llm_halted,
            "machine_halted": mach_halted,
            "halt_agree": llm_halted == mach_halted,
            "llm_news_boost": r.get("news_boost"),
            "machine_news_ignition": cand.news_ignition,
            "llm_viable": r.get("squeeze_viable"),
            "rationale": r.get("rationale", ""),
            "answered": bool(r),
        })
    return {
        "scenario": sc.name,
        "rows": rows,
        "veto_agree": sum(r["veto_agree"] for r in rows),
        "halt_agree": sum(r["halt_agree"] for r in rows),
        "answered": sum(r["answered"] for r in rows),
        "total": len(rows),
    }


async def grade_bank_with_llm(
    scenarios: Optional[list[SqueezeScenario]] = None,
    *,
    model: Optional[str] = None,
    max_scenarios: int = 10,
    call_fn: Optional[Callable[[str, str, str], Awaitable[str]]] = None,
) -> dict[str, Any]:
    """Grade up to ``max_scenarios`` scenarios (cost cap); aggregate agreement."""
    results = []
    for sc in (scenarios or scenario_bank())[:max_scenarios]:
        results.append(await grade_scenario_with_llm(sc, model=model, call_fn=call_fn))

    total = sum(r["total"] for r in results)
    veto = sum(r["veto_agree"] for r in results)
    halt = sum(r["halt_agree"] for r in results)
    answered = sum(r["answered"] for r in results)

    pairs = [(r2["llm_news_boost"], r2["machine_news_ignition"])
             for r in results for r2 in r["rows"]
             if r2["llm_news_boost"] is not None
             and r2["machine_news_ignition"] is not None]
    corr = None
    if len(pairs) >= 3:
        from catalyst_backtest import _spearman
        corr = _spearman([p[0] for p in pairs], [p[1] for p in pairs])

    report = {
        "model": model or DEFAULT_MODEL,
        "graded_at": datetime.now(tz=timezone.utc).isoformat(),
        "n_scenarios": len(results),
        "tickers_total": total,
        "answered": answered,
        "veto_agreement": round(veto / total, 4) if total else None,
        "halt_agreement": round(halt / total, 4) if total else None,
        "news_boost_corr": round(corr, 4) if corr is not None else None,
        "results": results,
    }
    report["passed"] = bool(
        total and answered == total
        and report["veto_agreement"] == 1.0
        and report["halt_agreement"] == 1.0
    )
    return report


def main() -> None:
    """CLI: grade the squeeze bank with the LLM; exit 0 only on full agreement."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        sys.exit("squeeze_llm_grade requires ANTHROPIC_API_KEY")
    report = asyncio.run(grade_bank_with_llm())
    verdict = "PASS" if report["passed"] else "FAIL"
    print(f"[squeeze llm grade] {verdict}  model={report['model']}"
          f"  veto_agreement={report['veto_agreement']}"
          f"  halt_agreement={report['halt_agreement']}"
          f"  boost_corr={report['news_boost_corr']}"
          f"  answered={report['answered']}/{report['tickers_total']}")
    for res in report["results"]:
        for row in res["rows"]:
            if not (row["veto_agree"] and row["halt_agree"]):
                print(f"  disagree {res['scenario']}/{row['ticker']}: "
                      f"llm_breaker={row['llm_thesis_breaker']} vs machine={row['machine_thesis_broken']}; "
                      f"llm_halted={row['llm_halted']} vs machine={row['machine_halted']}; "
                      f"({row['rationale']})")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
