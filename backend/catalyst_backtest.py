"""
catalyst_backtest.py
====================
Offline backtest harness for the catalyst **pre-score filter**.

Re-ranks already-*graded* runs under candidate scoring formulas and measures
whether the new ordering puts the bigger realized movers nearer the top — using
the open->close moves we already froze onto each run at grading time, so it
needs no network, no LLM, and no price re-fetch. Everything here is pure and
deterministic, hence unit-testable without a database.

How it works
------------
Each ranked item persists its bounded components (attention / abnormal /
sentiment / materiality) AND the three multiplicative factors (credibility /
size / confirmation). The live pre-score is just::

    100 * (w·components) * credibility * size * confirmation

so any candidate weight vector — or a factor toggled on/off — can be replayed
straight from persisted data. We then compare that replayed ordering against the
realized |move| per ticker (from ``metrics.per_ticker``).

What it can and cannot do
-------------------------
- CAN re-rank the persisted shortlist and re-score the ordering against realized
  moves; CAN ablate a factor (the confirmation/pre-market boost on vs off).
- CANNOT recover candidates the original cheap filter excluded — only the top_k
  shortlist is persisted with features. So this tunes the *ordering* of the
  surfaced names, not the *selection* boundary. With few graded runs a weight
  grid WILL overfit; treat it as directional. The single clean hypothesis test
  is the confirmation on/off ablation.
"""

from __future__ import annotations

from typing import Any, Optional

# The four bounded [0,1] components and the default weights (kept in sync with
# catalyst_ranker.score_candidates; duplicated here so the harness has no import
# side effects — market_calendar/tzdata — and stays a pure module).
_COMPONENT_KEYS = ("attention", "abnormal", "sentiment", "materiality")
_FACTOR_KEYS = ("credibility_factor", "size_factor", "confirmation_factor")
DEFAULT_WEIGHTS: dict[str, float] = {
    "attention": 0.30, "abnormal": 0.25, "sentiment": 0.25, "materiality": 0.20,
}


# --- pre-score replay ------------------------------------------------------ #

def recompute_pre_score(
    components: dict[str, Any],
    weights: Optional[dict[str, float]] = None,
    *,
    credibility: bool = True,
    size: bool = True,
    confirmation: bool = True,
) -> float:
    """Replay the composite pre-score from persisted components under a
    candidate weight vector, with any multiplicative factor toggleable off."""
    w = weights or DEFAULT_WEIGHTS
    base = sum(w.get(k, 0.0) * float(components.get(k, 0.0)) for k in _COMPONENT_KEYS)
    factor = 1.0
    if credibility:
        factor *= float(components.get("credibility_factor", 1.0))
    if size:
        factor *= float(components.get("size_factor", 1.0))
    if confirmation:
        factor *= float(components.get("confirmation_factor", 1.0))
    return round(100.0 * base * factor, 4)


# --- rank-correlation helpers (pure) --------------------------------------- #

