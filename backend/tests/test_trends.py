"""Unit tests for the Trends pure signal math (no network)."""

import trends


class TestVelocityOf:
    def test_insufficient_data_is_none(self):
        assert trends.velocity_of([1, 2, 3]) is None   # < 2*recent_points

    def test_ratio(self):
        vals = [10] * 24 + [20] * 24                   # baseline 10, recent 20
        assert trends.velocity_of(vals) == 2.0

    def test_zero_baseline_floored(self):
        vals = [0] * 24 + [5] * 24                      # baseline 0 -> floor 0.5
        assert trends.velocity_of(vals) == 10.0         # 5 / 0.5


class TestClockOf:
    def test_small_float_is_fast(self):
        assert trends.clock_of(9.0, 10e6) == "fast"

    def test_low_days_to_cover_is_fast(self):
        assert trends.clock_of(2.0, 500e6) == "fast"

    def test_high_dtc_large_float_is_slow(self):
        assert trends.clock_of(9.0, 500e6) == "slow"

    def test_unknown_fuel_defaults_slow(self):
        assert trends.clock_of(None, None) == "slow"


class TestSearchTerm:
    def test_none_or_no_acceleration_is_zero(self):
        assert trends.search_term(None, "fast") == 0.0
        assert trends.search_term(1.0, "fast") == 0.0   # 1x baseline = not accelerating

    def test_fast_is_more_sensitive_than_slow(self):
        assert trends.search_term(1.8, "fast") == 1.0   # fast saturates at 1.8x
        assert trends.search_term(1.8, "slow") < 1.0    # slow needs a bigger build

    def test_saturates_at_one(self):
        assert trends.search_term(5.0, "slow") == 1.0
