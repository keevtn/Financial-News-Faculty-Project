"""Unit tests for EDGAR CIK→ticker parsing + the extractor's CIK pass (no network)."""

import edgar_tickers as et
from ticker_extractor import TickerExtractor

# A trimmed slice of SEC's company_tickers.json shape.
_RAW = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1326200, "ticker": "GNK", "title": "GENCO SHIPPING & TRADING LTD"},
    "2": {"cik_str": 789019, "ticker": "msft", "title": "MICROSOFT CORP"},  # lower-cased ticker
    "3": {"cik_str": "bad", "ticker": "ZZZ", "title": "Bad CIK"},           # unparseable CIK
    "4": {"cik_str": 111, "ticker": "", "title": "No Ticker"},              # empty ticker
}


class TestParseCompanyTickers:
    def test_builds_cik_map(self):
        m = et.parse_company_tickers(_RAW)
        assert m[320193] == "AAPL"
        assert m[1326200] == "GNK"

    def test_uppercases_ticker(self):
        assert et.parse_company_tickers(_RAW)[789019] == "MSFT"

    def test_skips_unparseable_and_empty(self):
        m = et.parse_company_tickers(_RAW)
        assert 111 not in m                       # empty ticker dropped
        assert "ZZZ" not in m.values()            # bad cik_str dropped

    def test_empty_payload(self):
        assert et.parse_company_tickers({}) == {}
        assert et.parse_company_tickers(None) == {}


_CIK_MAP = {320193: "AAPL", 1326200: "GNK"}


class TestCikPass:
    def test_resolves_edgar_filing_title(self):
        # The exact shape EDGAR emits: "<type> - <NAME> (<10-digit CIK>) (<role>)".
        ex = TickerExtractor(cik_map=_CIK_MAP)
        out = ex.extract("SC 13D/A - GENCO SHIPPING & TRADING LTD (0001326200) (Subject)", "")
        assert "GNK" in out

    def test_unknown_cik_resolves_nothing(self):
        ex = TickerExtractor(cik_map=_CIK_MAP)
        assert ex.extract("8-K - SOME PRIVATE CO (0009999999) (Filer)", "") == ()

    def test_non_cik_numbers_ignored(self):
        # Years / short parenthesised numbers must not be treated as CIKs, even
        # when the bare integer (e.g. 320193) would map — only 10-digit matches.
        ex = TickerExtractor(cik_map=_CIK_MAP)
        assert ex.extract("Outlook for 2024 (2024) and Q3 (320193)", "") == ()

    def test_cik_pass_off_without_map(self):
        # Genco (a small-cap) isn't in the built-in name dict, so with no cik_map
        # there's nothing to resolve — proving the CIK pass is what found it above.
        ex = TickerExtractor()  # no cik_map
        assert ex.extract("8-K - GENCO SHIPPING & TRADING LTD (0001326200) (Filer)", "") == ()

    def test_cik_and_text_passes_combine(self):
        ex = TickerExtractor(cik_map=_CIK_MAP)
        out = ex.extract("Apple ($AAPL) files 8-K - GENCO (0001326200) (Filer)", "")
        assert set(out) == {"AAPL", "GNK"}
