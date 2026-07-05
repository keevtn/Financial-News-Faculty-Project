"""
catalyst_calibration.py
=======================
Close the expected-vs-true gap: turn graded catalyst runs into (a) a
calibrated **expected-move curve** and (b) a **shrunk weight recommendation**.

The ranker emits a unitless score (pre_score / catalyst_score, 0..100) that
says "this catalyst is strong" but never says what that implies in realized
move — so "expected vs true performance" was unmeasurable by construction.
This module fits the mapping from persisted graded runs:

  1. ``fit_expected_move``   — bins graded (score, |move|) pairs into score
     quantiles and pools bin means into a monotone curve (pool-adjacent-
     violators), so a higher score never predicts a *smaller* expected move.
     ``predict_expected_move`` then interpolates a per-item expectation the
     dashboard can show next to the realized move.
  2. ``recommend_weights``   — the backtest's weight sweep overfits badly on
     few runs (its own docstring says so). This shrinks the sweep's best
     vector toward the production defaults with an n-dependent factor
     λ = n / (n + prior_strength), so with ~11 runs the recommendation moves
     only modestly off the defaults, and converges to the sweep as grades
     accumulate.
  3. ``calibration_report``  — one dict for a dashboard/API panel: curve,
     weight recommendation, and per-signal predictive verdicts.

Everything is pure and deterministic (no Mongo, no network) — callers fetch
graded runs exactly as ``signal_eval`` consumers already do and pass them in.
Small-sample honesty: below ``min_pairs`` the fits return None rather than a
curve read off noise.
"""

from __future__ import annotations

from typing import Any, Optional

from catalyst_backtest import DEFAULT_WEIGHTS, backtest, sweep
from signal_eval import CATALYST_SIGNALS, _extract, evaluate_signals

# Fewest (score, move) pairs before fitting a curve; below this the honest
# answer is "insufficient data", not a 2-bin curve.
MIN_PAIRS = 12
DEFAULT_BINS = 4
# Pseudo-count of pairs at which the sweep gets equal say with the defaults.
PRIOR_STRENGTH = 40


# --- pair extraction (same join as signal_eval) ----------------------------- #

def score_move_pairs(
    runs: list[dict[str, Any]],
    score_key: str = "catalyst_score",
    outcome_key: str = "abs_move",
) -> list[tuple[float, float]]:
    """(score, realized outcome) across graded runs, joined per ticker."""
    pairs: list[tuple[float, float]] = []
    for run in runs:
        per = {g.get("ticker"): g
               for g in ((run.get("metrics") or {}).get("per_ticker") or [])}
        for it in run.get("items", []):
            sv = _extract(it, score_key)
            g = per.get(it.get("ticker"))
            if sv is None or not g:
                continue
            try:
                ov = float(g[outcome_key]) if g.get(outcome_key) is not None else None
            except (TypeError, ValueError):
                ov = None
            if ov is not None:
                pairs.append((sv, ov))
    return pairs


# --- expected-move curve ----------------------------------------------------- #

def _pav(means: list[float], weights: list[float]) -> list[float]:
    """Pool adjacent violators: smallest change making ``means`` non-decreasing."""
    vals = list(means)
    wts = list(weights)
    blocks = [[i] for i in range(len(vals))]
    i = 0
    while i < len(vals) - 1:
        if vals[i] > vals[i + 1]:
            w = wts[i] + wts[i + 1]
            v = (vals[i] * wts[i] + vals[i + 1] * wts[i + 1]) / w
            vals[i:i + 2] = [v]
            wts[i:i + 2] = [w]
            blocks[i:i + 2] = [blocks[i] + blocks[i + 1]]
            i = max(i - 1, 0)  # merged block may now violate to its left
        else:
            i += 1
    out = [0.0] * sum(len(b) for b in blocks)
    for v, block in zip(vals, blocks):
        for idx in block:
            out[idx] = v
    return out


