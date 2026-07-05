"""Unit tests for the Nasdaq Trader symbol-directory parser (pure; no network)."""

from listed_symbols import parse_symbol_directory

# Real column layout of nasdaqlisted.txt:
# Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
_NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
TQQQ|ProShares UltraPro QQQ|G|N|N|100|Y|N
ZWZZT|Nasdaq TEST Common Stock|G|Y|N|100|N|N
File Creation Time: 0704202618:30|||||||"""

# Real column layout of otherlisted.txt:
# ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
_OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
SOXL|Direxion Daily Semiconductor Bull 3X|P|SOXL|Y|100|N|SOXL
IWM|iShares Russell 2000 Index Fund|P|IWM|Y|40|N|IWM
ATEST|NYSE Arca TEST|P|ATEST|N|100|Y|ATEST
File Creation Time: 0704202618:30|||||||"""


class TestParseSymbolDirectory:
    def test_extracts_symbols_and_skips_header_and_trailer(self):
        out = parse_symbol_directory(_NASDAQ, symbol_col=0)
        assert "AAPL" in out and "TQQQ" in out
        assert "SYMBOL" not in out          # header skipped
        assert not any("File Creation" in s for s in out)  # trailer skipped

    def test_skips_test_issues_when_flagged(self):
        out = parse_symbol_directory(_NASDAQ, symbol_col=0, test_issue_col=3)
        assert "ZWZZT" not in out           # Test Issue = Y
        assert "AAPL" in out and "TQQQ" in out

    def test_other_listed_format_covers_etfs(self):
        out = parse_symbol_directory(_OTHER, symbol_col=0, test_issue_col=6)
        assert {"SOXL", "IWM"} <= out       # ETFs the SEC file misses
        assert "ATEST" not in out           # Test Issue = Y
        assert "ACT SYMBOL" not in out

    def test_blank_and_short_lines_are_ignored(self):
        out = parse_symbol_directory("Symbol|Name\n\nSHORT\nGOOD|Some Co", symbol_col=0)
        assert "GOOD" in out and "SHORT" in out  # single-col line still yields its token
        assert "" not in out
