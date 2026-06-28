"""Unit tests for the options-flow aggregation (pure; no yfinance)."""

import options_flow as of


def test_put_call_ratio_and_lean_bullish():
    calls = [{"strike": 10, "volume": 800, "openInterest": 1000, "impliedVolatility": 0.6}]
    puts = [{"strike": 10, "volume": 200, "openInterest": 500, "impliedVolatility": 0.7}]
    sig = of.compute_options_signal(calls, puts, spot=10.0)
    assert sig["call_volume"] == 800 and sig["put_volume"] == 200
    assert sig["put_call_ratio"] == 0.25       # 200/800
    assert sig["put_call_oi_ratio"] == 0.5     # 500/1000
    assert sig["lean"] == "bullish"            # < 0.7
    assert sig["atm_iv"] == 0.65               # avg of 0.6 and 0.7 at the ATM strike


def test_lean_bearish_when_puts_dominate():
    calls = [{"strike": 5, "volume": 100, "openInterest": 100, "impliedVolatility": 0.5}]
    puts = [{"strike": 5, "volume": 300, "openInterest": 100, "impliedVolatility": 0.5}]
    sig = of.compute_options_signal(calls, puts, spot=5.0)
    assert sig["put_call_ratio"] == 3.0
    assert sig["lean"] == "bearish"


def test_zero_call_volume_ratio_is_none():
    sig = of.compute_options_signal(
        [{"strike": 5, "volume": 0, "openInterest": 0, "impliedVolatility": None}],
        [{"strike": 5, "volume": 10, "openInterest": 0, "impliedVolatility": None}],
        spot=5.0,
    )
    assert sig["put_call_ratio"] is None
    assert sig["lean"] == "neutral"


def test_nan_values_are_safe():
    # yfinance returns NaN for illiquid strikes — must not raise (was a live bug)
    nan = float("nan")
    sig = of.compute_options_signal(
        [{"strike": 5, "volume": nan, "openInterest": nan, "impliedVolatility": nan},
         {"strike": 5, "volume": 100, "openInterest": 50, "impliedVolatility": 0.8}],
        [{"strike": 5, "volume": nan, "openInterest": nan, "impliedVolatility": nan}],
        spot=5.0,
    )
    assert sig["call_volume"] == 100      # NaN coerced to 0
    assert sig["put_volume"] == 0
    assert sig["put_call_ratio"] == 0.0
    assert sig["atm_iv"] == 0.8           # only the real IV counts


def test_atm_iv_picks_nearest_strike():
    calls = [
        {"strike": 8, "volume": 1, "openInterest": 1, "impliedVolatility": 0.9},
        {"strike": 10, "volume": 1, "openInterest": 1, "impliedVolatility": 0.5},  # nearest to spot 10
    ]
    puts = [{"strike": 10, "volume": 1, "openInterest": 1, "impliedVolatility": 0.5}]
    sig = of.compute_options_signal(calls, puts, spot=10.0)
    assert sig["atm_iv"] == 0.5
