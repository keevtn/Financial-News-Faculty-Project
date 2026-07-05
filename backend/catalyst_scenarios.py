"""
catalyst_scenarios.py
=====================
Scenario bank + pure evaluation loop for the catalyst pipeline.

Each scenario is a synthetic overnight news window with **known ground truth**
(direction and realized move per ticker), pushed through the *real* pipeline —
live lexicon sentiment, ``build_candidates``, ``score_candidates`` — so it
measures exactly what production runs, end to end.

Why synthetic: with only ~11 graded live runs, per-event-type accuracy is
unmeasurable from history alone. The bank encodes one clear case per catalyst
class (FDA approval, CRL, guidance cut, beat-and-raise, dilution, chapter 11,
short report, upgrade…) plus deliberate traps: syndicated reprints that must
cluster to one story, a mega-cap news-volume magnet that must not outrank a
small-cap catalyst, and an incidental body-mention that must not win a
deep-read slot.

DATABASE SAFETY: this module imports no storage layer and takes no client —
it *cannot* write to Mongo or Redis. Scenario results live in memory (and a
local JSON report if the CLI is asked to save one). Synthetic runs therefore
can never contaminate live grading data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from catalyst_backtest import _spearman
from catalyst_ranker import _direction_from_sentiment, build_candidates, score_candidates
from sentiment import LoughranMcDonaldAnalyzer

_T0 = datetime(2026, 7, 1, 1, 30, tzinfo=timezone.utc)  # overnight window


@dataclass
class Scenario:
    """One overnight window with ground truth."""

    name: str
    docs: list[dict[str, Any]]
    # ticker -> true open->close % move of the graded session (signed)
    truth: dict[str, float]
    # trailing daily mention baseline (for abnormal-attention)
    baseline: dict[str, float] = field(default_factory=dict)
    # tickers that must NOT out-rank every truth ticker (traps/distractors)
    distractors: list[str] = field(default_factory=list)


def _doc(
    title: str,
    tickers: tuple[str, ...],
    *,
    source: str = "Reuters",
    source_type: str = "rss",
    description: str = "",
    minutes: int = 0,
) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "tickers": list(tickers),
        "source": source,
        "source_type": source_type,
        "url": f"https://example.com/{abs(hash(title)) % 10**8}",
        "published_at": _T0 + timedelta(minutes=minutes),
        "content_hash": f"h{abs(hash(title)) % 10**12}",
        "sentiment": None,  # filled by the harness with the live analyzer
    }


def _syndicate(title: str, tickers: tuple[str, ...], outlets: list[str],
               description: str = "") -> list[dict[str, Any]]:
    """Same story reprinted by several outlets — must cluster to one story."""
    return [
        _doc(title, tickers, source=o, description=description, minutes=i * 7)
        for i, o in enumerate(outlets)
    ]


def scenario_bank() -> list[Scenario]:
    """One clear scenario per catalyst class, plus ranking traps."""
    wires = ["Reuters", "GlobeNewswire", "PR Newswire", "MarketWatch"]
    return [
        Scenario(
            name="fda_approval_small_cap",
            docs=_syndicate(
                "Aurelia Therapeutics receives FDA approval for lead cancer drug",
                ("AURX",), wires,
                description="The FDA approved aurelizumab for second-line NSCLC.",
            ) + [_doc("Aurelia wins FDA approval, analysts see upside", ("AURX",),
                      source="CNBC", minutes=41)],
            truth={"AURX": 38.0},
            baseline={"AURX": 0.4},
        ),
        Scenario(
            name="crl_rejection",
            docs=_syndicate(
                "FDA issues complete response letter for Nimbus Pharma's NDA",
                ("NMBP",), wires[:3],
                description="Nimbus said the agency cited manufacturing deficiencies.",
            ),
            truth={"NMBP": -42.0},
            baseline={"NMBP": 0.5},
        ),
        Scenario(
            name="guidance_cut",
            docs=_syndicate(
                "Vektra Systems cuts guidance on weak enterprise demand",
                ("VKTR",), ["Reuters", "MarketWatch", "Barron's"],
                description="Full-year revenue outlook lowered; shares under pressure.",
            ),
            truth={"VKTR": -18.0},
            baseline={"VKTR": 1.0},
        ),
        Scenario(
            name="beat_and_raise",
            docs=_syndicate(
                "Corvid Industrial beats estimates and raises guidance",
                ("CRVD",), ["Reuters", "CNBC", "GlobeNewswire"],
                description="Record quarter on strong margins; buyback expanded.",
            ),
            truth={"CRVD": 9.0},
            baseline={"CRVD": 1.2},
        ),
        Scenario(
            name="dilutive_offering",
            docs=_syndicate(
                "Helios BioSciences announces $60 million public offering",
                ("HLBS",), ["GlobeNewswire", "PR Newswire", "Benzinga"],
                description="Registered direct offering priced at a discount.",
            ),
            truth={"HLBS": -14.0},
            baseline={"HLBS": 0.3},
        ),
        Scenario(
            name="chapter_11",
            docs=_syndicate(
                "Meridian Retail files for chapter 11 bankruptcy protection",
                ("MRDN",), wires[:3],
                description="Going concern doubt disclosed; stores to close.",
            ),
            truth={"MRDN": -55.0},
            baseline={"MRDN": 0.6},
        ),
        Scenario(
            name="short_report",
            docs=_syndicate(
                "Short report alleges fraud at Quantia Logistics",
                ("QNTL",), ["MarketWatch", "Benzinga", "Reuters"],
                description="Research firm discloses short position, alleges misconduct.",
            ),
            truth={"QNTL": -21.0},
            baseline={"QNTL": 0.8},
        ),
        Scenario(
            name="sec_8k_material",
            docs=[
                _doc("Form 8-K: Talos Energy — material definitive agreement, merger",
                     ("TALO",), source="SEC EDGAR", source_type="sec",
                     description="Merger agreement with strategic acquirer at premium."),
                _doc("Talos Energy surges on merger agreement", ("TALO",),
                     source="Reuters", minutes=25),
                _doc("Talos to be acquired at 34% premium", ("TALO",),
                     source="CNBC", minutes=44),
            ],
            truth={"TALO": 31.0},
            baseline={"TALO": 0.7},
        ),
        Scenario(
            name="analyst_upgrade_mild",
            docs=[
                _doc("Sable Foods upgraded to buy rating at Fenwick, price target raised",
                     ("SBLF",), source="MarketWatch",
                     description="Analyst cites improving margins and solid momentum."),
                _doc("Fenwick turns bullish on Sable Foods", ("SBLF",),
                     source="Benzinga", minutes=18),
            ],
            truth={"SBLF": 3.0},
            baseline={"SBLF": 0.9},
        ),
        # ---- traps ---------------------------------------------------------
        Scenario(
            name="megacap_noise_vs_smallcap_catalyst",
            docs=[
                # 6 routine mega-cap mentions (its normal daily volume)
                _doc(f"Titan Micro {t}", ("TTNM",), source=s, minutes=i * 9)
                for i, (t, s) in enumerate([
                    ("in focus as index rebalances", "MarketWatch"),
                    ("supplier notes steady orders", "Reuters"),
                    ("featured in AI roundup", "CNBC"),
                    ("analyst maintains hold rating", "Benzinga"),
                    ("to present at tech conference", "PR Newswire"),
                    ("options volume in line with average", "Barron's"),
                ])
            ] + _syndicate(
                "Cobalt Dynamics wins FDA approval for wearable cardiac monitor",
                ("CBLT",), ["GlobeNewswire", "Reuters", "MarketWatch"],
                description="Breakthrough device approval; launch this quarter.",
            ),
            truth={"CBLT": 27.0, "TTNM": 0.4},
            baseline={"TTNM": 6.0, "CBLT": 0.3},
            distractors=["TTNM"],
        ),
        Scenario(
            name="incidental_body_mention",
            docs=[
                _doc("Nexa Grid misses estimates, guidance lowered", ("NXGR",),
                     source="Reuters",
                     description="Weak backlog; management cautious on second half."),
                _doc("Nexa Grid results weigh on sector", ("NXGR", "VOLT"),
                     source="MarketWatch", minutes=30,
                     description="Peers including Voltaic Systems traded lower in sympathy."),
                _doc("Utility sector wrap: soft quarter across the board",
                     ("NXGR", "VOLT"), source="Benzinga", minutes=55,
                     description="Voltaic Systems mentioned among peers."),
            ],
            truth={"NXGR": -12.0, "VOLT": -1.1},
            baseline={"NXGR": 0.5, "VOLT": 0.5},
            distractors=["VOLT"],
        ),
        Scenario(
            name="quiet_tape_no_catalyst",
            docs=[
                _doc("Harbor Bancorp declares quarterly dividend of $0.22", ("HRBB",),
                     source="PR Newswire"),
                _doc("Harbor Bancorp to speak at community banking forum", ("HRBB",),
                     source="GlobeNewswire", minutes=20),
            ],
            truth={"HRBB": 0.2},
            baseline={"HRBB": 0.4},
        ),
    ]


# --- evaluation --------------------------------------------------------------- #

def _apply_live_sentiment(docs: list[dict[str, Any]]) -> None:
    """Score docs with the production lexicon analyzer (end-to-end test)."""
    analyzer = LoughranMcDonaldAnalyzer()
    for d in docs:
        r = analyzer.analyze_text_batch([(d["title"], d.get("description", ""))])[0]
        d["sentiment"] = {"score": r.score, "label": r.label,
                          "confidence": r.confidence}


def run_scenario(
    scenario: Scenario, *, weights: Optional[dict[str, float]] = None,
    min_sources: int = 2,
) -> dict[str, Any]:
    """Push one scenario through the real pipeline and grade it."""
    _apply_live_sentiment(scenario.docs)
    candidates = build_candidates(scenario.docs, scenario.baseline)
    ranked = score_candidates(candidates, min_sources=min_sources, weights=weights)
    by_ticker = {c.ticker: c for c in ranked}

    rows = []
    for ticker, true_move in scenario.truth.items():
        c = by_ticker.get(ticker)
        pred_dir = _direction_from_sentiment(c.mean_sentiment) if c else "missed"
        # +/-2%: moves inside the band are intraday noise, not direction.
        true_dir = ("bullish" if true_move > 2.0 else
                    "bearish" if true_move < -2.0 else "neutral")
        rows.append({
            "ticker": ticker,
            "surfaced": c is not None,
            "pre_score": c.pre_score if c else None,
            "pred_direction": pred_dir,
            "true_direction": true_dir,
            "direction_hit": (pred_dir == true_dir) if c else False,
            "true_move": true_move,
            "abs_move": abs(true_move),
        })

    scored = [r for r in rows if r["pre_score"] is not None]
    rank_corr = (
        _spearman([r["pre_score"] for r in scored], [r["abs_move"] for r in scored])
        if len(scored) >= 2 else None
    )
    # trap check: every distractor must rank below the best true catalyst
    biggest = max(scenario.truth, key=lambda t: abs(scenario.truth[t]))
    big_score = (by_ticker[biggest].pre_score if biggest in by_ticker else -1.0)
    traps_ok = all(
        by_ticker.get(d) is None or by_ticker[d].pre_score < big_score
        for d in scenario.distractors
    )
    return {
        "scenario": scenario.name,
        "rows": rows,
        "direction_hits": sum(r["direction_hit"] for r in rows),
        "direction_total": len(rows),
        "catalyst_surfaced": any(
            r["surfaced"] for r in rows if abs(r["true_move"]) > 5.0
        ) if any(abs(r["true_move"]) > 5.0 for r in rows) else True,
        "rank_corr": rank_corr,
        "traps_ok": traps_ok,
    }


def as_graded_run(result: dict[str, Any]) -> dict[str, Any]:
    """Scenario result in the persisted graded-run shape, so the calibration
    and backtest tooling (recommend_weights, fit_expected_move) can consume
    scenario evidence exactly like live grades."""
    return {
        "items": [
            {"ticker": r["ticker"], "catalyst_score": r["pre_score"],
             "pre_score": r["pre_score"]}
            for r in result["rows"] if r["pre_score"] is not None
        ],
        "metrics": {"per_ticker": [
            {"ticker": r["ticker"], "abs_move": r["abs_move"],
             "direction_hit": r["direction_hit"]}
            for r in result["rows"] if r["pre_score"] is not None
        ]},
    }


def run_bank(
    scenarios: Optional[list[Scenario]] = None,
    *, weights: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Run every scenario; aggregate the pass/fail metrics."""
    results = [run_scenario(s, weights=weights) for s in (scenarios or scenario_bank())]
    hits = sum(r["direction_hits"] for r in results)
    total = sum(r["direction_total"] for r in results)
    corrs = [r["rank_corr"] for r in results if r["rank_corr"] is not None]
    return {
        "n_scenarios": len(results),
        "direction_accuracy": round(hits / total, 4) if total else None,
        "catalyst_recall": round(
            sum(1.0 for r in results if r["catalyst_surfaced"]) / len(results), 4),
        "avg_rank_corr": round(sum(corrs) / len(corrs), 4) if corrs else None,
        "traps_passed": sum(1 for r in results if r["traps_ok"]),
        "traps_total": len(results),
        "results": results,
    }
