"""
market_calendar.py
==================
Minimal US equity-market calendar — trading-day checks and the "overnight news"
window, with zero third-party dependencies (stdlib ``zoneinfo`` only).

Why this exists
---------------
The catalyst ranker batches news that broke *while the market was closed* and
ranks tickers before the next open. That requires knowing:

  * whether a given date is a trading day (skip weekends + holidays),
  * the previous session's close and the next session's open,
  * the overnight window = (previous close, next open) in UTC.

This is intentionally small. It covers NYSE/Nasdaq regular sessions and the
standard full-day holidays through 2026. Early-close (half) days are treated as
regular sessions — the half-hour difference is immaterial to a news window that
spans ~17 hours. Extend ``_HOLIDAYS`` as years roll over.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)   # 9:30 AM ET
MARKET_CLOSE = time(16, 0)  # 4:00 PM ET

# Full-day NYSE/Nasdaq closures. Observed dates (the actual closed day).
_HOLIDAYS: frozenset[date] = frozenset({
    # 2025
    date(2025, 1, 1),    # New Year's Day
    date(2025, 1, 9),    # National Day of Mourning (Carter) — markets closed
    date(2025, 1, 20),   # MLK Jr. Day
    date(2025, 2, 17),   # Washington's Birthday
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
})


def is_trading_day(d: date) -> bool:
    """True if ``d`` is a regular US-equity trading day (weekday, not a holiday)."""
    return d.weekday() < 5 and d not in _HOLIDAYS


def previous_trading_day(d: date) -> date:
    """The most recent trading day strictly before ``d``."""
    probe = d - timedelta(days=1)
    while not is_trading_day(probe):
        probe -= timedelta(days=1)
    return probe


def next_trading_day(d: date) -> date:
    """The next trading day strictly after ``d``."""
    probe = d + timedelta(days=1)
    while not is_trading_day(probe):
        probe += timedelta(days=1)
    return probe


def _et_dt(d: date, t: time) -> datetime:
    """Build a timezone-aware ET datetime from a date + wall-clock time."""
    return datetime.combine(d, t, tzinfo=ET)


def session_close(d: date) -> datetime:
    """UTC datetime of ``d``'s regular-session close (assumes ``d`` is a trading day)."""
    return _et_dt(d, MARKET_CLOSE).astimezone(timezone.utc)


def session_open(d: date) -> datetime:
    """UTC datetime of ``d``'s regular-session open (assumes ``d`` is a trading day)."""
    return _et_dt(d, MARKET_OPEN).astimezone(timezone.utc)


def overnight_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """
    Return the (start, end) UTC bounds of the current "overnight news" window:
    from the previous session's close up to the upcoming session's open.

    Behaviour, in ET terms, relative to ``now``:
      * Before today's open on a trading day  -> (prev close, today's open)
      * After today's close / weekend / holiday -> (last close, next open)
      * During the session                     -> (today's open, now)
        (so an intraday run still ranks what has accumulated since the open)

    ``end`` is never in the future: it is clamped to ``now`` so callers don't
    query for news that can't exist yet.
    """
    now = now or datetime.now(tz=timezone.utc)
    now_et = now.astimezone(ET)
    today = now_et.date()

    if is_trading_day(today):
        open_dt = session_open(today)
        close_dt = session_close(today)
        if now < open_dt:
            # Pre-market: window is last close -> today's open.
            start = session_close(previous_trading_day(today))
            end = open_dt
        elif now < close_dt:
            # Intraday: window is today's open -> now.
            start = open_dt
            end = now
        else:
            # Post-close: window is today's close -> next open.
            start = close_dt
            end = session_open(next_trading_day(today))
    else:
        # Weekend / holiday: last close -> next open.
        start = session_close(previous_trading_day(today))
        end = session_open(next_trading_day(today))

    # Never look into the future.
    if end > now:
        end = now
    return start, end


def next_session_bounds(after: datetime) -> tuple[datetime, datetime]:
    """
    Return the (open, close) UTC bounds of the first trading session that opens
    at or after ``after``. Used by the evaluation step to measure how a ranked
    ticker actually moved on the session following a ranking run.
    """
    after_et = after.astimezone(ET)
    d = after_et.date()
    if is_trading_day(d) and after < session_open(d):
        sess = d
    else:
        sess = next_trading_day(d)
    return session_open(sess), session_close(sess)
