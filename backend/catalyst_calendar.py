"""
catalyst_calendar.py
====================
Forward event calendar fed by the deep read's own output.

The cluster deep read already extracts ``is_forward_looking`` + ``event_date``
(PDUFA dates, scheduled votes, guidance dates) — until now those grades were
persisted on the run and forgotten. This module records them into a small
``catalyst_calendar`` collection and hands them back to later runs, closing two
loops:

1. **Priced-in corroboration** — when a story cluster lands on a ticker whose
   event was already on the calendar, the deep read's input gains a SCHEDULED
   line, so ``is_priced_in`` (rule 5) is grounded in a known date instead of
   inferred from prose alone.
2. **Lockup projection** — the one forward event that never announces itself:
   when an IPO grade lands, 90- and 180-day lockup-expiry entries are generated
   immediately (standard windows; ``subtype`` marks them as estimates).

Deliberately *internal*: no new product surface, no external calendar feeds to
maintain. Coverage is exactly what flowed through the news window — sparse at
first, accumulating with every run. Entries are keyed ``ticker:event_type:date``
so re-runs upsert instead of duplicating, and expired entries are pruned on
write. All Mongo I/O degrades silently (never breaks a ranking run); the
extraction/date logic is pure and unit-testable offline.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("catalyst_calendar")

# Standard IPO lockup windows (days). Real lockups vary by deal (some 25-day,
# some staggered) — these are projections, flagged via subtype, not certainties.
LOCKUP_WINDOWS_DAYS = (90, 180)

# Entries older than this many days past their event_date are pruned on write.
_EXPIRY_GRACE_DAYS = 7

_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


# --- Pure helpers ------------------------------------------------------------ #

def _iso_date(value: Any) -> Optional[str]:
    """Normalise an event_date-ish value to 'YYYY-MM-DD', else None."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str):
        m = _ISO_DATE_RE.match(value.strip())
        if m:
            return m.group(1)
    return None


def entry_id(ticker: str, event_type: str, event_date: str) -> str:
    return f"{ticker}:{event_type}:{event_date}"


def extract_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Forward-calendar entries from one ranking run's deep-read grades (pure).

    Two sources:
    - grades (incl. additional_catalysts) with ``is_forward_looking`` and a
      parseable ``event_date`` -> one entry per (ticker, event_type, date);
    - material IPO grades -> projected lockup_expiry entries at the standard
      windows from the run date.

    Only ``primary_ticker`` is recorded — affected peers' dates are the same
    event and would double-count it under other tickers.
    """
    deep = result.get("deep_read") or {}
    generated_at = result.get("generated_at")
    run_date = generated_at.date() if isinstance(generated_at, datetime) else None
    run_id = result.get("run_id")

    entries: dict[str, dict[str, Any]] = {}

    def _add(ticker: str, event_type: str, event_date: str,
             subtype: str, driver: str) -> None:
        eid = entry_id(ticker, event_type, event_date)
        entries.setdefault(eid, {
            "_id": eid,
            "ticker": ticker,
            "event_type": event_type,
            "event_date": event_date,   # ISO 'YYYY-MM-DD'; lexicographic == chronological
            "subtype": subtype,
            "driver": driver,
            "source_run_id": run_id,
        })

    for g in deep.get("grades") or []:
        grade = g.get("grade")
        if not grade:
            continue
        for sub in [grade, *grade.get("additional_catalysts", [])]:
            ticker = sub.get("primary_ticker")
            if not ticker:
                continue
            if sub.get("is_forward_looking"):
                date = _iso_date(sub.get("event_date"))
                if date:
                    _add(ticker, sub["event_type"], date,
                         sub.get("subtype", ""), sub.get("driver", ""))
            if (sub.get("event_type") == "ipo" and sub.get("is_material")
                    and run_date is not None):
                for days in LOCKUP_WINDOWS_DAYS:
                    date = (run_date + timedelta(days=days)).isoformat()
                    _add(ticker, "lockup_expiry", date,
                         f"projected {days}d from IPO",
                         f"{ticker} lockup window (~{days}d) projected from IPO "
                         f"graded {run_date.isoformat()}.")
    return list(entries.values())


# --- Mongo I/O (never raises out) --------------------------------------------- #

async def record_run(
    collection: Any, result: dict[str, Any], *, now: Optional[datetime] = None
) -> int:
    """
    Upsert one run's calendar entries and prune expired ones. Returns the
    number of entries written; 0 (with a log line) on any store failure —
    calendar bookkeeping must never take a ranking run down.
    """
    now = now or datetime.now(tz=timezone.utc)
    entries = extract_entries(result)
    written = 0
    try:
        for e in entries:
            await collection.update_one(
                {"_id": e["_id"]},
                {"$set": {**e, "last_seen": now},
                 "$setOnInsert": {"first_seen": now}},
                upsert=True,
            )
            written += 1
        cutoff = (now - timedelta(days=_EXPIRY_GRACE_DAYS)).date().isoformat()
        await collection.delete_many({"event_date": {"$lt": cutoff}})
    except Exception as exc:  # noqa: BLE001
        log.warning("calendar record failed (%s) — continuing", type(exc).__name__)
    return written


async def lookup_scheduled(
    collection: Any,
    tickers: list[str],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """
    ``{ticker -> [entries]}`` for calendar events dated inside [start, end] —
    the session being ranked. Empty dict on any failure (uncorroborated run,
    same as before the calendar existed).
    """
    if not tickers:
        return {}
    lo, hi = start.date().isoformat(), end.date().isoformat()
    try:
        docs = await (
            collection.find(
                {"ticker": {"$in": tickers}, "event_date": {"$gte": lo, "$lte": hi}},
                {"_id": 0, "ticker": 1, "event_type": 1, "event_date": 1, "subtype": 1},
            ).limit(200).to_list(length=200)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("calendar lookup failed (%s) — no SCHEDULED context", type(exc).__name__)
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        out.setdefault(d["ticker"], []).append(d)
    return out
