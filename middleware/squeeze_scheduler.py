"""
squeeze_scheduler.py
====================
Runs the short-squeeze ranker on its **own background lane**, separate from the
catalyst scheduler and the news-feed ingestion. Squeeze signal is intraday and
social-driven, so it refreshes a few times per trading day rather than once
pre-market.

It's an on-demand burst (universe screen + a few dozen yfinance/Bluesky lookups),
not a continuous poller — so it can't starve the feed pollers the way the RSS
lane once did. Gated by ``RUN_SQUEEZE_SCHEDULER`` (default off).

Environment
-----------
  RUN_SQUEEZE_SCHEDULER    enable the scheduler                 (default: off)
  SQUEEZE_CHECK_INTERVAL   seconds between loop checks          (default: 3600 = 1h)
  SQUEEZE_RUN_INTERVAL     min seconds between actual runs      (default: 14400 = 4h)
  SQUEEZE_TOP_K            how many ranked names to persist     (default: 15)
  SQUEEZE_MIN_SHORT_FLOAT  min short %float to be "fueled"      (default: 0.10)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("middleware.squeeze_scheduler")


def _env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _should_run(now: datetime, latest_generated_at: Any, min_gap_s: float) -> bool:
    """Run on trading days, no more often than ``min_gap_s`` since the last run."""
    from market_calendar import is_trading_day, ET

    if not is_trading_day(now.astimezone(ET).date()):
        return False
    if isinstance(latest_generated_at, datetime):
        age = (now - latest_generated_at).total_seconds()
        if age < min_gap_s:
            return False
    return True


async def _tick(app: Any) -> None:
    coll = getattr(app.state, "squeeze_collection", None)
    if coll is None:
        return
    from squeeze_ranker import (
        get_latest_squeeze,
        grade_squeeze_run,
        rank_squeezes,
        save_squeeze_ranking,
    )

    now = datetime.now(tz=timezone.utc)

    # 1) Refresh the ranking on the configured cadence.
    try:
        latest = await get_latest_squeeze(coll)
        latest_gen = latest.get("generated_at") if latest else None
        if _should_run(now, latest_gen, _env_float("SQUEEZE_RUN_INTERVAL", 14400.0)):
            log.info("squeeze scheduler: generating ranking")
            result = await rank_squeezes(
                top_k=_env_int("SQUEEZE_TOP_K", 15),
                min_short_float=_env_float("SQUEEZE_MIN_SHORT_FLOAT", 0.10),
                social_collection=getattr(app.state, "news_collection", None),
                now=now,
            )
            await save_squeeze_ranking(coll, result)
            log.info("squeeze scheduler: saved run_id=%s (%d ranked, %d fueled)",
                     result.get("run_id"), len(result.get("items", [])), result.get("fueled_count"))
            # Real-time push (Phase 4): best-effort, never blocks the lane.
            try:
                from squeeze_stream import publish_squeeze_run
                await publish_squeeze_run(result)
            except Exception as exc:  # noqa: BLE001
                log.info("squeeze scheduler: stream publish skipped (%s)", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001
        log.error("squeeze scheduler run step failed: %s", exc)

    # 2) Auto-grade ungraded runs whose window has fully closed.
    try:
        ungraded = await (
            coll.find({"metrics": {"$exists": False}}, {"_id": 0})
            .sort("generated_at", -1).limit(20).to_list(length=20)
        )
        for run_doc in ungraded:
            metrics = await grade_squeeze_run(coll, run_doc, now=now)
            if metrics is not None:
                log.info("squeeze scheduler: graded run %s (hit_rate=%s)",
                         run_doc.get("run_id"), metrics.get("squeeze_hit_rate"))
    except Exception as exc:  # noqa: BLE001
        log.error("squeeze scheduler grade step failed: %s", exc)


async def _loop(app: Any, interval: float) -> None:
    await asyncio.sleep(15)  # let DB settle first
    while True:
        await _tick(app)
        await asyncio.sleep(interval)


async def start_squeeze_scheduler(app: Any) -> None:
    """Start the squeeze scheduler when RUN_SQUEEZE_SCHEDULER is set."""
    app.state.squeeze_scheduler_task = None
    if not _env_flag("RUN_SQUEEZE_SCHEDULER"):
        log.info("RUN_SQUEEZE_SCHEDULER not set — squeeze scheduler disabled")
        return
    interval = _env_float("SQUEEZE_CHECK_INTERVAL", 3600.0)
    app.state.squeeze_scheduler_task = asyncio.create_task(
        _loop(app, interval), name="squeeze-scheduler"
    )
    log.info("squeeze scheduler started — check_interval=%ss run_interval=%ss",
             interval, _env_float("SQUEEZE_RUN_INTERVAL", 14400.0))


a