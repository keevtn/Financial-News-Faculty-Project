"""Unit tests for the ticker extractor's name passes (pure; no network)."""

from ticker_extractor import TickerExtractor


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
