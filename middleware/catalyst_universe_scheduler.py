"""
catalyst_universe_scheduler.py
==============================
Drives the candidate-universe accumulation + weight auto-tune on a slow cadence
(default every 12h), separate from the once-per-trading-day catalyst ranker.

Each tick:
  1. ``accumulate`` — fold new sub-threshold tickers into the candidate universe
     (new-data-only; idempotent), then
  2. ``auto_tune`` — incrementally nudge the pre-score weights from the graded
     track record (a no-op until enough graded runs exist).

Neither step calls the LLM, so this scheduler costs **zero Anthropic credits**.

Gated by ``RUN_CATALYST_UNIVERSE_SCHEDULER`` (default off). Runs in-process as a
background task (same pattern as catalyst_scheduler / squeeze_scheduler), so it
depends on the web service being awake.

Environment
-----------
  RUN_CATALYST_UNIVERSE_SCHEDULER   enable the scheduler          (default: off)
  CATALYST_UNIVERSE_INTERVAL        seconds between cycles        (default: 43200 = 12h)
  CATALYST_UNIVERSE_LOOKBACK_HOURS  first-run lookback window     (default: 12)
  CATALYST_TUNE_ENABLED             run the weight auto-tune step (default: off)
  CATALYST_TUNE_MIN_GRADED          graded runs required to tune  (default: 10)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger("middleware.catalyst_universe_scheduler")


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


async def _tick(app: Any) -> None:
    news = getattr(app.state, "news_collection", None)
    universe = getattr(app.state, "universe_collection", None)
    meta = getattr(app.state, "catalyst_meta_collection", None)
    rankings = getattr(app.state, "rankings_collection", None)
    if news is None or universe is None or meta is None:
        return

    # 1) Accumulate new sub-threshold candidates.
    try:
        from catalyst_universe import accumulate
        await accumulate(
            news, universe, meta,
            lookback_hours=_env_int("CATALYST_UNIVERSE_LOOKBACK_HOURS", 12),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("catalyst universe accumulate step failed: %s", exc)

    # 2) Optionally auto-tune the pre-score weights (default off; no LLM cost).
    if _env_flag("CATALYST_TUNE_ENABLED") and rankings is not None:
        try:
            from catalyst_universe import auto_tune
            result = await auto_tune(
                rankings, meta, min_graded=_env_int("CATALYST_TUNE_MIN_GRADED", 10)
            )
            log.info("catalyst auto-tune: %s", result)
        except Exception as exc:  # noqa: BLE001
            log.error("catalyst universe auto-tune step failed: %s", exc)


async def _loop(app: Any, interval: float) -> None:
    await asyncio.sleep(15)  # let DB / model init settle first
    while True:
        await _tick(app)
        await asyncio.sleep(interval)


async def start_catalyst_universe_scheduler(app: Any) -> None:
    """Start the background task when RUN_CATALYST_UNIVERSE_SCHEDULER is set."""
    app.state.catalyst_universe_scheduler_task = None
    if not _env_flag("RUN_CATALYST_UNIVERSE_SCHEDULER"):
        log.info("RUN_CATALYST_UNIVERSE_SCHEDULER not set — universe scheduler disabled")
        return
    interval = _env_float("CATALYST_UNIVERSE_INTERVAL", 43200.0)  # 12h
    app.state.catalyst_universe_scheduler_task = asyncio.create_task(
        _loop(app, interval), name="catalyst-universe-scheduler"
    )
    log.info(
        "catalyst universe scheduler started — interval=%ss tune=%s",
        interval, _env_flag("CATALYST_TUNE_ENABLED"),
    )


async def stop_catalyst_universe_scheduler(app: Any) -> None:
    task = getattr(app.state, "catalyst_universe_scheduler_task", None)
    if task is not None:
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            log.error("error stopping catalyst universe scheduler: %s", exc)
