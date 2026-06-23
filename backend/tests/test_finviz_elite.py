"""Unit tests for the Finviz Elite export parsers (pure; no network)."""

import finviz_elite as fe


class TestNum:
    def test_plain_float(self):
        assert fe._num("123.5") == 123.5

    def test_strips_commas(self):
        assert fe._num("1,020,859") == 1020859.0

    def test_suffix_million(self):
        assert fe._num("4.4M") == 4_400_000.0

    def test_suffix_billion(self):
        assert fe._num("2.33B") == 2_330_000_000.0

    def test_dash_and_na_are_none(self):
        assert fe._num("-") is None
        assert fe._num("N/A") is None

    def test_none_input(self):
        assert fe._num(None) is None


class TestMarketCap:
    def test_plain_number_is_millions(self):
        # Elite reports market cap in millions as a bare number
        assert fe._market_cap("329.74") == 329_740_000.0

    def test_suffixed_value_is_already_dollars(self):
        assert fe._market_cap("2.33B") == 2_330_000_000.0

    def test_blank_is_none(self):
        assert fe._market_cap("-") is None


class TestPct:
    def test_positive(self):
        assert fe._pct("3.84%") == 3.84

    def test_negative(self):
        assert fe._pct("-9.28%") == -9.28

    def test_blank_is_none(self):
        assert fe._pct("-") is None


class TestInt:
    def test_from_commas(self):
        assert fe._int("1,020,859") == 1020859

    def test_none(self):
        assert fe._int("-") is None


class TestToken:
    def test_bare_token(self, monkeypatch):
        monkeypatch.setenv("FINVIZ_AUTH_TOKEN", "abc123")
        assert fe._token() == "abc123"

    def test_extracts_from_pasted_export_url(self, monkeypatch):
        monkeypatch.setenv(
            "FINVIZ_AUTH_TOKEN",
            "https://elite.finviz.com/export.ashx?v=111&f=cap_large&auth=tok_DEADBEEF&o=-change",
        )
        assert fe._token() == "tok_DEADBEEF"

    def test_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("FINVIZ_AUTH_TOKEN", raising=False)
        assert fe._token() is None
        assert fe.has_token() is False

    def test_has_token_true(self, monkeypatch):
        monkeypatch.setenv("FINVIZ_AUTH_TOKEN", "x")
        assert fe.has_token() is True


class TestRowParsing:
    def test_row_from_csv(self):
        row = fe._row_from_csv({
            "Ticker": "cast", "Company": "Caster Inc", "Sector": "Tech",
            "Industry": "Software", "Country": "USA", "Market Cap": "329.74",
            "P/E": "15.2", "Price": "7.32", "Change": "-9.28%", "Volume": "1,020,859",
        })
        assert row["ticker"] == "CAST"           # upper-cased
        assert row["company"] == "Caster Inc"
        assert row["market_cap"] == 329_740_000.0
        assert row["price"] == 7.32
        assert row["change_pct"] == -9.28
        assert row["volume"] == 1020859

    def test_row_from_csv_requires_ticker(self):
        assert fe._row_from_csv({"Ticker": "", "Company": "X"}) is None

    def test_parse_csv(self):
        text = (
            "Ticker,Company,Sector,Industry,Country,Market Cap,P/E,Price,Change,Volume\n"
            "AAA,Acme,Tech,Software,USA,500,10,5.00,1.20%,1000\n"
        )
        rows = fe._parse_csv(text)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "AAA"
        assert rows[0]["market_cap"] == 500_000_000.0
