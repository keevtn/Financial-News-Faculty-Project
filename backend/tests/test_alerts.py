"""Unit tests for the alerts rules engine (pure; no I/O)."""

import alerts


def _sq(ticker, score, ignition=0.5, short=0.3):
    return {"ticker": ticker, "squeeze_score": score, "ignition_score": ignition,
            "short_pct_float": short}


def _go(ticker, velocity, score=70.0, recent=10, direction="bullish"):
    return {"ticker": ticker, "velocity": velocity, "gossip_score": score,
            "recent_count": recent, "direction": direction}


def _ca(ticker, score, rationale="big news"):
    return {"ticker": ticker, "catalyst_score": score, "rationale": rationale}


def test_squeeze_alone_is_high():
    out = alerts.evaluate_alerts(squeeze=[_sq("AAA", 60)])
    assert len(out) == 1
    assert out[0]["severity"] == "high"
    assert out[0]["signals"] == ["squeeze"]
    assert out[0]["tab"] == "squeeze"


def test_gossip_alone_is_medium():
    out = alerts.evaluate_alerts(gossip=[_go("BBB", 5.0)])
    assert out[0]["severity"] == "medium"
    assert out[0]["tab"] == "gossip"


def test_confluence_is_critical_and_ranks_first():
    out = alerts.evaluate_alerts(
        squeeze=[_sq("HOT", 70), _sq("MEH", 50)],
        gossip=[_go("HOT", 6.0)],          # HOT lights up on both
        catalyst=[_ca("NEWS", 80)],
    )
    top = out[0]
    assert top["ticker"] == "HOT"
    assert top["severity"] == "critical"
    assert set(top["signals"]) == {"squeeze", "gossip"}
    # critical sorts above the high/medium ones
    assert [a["severity"] for a in out][0] == "critical"


def test_below_threshold_does_not_fire():
    out = alerts.evaluate_alerts(
        squeeze=[_sq("LOW", 30)],          # < 45
        gossip=[_go("Q", 1.5, score=20)],  # < 3x and < 65
        catalyst=[_ca("C", 40)],           # < 65
    )
    assert out == []


def test_primed_but_not_firing_excluded():
    # high squeeze score but low ignition (primed, not firing) -> no alert
    out = alerts.evaluate_alerts(squeeze=[_sq("PRIMED", 60, ignition=0.1)])
    assert out == []


def test_severity_counts():
    out = alerts.evaluate_alerts(
        squeeze=[_sq("HOT", 70), _sq("S", 60)],
        gossip=[_go("HOT", 6.0), _go("G", 5.0)],
    )
    counts = alerts.severity_counts(out)
    assert counts["critical"] == 1   # HOT
    assert counts["high"] == 1       # S
    assert counts["medium"] == 1     # G
