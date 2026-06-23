"""Unit tests for the stdlib-only market calendar + overnight-news window.

Dates use a clean week in March 2026 (no listed market holidays): Mon 2026-03-09
through Fri 2026-03-13, then Sat 03-14 and the following Mon 03-16. Window
assertions compare against the calendar's own session_open/close to stay robust
across DST rather than hard-coding UTC offsets.
"""

from datetime import date, datetime, timezone

import market_calendar as mc


def _utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestTradingDay:
    def test_saturday_is_not_trading(self):
        assert not mc.is_trading_day(date(2026, 3, 14))

    def test_holiday_is_not_trading(self):
        assert not mc.is_trading_day(date(2026, 2, 16))  # Washington's Birthday

    def test_normal_weekday_is_trading(self):
        assert mc.is_trading_day(date(2026, 3, 10))  # Tuesday


class TestAdjacentTradingDays:
    def test_previous_skips_weekend(self):
        assert mc.previous_trading_day(date(2026, 3, 16)) == date(2026, 3, 13)

    def test_next_skips_weekend(self):
        assert mc.next_trading_day(date(2026, 3, 13)) == date(2026, 3, 16)

    def test_previous_skips_holiday(self):
        # Tue 2026-02-17 -> prev trading day skips Mon holiday (02-16) to Fri 02-13
        assert mc.previous_trading_day(date(2026, 2, 17)) == date(2026, 2, 13)


class TestOvernightWindow:
    def test_premarket_end_clamped_to_now(self):
        # 08:00 ET, before the 09:30 open: start = prior close, end clamps to now
        # (the open is still in the future).
        now = _utc(2026, 3, 10, 12, 0)
        start, end = mc.overnight_window(now)
        assert start == mc.session_close(date(2026, 3, 9))
        assert end == now

    def test_intraday_window(self):
        now = _utc(2026, 3, 10, 18, 0)  # 14:00 ET, mid-session
        start, end = mc.overnight_window(now)
        assert start == mc.session_open(date(2026, 3, 10))
        assert end == now

    def test_postclose_window(self):
        now = _utc(2026, 3, 10, 21, 0)  # 17:00 ET, after the 16:00 close
        start, end = mc.overnight_window(now)
        assert start == mc.session_close(date(2026, 3, 10))
        assert end == now  # next open is future -> clamped

    def test_weekend_window(self):
        now = _utc(2026, 3, 14, 12, 0)  # Saturday
        start, end = mc.overnight_window(now)
        assert start == mc.session_close(date(2026, 3, 13))
        assert end == now


class TestNextSessionBounds:
    def test_after_friday_close_is_monday(self):
        after = _utc(2026, 3, 13, 22, 0)  # Friday evening
        assert mc.next_session_bounds(after) == (
            mc.session_open(date(2026, 3, 16)), mc.session_close(date(2026, 3, 16)))

    def test_before_open_is_same_day(self):
        after = _utc(2026, 3, 10, 12, 0)  # 08:00 ET, before open
        assert mc.next_session_bounds(after) == (
            mc.session_open(date(2026, 3, 10)), mc.session_close(date(2026, 3, 10)))