def fit_expected_move(
    runs: list[dict[str, Any]],
    *,
    score_key: str = "catalyst_score",
    outcome_key: str = "abs_move",
    n_bins: int = DEFAULT_BINS,
    min_pairs: int = MIN_PAIRS,
) -> Optional[list[dict[str, Any]]]:
    """
    Monotone expected-|move| curve over score quantiles, or None when there is
    not enough graded data. Each bin: {score_lo, score_hi, score_mid,
    expected_move, n}. ``expected_move`` is in the same units the grader froze
    (e.g. percent open->close move).
    """
    pairs = sorted(score_move_pairs(runs, score_key, outcome_key))
    if len(pairs) < min_pairs:
        return None
    n_bins = max(2, min(n_bins, len(pairs) // 3))  # ≥3 pairs per bin

    size, rem = divmod(len(pairs), n_bins)
    bins: list[list[tuple[float, float]]] = []
    start = 0
    for b in range(n_bins):
        end = start + size + (1 if b < rem else 0)
        bins.append(pairs[start:end])
        start = end

    means = [sum(p[1] for p in b) / len(b) for b in bins]
    weights = [float(len(b)) for b in bins]
    monotone = _pav(means, weights)

    return [
        {
            "score_lo": round(b[0][0], 4),
            "score_hi": round(b[-1][0], 4),
            "score_mid": round((b[0][0] + b[-1][0]) / 2.0, 4),
            "expected_move": round(m, 5),
            "n": len(b),
        }
        for b, m in zip(bins, monotone)
    ]


def predict_expected_move(
    curve: Optional[list[dict[str, Any]]], score: float
) -> Optional[float]:
    """
    Expected |move| for a score: linear interpolation between bin midpoints,
    clamped to the end bins. None when there is no curve.
    """
    if not curve:
        return None
    if score <= curve[0]["score_mid"]:
        return curve[0]["expected_move"]
    if score >= curve[-1]["score_mid"]:
        return curve[-1]["expected_move"]
    for lo, hi in zip(curve, curve[1:]):
        if lo["score_mid"] <= score <= hi["score_mid"]:
            span = hi["score_mid"] - lo["score_mid"]
            if span <= 0:
                return hi["expected_move"]
            frac = (score - lo["score_mid"]) / span
            return round(
                lo["expected_move"]
                + frac * (hi["expected_move"] - lo["expected_move"]),
                5,
            )
    return curve[-1]["expected_move"]  # unreachable; defensive


def attach_expected_moves(
    items: list[dict[str, Any]],
    curve: Optional[list[dict[str, Any]]],
    *,
    score_key: str = "catalyst_score",
) -> None:
    """Annotate ranked items in place with ``expected_move`` (None if no curve)."""
    for it in items:
        sv = _extract(it, score_key)
        it["expected_move"] = (
            predict_expected_move(curve, sv) if sv is not None else None
        )


# --- shrunk weight recommendation -------------------------------------------- #

def recommend_weights(
    runs: list[dict[str, Any]],
    *,
    step: float = 0.1,
    prior_strength: int = PRIOR_STRENGTH,
) -> dict[str, Any]:
    """
    Sweep-best component weights shrunk toward the production defaults.

    λ = n_pairs / (n_pairs + prior_strength) — with 11 graded runs of a few
    items each, λ stays small and the recommendation is a nudge, not a lurch.
    Returns {weights, shrinkage, n_pairs, sweep_best, baseline_rank_corr,
    recommended_rank_corr}.
    """
    pairs = score_move_pairs(runs, "pre_score", "abs_move")
    n = len(pairs)
    result = sweep(runs, step=step, top=1)
    if not result["candidates"] or n == 0:
        return {
            "weights": dict(DEFAULT_WEIGHTS), "shrinkage": 0.0, "n_pairs": n,
            "sweep_best": None, "baseline_rank_corr": None,
            "recommended_rank_corr": None,
        }
    best = result["candidates"][0]["weights"]
    lam = n / (n + prior_strength)
    blended = {
        k: DEFAULT_WEIGHTS[k] + lam * (best[k] - DEFAULT_WEIGHTS[k])
        for k in DEFAULT_WEIGHTS
    }
    total = sum(blended.values()) or 1.0
    blended = {k: round(v / total, 4) for k, v in blended.items()}
    return {
        "weights": blended,
        "shrinkage": round(lam, 4),
        "n_pairs": n,
        "sweep_best": best,
        "baseline_rank_corr": result["baseline"].get("avg_rank_corr"),
        "recommended_rank_corr": backtest(runs, blended).get("avg_rank_corr"),
    }


# --- combined report ---------------------------------------------------------- #

def calibration_report(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything a calibration panel needs, from graded runs alone."""
    curve = fit_expected_move(runs)
    return {
        "n_runs": len(runs),
        "n_pairs": len(score_move_pairs(runs)),
        "expected_move_curve": curve,
        "weight_recommendation": recommend_weights(runs),
        "signal_verdicts": evaluate_signals(runs, CATALYST_SIGNALS, "abs_move"),
    }
