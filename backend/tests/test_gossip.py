"""Unit tests for gossip rolling-window mention velocity (pure; no Mongo)."""

from datetime import datetime, timedelta, timezone

import gossip

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


def h(n: float):  # n hours ago
    return NOW - timedelta(hours=n)


def d(n: float):  # n days ago
    return NOW - timedelta(days=n)


def test_min_recent_filters_noise():
    # 2 recent mentions, below the default min_recent=3 -> excluded
    out = gossip.score_gossip({"Q": [(h(1), None), (h(2), None)]}, now=NOW, min_recent=3)
    assert out == []


def test_emerging_from_zero_is_gossip():
    out = gossip.score_gossip(
        {"NEW": [(h(1), 0.4), (h(2), 0.4), (h(3), 0.4), (h(4), 0.4), (h(5), 0.4)]},
        now=NOW, recent_hours=6, baseline_days=7, min_recent=3,
    )
    assert len(out) == 1
    r = out[0]
    assert r["recent_count"] == 5
    assert r["velocity"] == 10.0          # 5 recent / floored baseline (0.5)
    assert r["direction"] == "bullish"
    assert r["rank"] == 1


def test_acceleration_outranks_steady():
    mentions = {
        # spiking: 5 recent, no baseline
        "NEW": [(h(0.5 + i), 0.2) for i in range(5)],
        # steady: heavy baseline, only 3 recent
        "STEADY": [(d(1 + i * 0.05), 0.0) for i in range(60)] + [(h(1), 0.0), (h(2), 0.0), (h(3), 0.0)],
    }
    out = gossip.score_gossip(mentions, now=NOW, recent_hours=6, baseline_days=7, min_recent=3)
    assert out[0]["ticker"] == "NEW"
    new = next(r for r in out if r["ticker"] == "NEW")
    steady = next(r for r in out if r["ticker"] == "STEADY")
    assert new["velocity"] > steady["velocity"]


def test_direction_from_recent_sentiment():
    bear = gossip.score_gossip(
        {"DN": [(h(1), -0.5), (h(2), -0.4), (h(3), -0.3)]}, now=NOW, min_recent=3
    )[0]
    assert bear["direction"] == "bearish"


def test_old_mentions_excluded_from_recent():
    # mentions older than recent_hours don't count toward recent_count
    out = gossip.score_gossip(
        {"X": [(h(1), 0.1), (h(2), 0.1), (h(3), 0.1), (h(20), 0.1), (h(30), 0.1)]},
        now=NOW, recent_hours=6, min_recent=3,
    )
    assert out[0]["recent_count"] == 3   # the 20h/30h-old ones are baseline, not recent
