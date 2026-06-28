"""
signal_eval.py
==============
Signal validation — does each signal actually predict the move?

The honest research question for a dashboard drowning in signals: of all the
things we compute (squeeze score, fuel, ignition, social velocity, search
velocity, catalyst score, abnormal attention …), **which ones have predictive
value?** This answers it with no new data capture, because every *graded* run
already persists, per ticker, both the signal values (on the item) and the
realized forward move (on ``metrics.per_ticker``). We just pair them.

For each signal we report:
  - ``correlation`` — Spearman rank correlation between the signal value and the
    realized outcome across all graded tickers (the core predictive read).
  - ``top_minus_bottom`` — mean outcome of the top-third signal values minus the
    bottom-third (interpretable: "high-signal names moved X more").
  - ``verdict`` — predictive / weak / no edge / insufficient data.

Pure and deterministic (reuses the backtest's Spearman), so it's unit-tested
without a database. ``min_n`` guards against reading noise off tiny samples —
with the schedulers freshly live, most signals will honestly read "insufficient
data" until grades accumulate.
"""

from __future__ import annotations

from typing import Any, Optional

from catalyst_backtest import _spearman

# Signal fields to validate, per run type. Outcome is realized forward move.
SQUEEZE_SIGNALS = [
    "squeeze_score", "fuel_score", "ignition_score", "social_velocity",
    "search_velocity", "short_pct_float", "short_ratio", "focus_score",
]
SQUEEZE_OUTCOME = "max_gain"          # 5-session peak gain vs entry

CATALYST_SIGNALS = [
    "catalyst_score", "pre_score", "abnormal_attention", "n_sources",
    "confirmation_factor",
]
CATALYST_OUTCOME = "abs_move"         # |open->close| of the graded session

_MIN_N = 8


def _extract(item: dict[str, Any], key: str) -> Optional[float]:
    """Signal value from an item — top-level first, then ``components``."""
    v = item.get(key)
    if v is None:
        v = (item.get("components") or {}).get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pairs(runs: list[dict[str, Any]], signal_key: str, outcome_key: str) -> list[tuple[float, float]]:
    """(signal, outcome) across all graded runs, joining items to per_ticker by ticker."""
    pairs: list[tuple[float, float]] = []
    for run in runs:
        per = {g.get("ticker"): g
               for g in ((run.get("metrics") or {}).get("per_ticker") or [])}
        for it in run.get("items", []):
            sv = _extract(it, signal_key)
            g = per.get(it.get("ticker"))
            if sv is None or not g:
                continue
            ov = g.get(outcome_key)
            try:
                ov = float(ov) if ov is not None else None
            except (TypeError, ValueError):
                ov = None
            if ov is None:
                continue
            pairs.append((sv, ov))
    return pairs


def _verdict(correlation: Optional[float]) -> str:
    if correlation is None:
        return "no variance"
    a = abs(correlation)
    if a >= 0.30:
        return "predictive"
    if a >= 0.15:
        return "weak"
    return "no edge"


def evaluate_signal(
    runs: list[dict[str, Any]], signal_key: str, outcome_key: str, *, min_n: int = _MIN_N
) -> dict[str, Any]:
    """Predictive read for one signal. ``correlation`` sign matters — a strong
    *negative* correlation means the signal works backwards (still flagged
    'predictive' by magnitude; read the sign)."""
    pairs = _pairs(runs, signal_key, outcome_key)
    n = len(pairs)
    if n < min_n:
        return {"signal": signal_key, "n": n, "correlation": None,
                "top_minus_bottom": None, "verdict": "insufficient data"}

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    corr = _spearman(xs, ys)

    order = sorted(pairs, key=lambda p: p[0])
    k = max(1, n // 3)
    bottom_mean = sum(p[1] for p in order[:k]) / k
    top_mean = sum(p[1] for p in order[-k:]) / k

    return {
        "signal": signal_key,
        "n": n,
        "correlation": round(corr, 3) if corr is not None else None,
        "top_minus_bottom": round(top_mean - bottom_mean, 5),
        "verdict": _verdict(corr),
    }


def evaluate_signals(
    runs: list[dict[str, Any]], signal_keys: list[str], outcome_key: str, *, min_n: int = _MIN_N
) -> list[dict[str, Any]]:
    """Evaluate each signal, sorted strongest-correlation first."""
    results = [evaluate_signal(runs, k, outcome_key, min_n=min_n) for k in signal_keys]
    results.sort(key=lambda r: abs(r["correlation"]) if r["correlation"] is not None else -1,
                 reverse=True)
    return results
