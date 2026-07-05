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
    out = gossip.score_gossip({"Q": [(h(1), None, "a"), (h(2), None, "b")]}, now=NOW, min_recent=3)
    assert out == []


def test_emerging_from_zero_is_gossip():
    out = gossip.score_gossip(
        {"NEW": [(h(1), 0.4, "u1"), (h(2), 0.4, "u2"), (h(3), 0.4, "u3"),
                 (h(4), 0.4, "u4"), (h(5), 0.4, "u5")]},
        now=NOW, recent_hours=6, baseline_days=7, min_recent=3,
    )
    assert len(out) == 1
    r = out[0]
    assert r["recent_count"] == 5
    assert r["breadth"] == 5              # 5 distinct authors
    assert r["velocity"] == 10.0          # 5 recent / floored baseline (0.5)
    assert r["direction"] == "bullish"
    assert r["rank"] == 1


def test_acceleration_outranks_steady():
    mentions = {
        # spiking: 5 recent, no baseline, 5 distinct authors
        "NEW": [(h(0.5 + i), 0.2, f"u{i}") for i in range(5)],
        # steady: heavy baseline, only 3 recent
        "STEADY": [(d(1 + i * 0.05), 0.0, f"b{i}") for i in range(60)]
                  + [(h(1), 0.0, "s1"), (h(2), 0.0, "s2"), (h(3), 0.0, "s3")],
    }
    out = gossip.score_gossip(mentions, now=NOW, recent_hours=6, baseline_days=7, min_recent=3)
    assert out[0]["ticker"] == "NEW"
    new = next(r for r in out if r["ticker"] == "NEW")
    steady = next(r for r in out if r["ticker"] == "STEADY")
    assert new["velocity"] > steady["velocity"]


def test_breadth_outranks_concentration():
    # same recent_count + velocity; only the author spread differs.
    mentions = {
        "WIDE":   [(h(0.5 + i), 0.2, f"u{i}") for i in range(6)],   # 6 distinct authors
        "NARROW": [(h(0.5 + i), 0.2, "spammer") for i in range(6)],  # 1 author, 6 posts
    }
    out = gossip.score_gossip(mentions, now=NOW, recent_hours=6, min_recent=3)
    wide = next(r for r in out if r["ticker"] == "WIDE")
    narrow = next(r for r in out if r["ticker"] == "NARROW")
    assert wide["breadth"] == 6 and narrow["breadth"] == 1
    assert wide["recent_count"] == narrow["recent_count"]           # same volume
    assert wide["gossip_score"] > narrow["gossip_score"]            # propagation wins
    assert out[0]["ticker"] == "WIDE"


def test_direction_from_recent_sentiment():
    bear = gossip.score_gossip(
        {"DN": [(h(1), -0.5, "a"), (h(2), -0.4, "b"), (h(3), -0.3, "c")]}, now=NOW, min_recent=3
    )[0]
    assert bear["direction"] == "bearish"


def test_old_mentions_excluded_from_recent():
    # mentions older than recent_hours don't count toward recent_count
    out = gossip.score_gossip(
        {"X": [(h(1), 0.1, "a"), (h(2), 0.1, "b"), (h(3), 0.1, "c"),
               (h(20), 0.1, "d"), (h(30), 0.1, "e")]},
        now=NOW, recent_hours=6, min_recent=3,
    )
    assert out[0]["recent_count"] == 3   # the 20h/30h-old ones are baseline, not recent
