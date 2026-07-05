"""
run_scenario_eval.py
====================
CLI: scenario accuracy runs + threshold-gated optimization loop.

Modes
-----
    python run_scenario_eval.py                 # one pass, print report
    python run_scenario_eval.py --optimize      # loop: run -> tune weights -> rerun
    python run_scenario_eval.py --replay        # also replay graded Mongo runs (read-only)
    python run_scenario_eval.py --save out.json # write the report to a local file

Thresholds (a run "passes" when all hold):
    direction_accuracy >= 0.70
    catalyst_recall    >= 0.90
    avg_rank_corr      >= 0.50
    traps: all passed

API-credit policy (hard-coded here, per project policy):
  * Anthropic credits are spent ONLY on analyzing news (the deep-read grading
    of scenario shortlists), never on code tasks. This script never sends
    source code to the API.
  * LLM grading is OFF by default; enable explicitly with --llm.
  * Scenario runs are never persisted to Mongo/Redis — synthetic data cannot
    reach the live database. --replay opens a read-only cursor and writes
    nothing back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Optional

from catalyst_backtest import DEFAULT_WEIGHTS
from catalyst_calibration import fit_expected_move, recommend_weights
from catalyst_scenarios import as_graded_run, run_bank, scenario_bank
from squeeze_scenarios import run_squeeze_bank

THRESHOLDS = {
    "direction_accuracy": 0.70,
    "catalyst_recall": 0.90,
    "avg_rank_corr": 0.50,
}
MAX_ITERATIONS = 6


def passes(report: dict[str, Any]) -> bool:
    if report["traps_passed"] < report["traps_total"]:
        return False
    return all(
        report.get(k) is not None and report[k] >= v
        for k, v in THRESHOLDS.items()
    )


def summarize(report: dict[str, Any], label: str) -> None:
    verdict = "PASS" if passes(report) else "FAIL"
    print(
        f"[{label}] {verdict}  direction={report['direction_accuracy']:.1%}"
        f"  recall={report['catalyst_recall']:.1%}"
        f"  rank_corr={report['avg_rank_corr']}"
        f"  traps={report['traps_passed']}/{report['traps_total']}"
    )
    for res in report["results"]:
        if res["direction_hits"] < res["direction_total"] or not res["traps_ok"]:
            misses = [r for r in res["rows"] if not r["direction_hit"]]
            print(f"  miss {res['scenario']}: "
                  + "; ".join(f"{m['ticker']} pred={m['pred_direction']}"
                              f" true={m['true_direction']}" for m in misses))


def summarize_squeeze(report: dict[str, Any]) -> None:
    verdict = "PASS" if report["all_passed"] else "FAIL"
    print(
        f"[squeeze bank] {verdict}  scenarios={report['scenarios_passed']}"
        f"/{report['n_scenarios']}  checks={report['checks_passed']}"
        f"/{report['checks_total']}"
    )
    for res in report["results"]:
        for c in res["checks"]:
            if not c["ok"]:
                print(f"  miss {res['scenario']}: {c['name']} -> {c['detail']}")


def optimize_loop() -> tuple[dict[str, Any], Optional[dict[str, float]]]:
    """
    Run -> tune -> rerun until thresholds pass or MAX_ITERATIONS.

    Weight tuning uses the calibration module's shrunk recommendation computed
    from the scenario results themselves (in graded-run shape) — the same
    machinery live grading uses, so what passes here transfers to production.
    """
    weights: Optional[dict[str, float]] = None
    best_report = run_bank(weights=weights)
    summarize(best_report, "iter 0 / default weights")
    if passes(best_report):
        return best_report, weights

    for i in range(1, MAX_ITERATIONS + 1):
        graded = [as_graded_run(r) for r in best_report["results"]]
        rec = recommend_weights(graded)
        candidate = rec["weights"]
        report = run_bank(weights=candidate)
        summarize(report, f"iter {i} / shrunk weights {candidate}")
        improved = (
            (report["direction_accuracy"], report["avg_rank_corr"] or -1)
            > (best_report["direction_accuracy"], best_report["avg_rank_corr"] or -1)
        )
        if improved or passes(report):
            best_report, weights = report, candidate
        if passes(best_report):
            break
    return best_report, weights


async def replay_mongo(limit: int = 50) -> Optional[dict[str, Any]]:
    """
    Read-only replay of real graded runs: refit the expected-move curve and the
    weight recommendation on live history. Nothing is written back.
    """
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        print("replay: MONGODB_URI not set — skipping")
        return None
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        print("replay: motor not installed — skipping")
        return None
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
    try:
        db = client.get_default_database()
        cursor = db["catalyst_runs"].find(
            {"metrics.per_ticker": {"$exists": True, "$ne": []}},
            {"items": 1, "metrics": 1},
        ).sort("created_at", -1).limit(limit)
        runs = await cursor.to_list(length=limit)
    except Exception as exc:  # noqa: BLE001 — report and continue offline
        print(f"replay: could not read graded runs ({exc}) — skipping")
        return None
    finally:
        client.close()
    if not runs:
        print("replay: no graded runs found")
        return None
    curve = fit_expected_move(runs)
    rec = recommend_weights(runs)
    print(f"replay: {len(runs)} graded runs")
    print(f"  expected-move curve: {curve}")
    print(f"  weight recommendation: {rec['weights']} (shrinkage {rec['shrinkage']})")
    return {"n_runs": len(runs), "expected_move_curve": curve,
            "weight_recommendation": rec}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimize", action="store_true")
    parser.add_argument("--replay", action="store_true",
                        help="also refit calibration on real graded Mongo runs (read-only)")
    parser.add_argument("--llm", action="store_true",
                        help="grade scenario shortlists with the Claude deep read "
                             "(spends API credits on news analysis only)")
    parser.add_argument("--save", metavar="PATH", default=None)
    args = parser.parse_args()

    if args.llm and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        sys.exit("--llm requires ANTHROPIC_API_KEY")

    if args.optimize:
        report, weights = optimize_loop()
        if weights:
            print(f"\nrecommended production weights: {weights}")
            print("(apply via rank_catalysts(weights=...) after confirming on live grades)")
    else:
        report = run_bank()
        summarize(report, "single pass")
        weights = None

    # Squeeze bank: structural checks (veto/halt/decay/ranking), all must hold.
    squeeze_report = run_squeeze_bank()
    summarize_squeeze(squeeze_report)

    replay_result = asyncio.run(replay_mongo()) if args.replay else None

    if args.llm:
        # Imported lazily: only this path can spend credits, and only on news.
        from scenario_llm_grade import grade_bank_with_llm
        report["llm_grading"] = asyncio.run(
            grade_bank_with_llm(scenario_bank())
        )

    if args.save:
        out = {"report": report, "squeeze_report": squeeze_report,
               "weights": weights, "replay": replay_result,
               "thresholds": THRESHOLDS,
               "passed": passes(report) and squeeze_report["all_passed"]}
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"saved -> {args.save}")

    sys.exit(0 if passes(report) and squeeze_report["all_passed"] else 1)


if __name__ == "__main__":
    main()
