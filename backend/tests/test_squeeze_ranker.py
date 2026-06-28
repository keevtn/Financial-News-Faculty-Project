"""Unit tests for the squeeze ranker's pure scoring (no network/Mongo)."""

import squeeze_ranker as sq


class TestSat:
    def test_zero_and_negative(self):
        assert sq._sat(0, 5) == 0.0
        assert sq._sat(-3, 5) == 0.0
        assert sq._sat(None, 5) == 0.0

    def test_ramp_and_saturation(self):
        assert sq._sat(2.5, 5) == 0.5
        assert sq._sat(5, 5) == 1.0
        assert sq._sat(10, 5) == 1.0


class TestFuelScore:
    def test_max_fuel(self):
        fuel, c = sq._fuel_score(0.30, 8.0, 20e6)   # all terms saturate
        assert fuel == 1.0
        assert c["short_float"] == 1.0 and c["days_to_cover"] == 1.0 and c["low_float"] == 1.0

    def test_half_fuel(self):
        fuel, _ = sq._fuel_score(0.15, 4.0, 100e6)  # each term at 0.5
        assert abs(fuel - 0.5) < 1e-9

    def test_no_data_is_zero(self):
        fuel, _ = sq._fuel_score(None, None, None)
        assert fuel == 0.0

    def test_low_float_boost(self):
        # smaller float -> larger low-float term
        big = sq._fuel_score(0.10, 1.0, 500e6)[1]["low_float"]
        small = sq._fuel_score(0.10, 1.0, 10e6)[1]["low_float"]
        assert small > big == 0.1   # 50e6/500e6


class TestVelocityTerm:
    def test_no_velocity_or_normal_is_zero(self):
        assert sq._velocity_term(None) == 0.0
        assert sq._velocity_term(1.0) == 0.0   # 1x baseline = not accelerating

    def test_ramp_and_saturation(self):
        assert sq._velocity_term(3.0) == 0.5   # halfway from 1x to 5x
        assert sq._velocity_term(5.0) == 1.0
        assert sq._velocity_term(10.0) == 1.0


class TestIgnitionScore:
    def test_max_ignition_snapshot(self):
        ign, _ = sq._ignition_score(12.0, 1.0, 100.0)   # no velocity -> 3 terms renormalized
        assert abs(ign - 1.0) < 1e-9

    def test_max_ignition_with_velocity(self):
        ign, c = sq._ignition_score(12.0, 1.0, 100.0, velocity=5.0)
        assert "velocity" in c and c["velocity"] == 1.0
        assert abs(ign - 1.0) < 1e-9

    def test_bearish_sentiment_zeroes_bull_term(self):
        ign, c = sq._ignition_score(12.0, -0.5, 100.0)
        assert c["bullish"] == 0.0                     # only bullish chatter ignites
        # snapshot weights renormalized: (0.40*1 + 0.25*0 + 0.15*1)/0.80
        assert abs(ign - 0.6875) < 1e-9

    def test_velocity_alone_contributes(self):
        # quiet snapshot but accelerating mentions -> nonzero ignition
        ign, _ = sq._ignition_score(0.0, 0.0, 0.0, velocity=5.0)
        assert abs(ign - 0.20) < 1e-9                  # only the velocity weight

    def test_search_term_lifts_ignition(self):
        base, _ = sq._ignition_score(6.0, 0.2, 20, velocity=1.0)
        withs, c = sq._ignition_score(6.0, 0.2, 20, velocity=1.0, search=0.8)
        assert "search" in c and c["search"] == 0.8
        assert withs > base                            # a strong search term raises ignition

    def test_quiet_is_zero(self):
        ign, _ = sq._ignition_score(0.0, 0.0, 0.0)
        assert ign == 0.0


class TestDivergence:
    def test_none_without_search(self):
        assert sq._divergence(3.0, None) is None

    def test_mainstream_when_both_rising(self):
        assert sq._divergence(3.0, 3.0) == "mainstream"

    def test_early_when_only_social(self):
        assert sq._divergence(3.0, 1.0) == "early"

    def test_search_led_when_only_search(self):
        assert sq._divergence(1.0, 3.0) == "search-led"

    def test_aligned_when_neither(self):
        assert sq._divergence(1.0, 1.0) == "aligned"


