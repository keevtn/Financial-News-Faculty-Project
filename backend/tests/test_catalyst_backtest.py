"""Unit tests for the catalyst backtest harness (pure, no DB/network)."""

import pytest

import catalyst_backtest as bt


# --- pre-score replay ------------------------------------------------------ #

def test_recompute_matches_live_formula():
    # components from a real boosted candidate (live pre_score was 72.71)
    comp = {"attention": 0.6309, "abnormal": 0.6667, "sentiment": 0.5,
            "materiality": 0.625, "credibility_factor": 1.0, "size_factor": 1.0,
            "confirmation_factor": 1.2}
    assert abs(bt.recompute_pre_score(comp) - 72.71) < 0.05
    # toggling the confirmation factor off recovers the un-boosted base (~60.59)
    assert abs(bt.recompute_pre_score(comp, confirmation=False) - 60.59) < 0.05


def test_recompute_factor_toggles():
    comp = {"attention": 1.0, "abnormal": 0.0, "sentiment": 0.0, "materiality": 0.0,
            "credibility_factor": 1.1, "size_factor": 0.9, "confirmation_factor": 1.2}
    assert abs(bt.recompute_pre_score(comp) - 100 * 0.30 * 1.1 * 0.9 * 1.2) < 1e-4
    assert abs(bt.recompute_pre_score(comp, size=False) - 100 * 0.30 * 1.1 * 1.2) < 1e-4
    assert abs(bt.recompute_pre_score(comp, credibility=False, size=False,
                                      confirmation=False) - 100 * 0.30) < 1e-4


def test_recompute_custom_weights():
    comp = {"attention": 0.0, "abnormal": 0.0, "sentiment": 1.0, "materiality": 0.0,
            "credibility_factor": 1.0, "size_factor": 1.0, "confirmation_factor": 1.0}
    assert bt.recompute_pre_score(comp, {"attention": 0, "abnormal": 0,
                                         "sentiment": 0.5, "materiality": 0}) == 50.0


# --- rank-correlation helpers ---------------------------------------------- #

def test_avg_ranks_handles_ties():
    assert bt._avg_ranks([10, 10, 20]) == [1.5, 1.5, 3.0]


def test_spearman_monotonic():
    assert bt._spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert bt._spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_none_without_variance():
    assert bt._spearman([1, 1, 1], [1, 2, 3]) is None


def test_pearson_none_when_too_short():
    assert bt._pearson([1.0], [1.0]) is None


# --- run helpers ----------------------------------------------------------- #

def _run(*rows):
    """rows: (ticker, component_value, confirmation_factor, realized_abs_move)."""
    items, per = [], []
    for t, v, conf, move in rows:
        items.append({"ticker": t, "components": {
            "attention": v, "abnormal": v, "sentiment": v, "materiality": v,
            "credibility_factor": 1.0, "size_factor": 1.0, "confirmation_factor": conf}})
        per.append({"ticker": t, "abs_move": move, "direction_hit": True})
    return {"run_id": "r", "items": items, "metrics": {"graded": len(items), "per_ticker": per}}


# A run where the biggest mover (AAA) only ranks on top BECAUSE of its
# pre-market confirmation boost — the canonical ablation case.
_BOOST_DECIDES = _run(("AAA", 0.40, 1.30, 0.10),
                      ("BBB", 0.45, 1.00, 0.02),
                      ("CCC", 0.20, 1.00, 0.005))


def test_evaluate_run_orders_by_candidate_score():
    on = bt.evaluate_run(_BOOST_DECIDES, confirmation=True)
    off = bt.evaluate_run(_BOOST_DECIDES, confirmation=False)
    assert on["graded"] == 3
    assert on["rank_corr"] == 1.0
    assert on["reaction_separation"] > 0
    assert off["reaction_separation"] < on["reaction_separation"]


def test_evaluate_run_needs_two_gradeable_items():
    assert bt.evaluate_run(_run(("AAA", 0.4, 1.0, 0.1))) is None


def test_evaluate_run_skips_ungraded_tickers():
    run = _run(("AAA", 0.4, 1.0, 0.1), ("BBB", 0.3, 1.0, 0.02))
    # drop BBB's realized move -> only one gradeable item left -> None
    run["metrics"]["per_ticker"] = [run["metrics"]["per_ticker"][0]]
    assert bt.evaluate_run(run) is None


def test_confirmation_ablation_positive_when_boost_helps():
    abl = bt.confirmation_ablation([_BOOST_DECIDES])
    assert abl["delta_reaction_separation"] > 0
    assert abl["delta_rank_corr"] > 0


def test_backtest_empty_input():
    res = bt.backtest([])
    assert res["n_runs"] == 0
    assert res["avg_rank_corr"] is None


def test_backtest_aggregates():
    res = bt.backtest([_BOOST_DECIDES])
    assert res["n_runs"] == 1
    assert res["avg_rank_corr"] == 1.0
    assert res["positive_separation_rate"] == 1.0


# --- weight sweep ---------------------------------------------------------- #

def test_simplex_vectors_sum_to_one():
    grid = bt._simplex(0.25)
    assert len(grid) == 35  # 4-part compositions of 4 -> C(7,3)
    assert all(abs(sum(w.values()) - 1.0) < 1e-9 for w in grid)


def test_sweep_returns_top_candidates_and_baseline():
    sw = bt.sweep([_BOOST_DECIDES], step=0.25, top=3)
    assert sw["n_runs"] == 1
    assert sw["baseline"]["n_runs"] == 1
    assert len(sw["candidates"]) == 3
    # sorted by rank corr descending
    corrs = [c["avg_rank_corr"] for c in sw["candidates"]]
    assert corrs == sorted(corrs, reverse=True)


def test_sweep_empty_input():
    assert bt.sweep([])["n_runs"] == 0
