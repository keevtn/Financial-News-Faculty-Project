"""
squeeze_scenarios.py
====================
Scenario bank + pure evaluation loop for the news-aware squeeze pipeline —
the squeeze cousin of ``catalyst_scenarios.py``.

Each scenario is a synthetic squeeze tape with **known expectations**, pushed
through the *real* pipeline — ``news_signal.evaluate_ticker_news`` over
realistic docs (including a to-the-byte Nasdaq Trade Halts item) into
``squeeze_ranker.score_candidate`` — so it measures exactly what production
runs. Checks are structural (veto fired, flag set, ranking order) rather than
direction-accuracy, because squeeze ground truth is "did the machinery read
the tape right", not a price move.

The bank encodes the three agreed launch traps:
  * offering-kills-squeeze   — hot social must NOT rank a diluted name "firing"
  * news-ignites-quiet-name  — a fresh wire catalyst lifts a socially dead name
  * halt-on-fueled-name      — T1 on a loaded name surfaces with its code
plus decay, flat veto memory, syndication-capping, and bearish-wire traps.

DATABASE SAFETY: this module imports no storage layer and takes no client —
it *cannot* write to Mongo or Redis. Synthetic runs can never contaminate
live grading data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from news_signal import HALT_SOURCE, evaluate_ticker_news
from squeeze_ranker import SqueezeCandidate, score_candidate

# Thu 2026-07-02 18:00 UTC — 2026-07-03 is a market holiday, so the flat
# 5-trading-day veto window spans both a weekend and a holiday.
_T0 = datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc)

_LOADED = {"short_pct_float": 0.35, "short_ratio": 7.0, "float_shares": 25e6}
_HOT_SOCIAL = {"focus_score": 10.0, "engagement": 150, "n_posts": 40,
               "breadth": 9, "sources": ["bluesky"], "top_posts": []}


def _doc(title: str, hours_ago: float, *, source: str = "Reuters",
         stype: str = "rss", desc: str = "", tickers: tuple[str, ...] = (),
         sent: Optional[float] = None) -> dict[str, Any]:
    return {
        "title": title, "description": desc, "source": source,
        "source_type": stype, "published_at": _T0 - timedelta(hours=hours_ago),
        "tickers": list(tickers),
        "sentiment": {"score": sent} if sent is not None else None,
    }


def _at(title: str, when: datetime, tickers: tuple[str, ...]) -> dict[str, Any]:
    d = _doc(title, 0.0, tickers=tickers)
    d["published_at"] = when
    return d


def _halt_item(sym: str, hours_ago: float, code: str = "T1") -> dict[str, Any]:
    """Byte-faithful Nasdaq Trade Halts item: bare-symbol title, no ticker
    tags, reason code inside the description table, empty resumption cells."""
    desc = (
        '<table width="100%" cellpadding="5"><tr><th align="left">Halt Date</th>'
        '<th align="left">Reason Code</th></tr>'
        f'<tr><td valign="top">07/02/2026</td><td valign="top">13:50:00.000</td>'
        f'<td valign="top">{sym}</td><td valign="top">Synthetic Corp</td>'
        f'<td valign="top">NASDAQ</td><td valign="top">{code}</td>'
        '<td valign="top"></td><td valign="top"></td><td valign="top"></td>'
        '<td valign="top"></td></tr></table>'
    )
    return {"title": sym, "description": desc, "source": HALT_SOURCE,
            "source_type": "rss", "published_at": _T0 - timedelta(hours=hours_ago),
            "tickers": []}


@dataclass
class SqueezeScenario:
    """One synthetic tape with structural expectations."""

    name: str
    # ticker -> (short metrics, social snapshot|None, social sentiment, news docs)
    candidates: dict[str, tuple[dict, Optional[dict], float, list[dict]]]
    checks: list[Callable[[dict[str, SqueezeCandidate], list[str]], tuple[str, bool, str]]] = field(
        default_factory=list)


def _run_candidates(
    sc: SqueezeScenario,
) -> tuple[dict[str, SqueezeCandidate], list[str]]:
    """The real pipeline: per-ticker news read -> score -> rank."""
    by_ticker: dict[str, SqueezeCandidate] = {}
    for ticker, (short, social, sentiment, docs) in sc.candidates.items():
        news = evaluate_ticker_news(docs, ticker, now=_T0)
        by_ticker[ticker] = score_candidate(ticker, short, social, sentiment, news=news)
    ranked = sorted(by_ticker, key=lambda t: by_ticker[t].squeeze_score, reverse=True)
    return by_ticker, ranked


# --- check helpers ------------------------------------------------------------ #

def _check(name: str, ok: bool, detail: str = "") -> tuple[str, bool, str]:
    return (name, bool(ok), detail)


def _broken(t: str, expect: bool = True):
    def c(by, ranked):
        got = by[t].thesis_broken
        return _check(f"{t}_thesis_broken={expect}", got is expect,
                      f"got {got}, veto={by[t].veto}")
    return c


def _outranks(hi: str, lo: str):
    def c(by, ranked):
        return _check(f"{hi}_outranks_{lo}",
                      by[hi].squeeze_score > by[lo].squeeze_score,
                      f"{by[hi].squeeze_score} vs {by[lo].squeeze_score}")
    return c


def scenario_bank() -> list[SqueezeScenario]:
    return [
        # ---- trap 1: offering on a loaded, chattering name -----------------
        SqueezeScenario(
            name="offering_kills_squeeze",
            candidates={
                "NOVA": (_LOADED, dict(_HOT_SOCIAL), 0.6, [
                    _doc("Nova Minerals announces $50 million public offering",
                         28.0, source="GlobeNewswire", tickers=("NOVA",),
                         desc="Registered direct offering priced at a discount."),
                ]),
                "CLEN": (_LOADED, dict(_HOT_SOCIAL), 0.6, []),
            },
            checks=[
                _broken("NOVA", True),
                _broken("CLEN", False),
                lambda by, r: _check("NOVA_ignition_zeroed",
                                     by["NOVA"].ignition_score == 0.0,
                                     str(by["NOVA"].ignition_score)),
                lambda by, r: _check("NOVA_not_called_bullish",
                                     by["NOVA"].direction == "neutral",
                                     by["NOVA"].direction),
                lambda by, r: _check("NOVA_veto_reason",
                                     (by["NOVA"].veto or {}).get("reason")
                                     == "dilutive_offering",
                                     str(by["NOVA"].veto)),
                _outranks("CLEN", "NOVA"),
            ],
        ),
        # ---- trap 2: fresh wire catalyst on a socially dead name -----------
        SqueezeScenario(
            name="news_ignites_quiet_name",
            candidates={
                "QUIT": (_LOADED, None, 0.0, [
                    _doc("Quiet Therapeutics receives FDA approval for lead drug",
                         3.0, source="Reuters", tickers=("QUIT",),
                         desc="The FDA approved the company's lead candidate."),
                    _doc("Quiet Therapeutics wins FDA approval, analysts see upside",
                         3.4, source="MarketWatch", tickers=("QUIT",)),
                ]),
                "DEAD": (_LOADED, None, 0.0, []),
            },
            checks=[
                lambda by, r: _check("QUIT_news_ignition_strong",
                                     (by["QUIT"].news_ignition or 0) >= 0.5,
                                     str(by["QUIT"].news_ignition)),
                _outranks("QUIT", "DEAD"),
                lambda by, r: _check("QUIT_ranked_first", r[0] == "QUIT", str(r)),
            ],
        ),
        # ---- trap 3: T1 halt on a fueled name -------------------------------
        SqueezeScenario(
            name="halt_on_fueled_name",
            candidates={
                "HALT": (_LOADED, dict(_HOT_SOCIAL), 0.3, [_halt_item("HALT", 2.0)]),
            },
            checks=[
                lambda by, r: _check("HALT_flag_code_T1",
                                     (by["HALT"].halted or {}).get("code") == "T1",
                                     str(by["HALT"].halted)),
                lambda by, r: _check("HALT_not_resumed",
                                     (by["HALT"].halted or {}).get("resumed") is False,
                                     str(by["HALT"].halted)),
                _broken("HALT", False),   # a halt flags; it does not veto
            ],
        ),
        # ---- decay: yesterday's catalyst is not today's ---------------------
        SqueezeScenario(
            name="stale_catalyst_fades",
            candidates={
                "FRSH": (_LOADED, dict(_HOT_SOCIAL), 0.4, [
                    _doc("Fresh Bio wins FDA approval", 2.0, tickers=("FRSH",)),
                ]),
                "STAL": (_LOADED, dict(_HOT_SOCIAL), 0.4, [
                    _doc("Stale Bio wins FDA approval", 20.0, tickers=("STAL",)),
                ]),
            },
            checks=[
                lambda by, r: _check("fresh_news_ignition_higher",
                                     (by["FRSH"].news_ignition or 0)
                                     > (by["STAL"].news_ignition or 0) > 0,
                                     f"{by['FRSH'].news_ignition} vs {by['STAL'].news_ignition}"),
                _outranks("FRSH", "STAL"),
            ],
        ),
        # ---- veto memory: flat five trading days, then forgotten ------------
        SqueezeScenario(
            name="offering_memory_flat_five_days",
            candidates={
                # Jun 26 = 4 trading days before Thu Jul 2 -> still broken
                "OLDD": (_LOADED, dict(_HOT_SOCIAL), 0.5, [
                    _at("Oldd Corp prices public offering",
                        datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc), ("OLDD",)),
                ]),
                # Jun 24 = 7 trading days back -> outside the window
                "ANCN": (_LOADED, dict(_HOT_SOCIAL), 0.5, [
                    _at("Ancn Corp prices public offering",
                        datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc), ("ANCN",)),
                ]),
            },
            checks=[_broken("OLDD", True), _broken("ANCN", False),
                    _outranks("ANCN", "OLDD")],
        ),
        # ---- syndication: reprints confirm, they don't multiply -------------
        SqueezeScenario(
            name="syndication_capped",
            candidates={
                "SYND": (_LOADED, None, 0.0, [
                    _doc("Synd Bio wins FDA approval", 3.0, source=s, tickers=("SYND",))
                    for s in ("Reuters", "MarketWatch", "GlobeNewswire", "Benzinga")
                ]),
                "SOLO": (_LOADED, None, 0.0, [
                    _doc("Solo Bio wins FDA approval", 3.0, tickers=("SOLO",)),
                ]),
            },
            checks=[
                lambda by, r: _check("reprints_confirm_slightly",
                                     (by["SYND"].news_ignition or 0)
                                     >= (by["SOLO"].news_ignition or 0),
                                     f"{by['SYND'].news_ignition} vs {by['SOLO'].news_ignition}"),
                lambda by, r: _check("reprints_never_multiply",
                                     (by["SYND"].news_ignition or 0)
                                     <= (by["SOLO"].news_ignition or 1) * 1.35,
                                     f"{by['SYND'].news_ignition} vs {by['SOLO'].news_ignition}"),
            ],
        ),
        # ---- bearish wire: bad news is not ignition (and not a veto) --------
        SqueezeScenario(
            name="bearish_wire_stays_cold",
            candidates={
                "BEAR": (_LOADED, None, 0.0, [
                    _doc("Bear Corp misses estimates, announces restructuring",
                         1.0, tickers=("BEAR",), sent=-0.7,
                         desc="Weak quarter; cost cuts planned."),
                ]),
            },
            checks=[
                lambda by, r: _check("no_ignition_from_bad_news",
                                     (by["BEAR"].news_ignition or 0) <= 0.1,
                                     str(by["BEAR"].news_ignition)),
                _broken("BEAR", False),   # a miss hurts, but it isn't thesis-breaking
            ],
        ),
    ]


# --- evaluation ---------------------------------------------------------------- #

def run_scenario(sc: SqueezeScenario) -> dict[str, Any]:
    by_ticker, ranked = _run_candidates(sc)
    checks = [c(by_ticker, ranked) for c in sc.checks]
    return {
        "scenario": sc.name,
        "ranked": ranked,
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
        "checks_passed": sum(1 for _, ok, _ in checks if ok),
        "checks_total": len(checks),
        "passed": all(ok for _, ok, _ in checks),
    }


def run_squeeze_bank(scenarios: Optional[list[SqueezeScenario]] = None) -> dict[str, Any]:
    """Run every squeeze scenario; a bank 'passes' only when every check holds."""
    results = [run_scenario(s) for s in (scenarios or scenario_bank())]
    return {
        "n_scenarios": len(results),
        "scenarios_passed": sum(1 for r in results if r["passed"]),
        "checks_passed": sum(r["checks_passed"] for r in results),
        "checks_total": sum(r["checks_total"] for r in results),
        "all_passed": all(r["passed"] for r in results),
        "results": results,
    }