def _avg_ranks(xs: list[float]) -> list[float]:
    """1-based ranks with averaged ranks for ties."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # mean of the 0-based tie positions, +1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:  # no variance -> correlation undefined
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / (sx ** 0.5 * sy ** 0.5)


def _spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    """Spearman rank correlation; None if <2 points or no variance."""
    return _pearson(_avg_ranks(xs), _avg_ranks(ys))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


# --- single-run evaluation ------------------------------------------------- #

def evaluate_run(
    run: dict[str, Any],
    weights: Optional[dict[str, float]] = None,
    *,
    credibility: bool = True,
    size: bool = True,
    confirmation: bool = True,
) -> Optional[dict[str, Any]]:
    """
    Re-rank one graded run's items by a candidate pre-score and score the
    ordering against realized moves. Returns None when the run has fewer than 2
    gradeable items (nothing to separate / correlate).

    Metrics:
      reaction_separation — avg |move| of the candidate top half minus bottom
                            half (>0 = candidate put the bigger movers on top).
      rank_corr           — Spearman(pre_score, realized |move|): a smoother,
                            tie-aware version of the same question.
      direction_hit_rate  — invariant to weights (carried for reference only).
    """
    metrics = run.get("metrics") or {}
    per = {
        g["ticker"]: g
        for g in metrics.get("per_ticker", [])
        if g.get("ticker") and g.get("abs_move") is not None
    }
    rows: list[dict[str, Any]] = []
    for it in run.get("items", []):
        comp = it.get("components")
        g = per.get(it.get("ticker"))
        if not comp or not g:
            continue
        rows.append({
            "ticker": it["ticker"],
            "pre_score": recompute_pre_score(
                comp, weights, credibility=credibility, size=size, confirmation=confirmation
            ),
            "abs_move": float(g["abs_move"]),
            "direction_hit": g.get("direction_hit"),
        })
    if len(rows) < 2:
        return None

    rows.sort(key=lambda r: r["pre_score"], reverse=True)
    mid = max(1, len(rows) // 2)
    top, bottom = rows[:mid], rows[mid:]
    top_move = _mean([r["abs_move"] for r in top])
    bottom_move = _mean([r["abs_move"] for r in bottom]) if bottom else 0.0

    spear = _spearman([r["pre_score"] for r in rows], [r["abs_move"] for r in rows])
    directional = [r for r in rows if r["direction_hit"] is not None]
    hit_rate = (
        _mean([1.0 if r["direction_hit"] else 0.0 for r in directional])
        if directional else None
    )
    return {
        "graded": len(rows),
        "reaction_separation": round(top_move - bottom_move, 5),
        "rank_corr": round(spear, 4) if spear is not None else None,
        "direction_hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
    }


# --- aggregate across runs ------------------------------------------------- #

def backtest(
    runs: list[dict[str, Any]],
    weights: Optional[dict[str, float]] = None,
    *,
    credibility: bool = True,
    size: bool = True,
    confirmation: bool = True,
) -> dict[str, Any]:
    """Average the per-run evaluation across all gradeable runs."""
    evals = [
        e for e in (
            evaluate_run(r, weights, credibility=credibility, size=size, confirmation=confirmation)
            for r in runs
        ) if e is not None
    ]
    if not evals:
        return {"n_runs": 0, "avg_reaction_separation": None,
                "positive_separation_rate": None, "avg_rank_corr": None}
    seps = [e["reaction_separation"] for e in evals]
    corrs = [e["rank_corr"] for e in evals if e["rank_corr"] is not None]
    return {
        "n_runs": len(evals),
        "avg_reaction_separation": round(_mean(seps), 5),
        "positive_separation_rate": round(_mean([1.0 if s > 0 else 0.0 for s in seps]), 4),
        "avg_rank_corr": round(_mean(corrs), 4) if corrs else None,
    }


def confirmation_ablation(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    The one clean hypothesis test: does the pre-market confirmation boost improve
    the ranking? Re-scores every graded run with the confirmation factor ON vs
    OFF (all else equal) and reports the delta. ``delta_* > 0`` means the boost
    helped. Only meaningful once some graded runs actually carried a non-trivial
    confirmation factor (i.e. ran with FINVIZ_AUTH_TOKEN during pre-market).
    """
    on = backtest(runs, confirmation=True)
    off = backtest(runs, confirmation=False)

    def _delta(key: str) -> Optional[float]:
        a, b = on.get(key), off.get(key)
        return round(a - b, 5) if a is not None and b is not None else None

    return {
        "with_confirmation": on,
        "without_confirmation": off,
        "delta_reaction_separation": _delta("avg_reaction_separation"),
        "delta_rank_corr": _delta("avg_rank_corr"),
    }


# --- weight sweep (exploratory) -------------------------------------------- #

def _simplex(step: float = 0.1) -> list[dict[str, float]]:
    """All 4-component weight vectors on a ``step`` grid that sum to 1.0."""
    n = round(1.0 / step)
    out: list[dict[str, float]] = []
    for a in range(n + 1):
        for b in range(n + 1 - a):
            for c in range(n + 1 - a - b):
                d = n - a - b - c
                out.append({
                    "attention": round(a * step, 4),
                    "abnormal": round(b * step, 4),
                    "sentiment": round(c * step, 4),
                    "materiality": round(d * step, 4),
                })
    return out


def sweep(
    runs: list[dict[str, Any]],
    *,
    step: float = 0.1,
    top: int = 5,
) -> dict[str, Any]:
    """
    Exploratory grid search over component weights, ranked by avg rank
    correlation (tiebreak: avg reaction separation). Includes the current
    default weights as ``baseline`` for reference.

    WARNING: with few graded runs this overfits. Read it as "which directions
    look promising", not "deploy this vector". Confirm any change with more data
    and the ``confirmation_ablation`` discipline.
    """
    baseline = backtest(runs, DEFAULT_WEIGHTS)
    if baseline["n_runs"] == 0:
        return {"n_runs": 0, "baseline": baseline, "candidates": []}

    scored: list[dict[str, Any]] = []
    for w in _simplex(step):
        res = backtest(runs, w)
        if res["avg_rank_corr"] is None:
            continue
        scored.append({
            "weights": w,
            "avg_rank_corr": res["avg_rank_corr"],
            "avg_reaction_separation": res["avg_reaction_separation"],
            "positive_separation_rate": res["positive_separation_rate"],
        })
    scored.sort(
        key=lambda s: (s["avg_rank_corr"], s["avg_reaction_separation"] or 0.0),
        reverse=True,
    )
    return {"n_runs": baseline["n_runs"], "baseline": baseline, "candidates": scored[:top]}
