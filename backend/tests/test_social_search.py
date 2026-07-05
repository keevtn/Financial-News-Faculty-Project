"""Unit tests for social_search pure helpers (no network)."""

import social_search as ss


class TestCleanTicker:
    def test_strips_cashtag_and_uppercases(self):
        assert ss._clean_ticker("$gme ") == "GME"

    def test_strips_crypto_suffix(self):
        assert ss._clean_ticker("btc.x") == "BTC"


class TestCashtagRegex:
    def test_finds_distinct_cashtags(self):
        tags = {c.upper() for c in ss._CASHTAG_RE.findall("$GME to the moon $gme and $AMC")}
        assert tags == {"$GME", "$AMC"}

    def test_ignores_plain_dollar_amounts(self):
        # "$5" / "$100" are not 1-6 letter cashtags
        tags = ss._CASHTAG_RE.findall("up $5 to $100 on $TSLA")
        assert tags == ["$TSLA"]


class TestParseDt:
    def test_handles_none(self):
        # falls back to an aware datetime, never raises
        dt = ss._parse_dt(None)
        assert dt.tzinfo is not None
