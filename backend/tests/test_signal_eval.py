"""Unit tests for signal validation (pure; no DB)."""

import signal_eval as se


def _run(rows):
    """rows: list of (ticker, signal_value, outcome). Builds a graded run where
    item['sig'] = signal_value and metrics.per_ticker['out'] = outcome."""
    items = [{"ticker": t, "sig": s} for t, s, _ in rows]
    per = [{"ticker": t, "out": o} for t, _, o in rows]
    return {"items": items, "metrics": {"per_ticker": per}}


def test_perfectly_predictive_signal():
    # signal increases with outcome -> correlation ~ +1 -> predictive
    rows = [(f"T{i}", i, i * 2.0) for i in range(10)]
    r = se.evaluate_signal([_run(rows)], "sig", "out")
    assert r["n"] == 10
    assert r["correlation"] > 0.9
    assert r["verdict"] == "predictive"
    assert r["top_minus_bottom"] > 0


def test_backwards_signal_has_negative_correlation():
    rows = [(f"T{i}", i, -i * 1.0) for i in range(10)]   # higher signal -> lower outcome
    r = se.evaluate_signal([_run(rows)], "sig", "out")
    assert r["correlation"] < -0.9
    assert r["verdict"] == "predictive"            # strong, but read the sign
    assert r["top_minus_bottom"] < 0


def test_no_edge_when_uncorrelated():
    rows = [("A", 1, 5.0), ("B", 2, 5.0), ("C", 3, 5.0), ("D", 4, 5.0),
            ("E", 5, 5.0), ("F", 6, 5.0), ("G", 7, 5.0), ("H", 8, 5.0)]
    r = se.evaluate_signal([_run(rows)], "sig", "out")
    # zero variance in outcome -> spearman None -> "no variance"
    assert r["correlation"] is None
    assert r["verdict"] == "no variance"


def test_insufficient_data():
    rows = [("A", 1, 2.0), ("B", 2, 3.0)]           # n=2 < min_n
    r = se.evaluate_signal([_run(rows)], "sig", "out")
    assert r["verdict"] == "insufficient data"
    assert r["correlation"] is None


def test_extract_reads_components():
    item = {"ticker": "X", "components": {"nested": 0.5}}
    assert se._extract(item, "nested") == 0.5
    assert se._extract(item, "missing") is None


def test_pairs_join_by_ticker_and_skip_ungraded():
    run = {"items": [{"ticker": "A", "sig": 1}, {"ticker": "B", "sig": 2}],
           "metrics": {"per_ticker": [{"ticker": "A", "out": 9.0}]}}  # B ungraded
    pairs = se._pairs([run], "sig", "out")
    assert pairs == [(1.0, 9.0)]


def test_evaluate_signals_sorted_by_strength():
    rows_strong = [(f"T{i}", i, i * 1.0) for i in range(10)]
    runs = [_run(rows_strong)]
    # 'sig' is perfectly predictive; 'missing' has no data
    out = se.evaluate_signals(runs, ["missing", "sig"], "out")
    assert out[0]["signal"] == "sig"               # strongest first
