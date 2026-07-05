"""Unit tests for the ticker extractor's name passes (pure; no network)."""

from ticker_extractor import (
    TickerExtractor,
    CRYPTO_TICKERS,
    extract_social_tickers,
)


class TestSubsidiaryReadThrough:
    def test_subsidiary_resolves_to_listed_parent(self):
        ex = TickerExtractor()
        assert "GOOGL" in ex.extract("YouTube pulls ads from kids channels", "")
        assert "META" in ex.extract("Instagram launches new creator fund", "")
        assert "MSFT" in ex.extract("GitHub outage hits CI pipelines", "")
        assert "AMZN" in ex.extract("AWS announces price cuts", "")

    def test_facebook_alias_maps_to_meta(self):
        ex = TickerExtractor()
        assert "META" in ex.extract("Facebook fined over privacy practices", "")

    def test_word_boundary_no_partial_match(self):
        ex = TickerExtractor()
        # "waze" must not fire inside another word.
        assert ex.extract("The airwazes were crowded", "") == ()

    def test_subsidiaries_can_be_disabled(self):
        ex = TickerExtractor(include_subsidiaries=False)
        assert ex.extract("YouTube pulls ads from kids channels", "") == ()
        # The core company map is unaffected by the toggle.
        assert "AAPL" in ex.extract("Apple ships new phones", "")

    def test_extra_mappings_override_subsidiaries(self):
        ex = TickerExtractor(extra_mappings={"youtube": "TEST"})
        assert "TEST" in ex.extract("YouTube pulls ads", "")


# A tiny stand-in universe so tests stay pure (no SEC fetch).
_UNIVERSE = {"AAPL", "TSLA", "NVDA", "GME"}


class TestRealTickerValidation:
    def test_no_universe_means_no_gating(self):
        # Backward compatible: without a universe, any cashtag is kept.
        ex = TickerExtractor()
        assert "YOLO" in ex.extract("buying $YOLO calls to the moon", "")

    def test_fake_cashtag_dropped_when_universe_set(self):
        ex = TickerExtractor(valid_tickers=_UNIVERSE)
        out = ex.extract("$YOLO $MOON $PUMP to the moon, also $AAPL", "")
        assert "AAPL" in out
        assert "YOLO" not in out and "MOON" not in out and "PUMP" not in out

    def test_real_cashtag_kept(self):
        ex = TickerExtractor(valid_tickers=_UNIVERSE)
        assert ex.extract("loading up on $GME and $TSLA", "") == ("GME", "TSLA")

    def test_paren_and_exchange_patterns_are_gated(self):
        ex = TickerExtractor(valid_tickers=_UNIVERSE)
        assert "NVDA" in ex.extract("Nvidia (NVDA) jumps; NASDAQ: FAKE flat", "")
        assert "FAKE" not in ex.extract("NASDAQ: FAKE debuts", "")

    def test_name_map_not_gated_even_if_absent_from_universe(self):
        # META isn't in _UNIVERSE, but the curated name map is trusted.
        ex = TickerExtractor(valid_tickers=_UNIVERSE)
        assert "META" in ex.extract("Facebook fined over privacy practices", "")

    def test_cik_pass_not_gated(self):
        ex = TickerExtractor(cik_map={1326200: "GNK"}, valid_tickers=_UNIVERSE)
        # GNK isn't in _UNIVERSE, but a CIK resolution is authoritative.
        out = ex.extract("8-K - GENCO SHIPPING & TRADING LTD (0001326200) (Filer)", "")
        assert "GNK" in out

    def test_validate_false_forces_gating_off(self):
        ex = TickerExtractor(valid_tickers=_UNIVERSE)
        # Structured feeds pass validate=False to keep un-gated behavior.
        assert "YOLO" in ex.extract("$YOLO surges", "", validate=False)

    def test_validate_true_is_noop_without_universe(self):
        ex = TickerExtractor()
        assert "YOLO" in ex.extract("$YOLO surges", "", validate=True)

    def test_crypto_allowlist_survives(self):
        ex = TickerExtractor(valid_tickers=set(_UNIVERSE) | set(CRYPTO_TICKERS))
        out = ex.extract("$BTC and $ETH pumping, $NOTREAL dumping", "")
        assert "BTC" in out and "ETH" in out
        assert "NOTREAL" not in out

    def test_set_valid_tickers_toggles_gating(self):
        ex = TickerExtractor()
        assert "YOLO" in ex.extract("$YOLO", "")     # no universe yet
        ex.set_valid_tickers(_UNIVERSE)
        assert "YOLO" not in ex.extract("$YOLO", "")  # gated after install
        ex.set_valid_tickers(None)
        assert "YOLO" in ex.extract("$YOLO", "")     # cleared → un-gated again


class TestExtractSocialTickers:
    def test_platform_symbols_validated_and_x_suffix_stripped(self):
        ex = TickerExtractor(valid_tickers=set(_UNIVERSE) | set(CRYPTO_TICKERS))
        # StockTwits-style extra: watchlist ticker + resolved symbol list.
        extra = {"ticker": "BTC.X", "symbols": ["AAPL", "FAKE"]}
        out = extract_social_tickers(ex, "some post about $TSLA", "", extra)
        assert "AAPL" in out       # real, from platform symbols
        assert "BTC" in out        # crypto, .X stripped, allowlisted
        assert "TSLA" in out       # real cashtag from text
        assert "FAKE" not in out   # bogus platform symbol rejected

    def test_fake_cashtags_dropped_in_social_helper(self):
        ex = TickerExtractor(valid_tickers=_UNIVERSE)
        out = extract_social_tickers(ex, "$YOLO $MOON diamond hands $GME", "", {})
        assert out == ("GME",)

    def test_no_universe_social_helper_keeps_everything(self):
        ex = TickerExtractor()  # no gating
        out = extract_social_tickers(ex, "$YOLO to the moon", "", {"symbols": ["ABC"]})
        assert "YOLO" in out and "ABC" in out