class TestSqueezeScore:
    def test_floor_when_no_ignition(self):
        # primed but not firing -> 25% of fuel*100
        assert sq._squeeze_score(1.0, 0.0) == 25.0

    def test_full_when_max_ignition(self):
        assert sq._squeeze_score(1.0, 1.0) == 100.0

    def test_monotonic_in_ignition(self):
        assert sq._squeeze_score(0.8, 0.2) < sq._squeeze_score(0.8, 0.9)


class TestDirection:
    def test_bands(self):
        assert sq._direction(0.05) == "bullish"
        assert sq._direction(-0.05) == "bearish"
        assert sq._direction(0.0) == "neutral"


class TestScoreCandidate:
    def test_primed_but_quiet_keeps_floor(self):
        c = sq.score_candidate("ABC", {"short_pct_float": 0.40, "short_ratio": 6.0,
                                        "float_shares": 30e6}, None, 0.0)
        assert c.ignition_score == 0.0
        assert c.squeeze_score > 0            # floored by fuel, not zeroed
        assert c.direction == "neutral"
        assert c.fuel_score > 0.8             # heavily loaded

    def test_firing_outranks_primed(self):
        short = {"short_pct_float": 0.40, "short_ratio": 6.0, "float_shares": 30e6}
        primed = sq.score_candidate("ABC", short, None, 0.0)
        firing = sq.score_candidate("ABC", short,
                                    {"focus_score": 12.0, "engagement": 100, "n_posts": 30,
                                     "sources": ["bluesky"], "top_posts": []}, 0.5)
        assert firing.squeeze_score > primed.squeeze_score
        assert firing.direction == "bullish"
        assert firing.n_posts == 30

    def test_velocity_records_and_lifts_ignition(self):
        short = {"short_pct_float": 0.30, "short_ratio": 5.0, "float_shares": 40e6}
        social = {"focus_score": 6.0, "engagement": 20, "n_posts": 10,
                  "sources": ["bluesky"], "top_posts": []}
        flat = sq.score_candidate("X", short, social, 0.2, velocity=1.0)  # not accelerating
        hot = sq.score_candidate("X", short, social, 0.2, velocity=5.0)   # accelerating
        assert hot.social_velocity == 5.0
        assert hot.ignition_score > flat.ignition_score


class TestGradeSqueeze:
    _RESULT = {"items": [{"ticker": "A", "rank": 1}, {"ticker": "B", "rank": 2}]}

    def test_hit_rate_and_separation(self):
        windows = {
            "A": {"entry": 10.0, "max_high": 13.0, "last_close": 12.0},  # +30% peak -> squeezed
            "B": {"entry": 10.0, "max_high": 10.5, "last_close": 9.0},   # +5% peak  -> no
        }
        m = sq.grade_squeeze(self._RESULT, windows, hit_threshold=0.15)
        assert m["graded"] == 2
        assert m["squeeze_hit_rate"] == 0.5
        assert m["reaction_separation"] == 0.25            # top(0.30) - bottom(0.05)
        assert abs(m["mean_close_return"] - 0.05) < 1e-9    # (+0.20, -0.10)/2
        assert m["per_ticker"][0]["squeezed"] is True

    def test_no_price_data(self):
        assert sq.grade_squeeze(self._RESULT, {})["graded"] == 0

    def test_threshold_respected(self):
        windows = {"A": {"entry": 10.0, "max_high": 11.0, "last_close": 11.0},  # +10%
                   "B": {"entry": 10.0, "max_high": 10.2, "last_close": 10.1}}
        # at 0.08 threshold A squeezes, B doesn't
        m = sq.grade_squeeze(self._RESULT, windows, hit_threshold=0.08)
        assert m["squeeze_hit_rate"] == 0.5
