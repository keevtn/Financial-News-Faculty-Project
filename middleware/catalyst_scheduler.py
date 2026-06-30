"""
catalyst_scheduler.py
=====================
Operationalizes the catalyst ranker. Instead of only running when someone hits
the manual endpoints, this:

  1. Generates a ranking automatically **pre-market** each trading day, and
  2. **Auto-grades** past runs once the session they predicted has closed,

so the system banks an auditable track record on its own — the evidence that
turns "we built an AI ranker" into "here is how it has actually performed."

Gated by ``RUN_CATALYST_SCHEDULER`` (default off). Runs in-process as a
background task (same pattern as ingestion_runner), so it depends on the web
service being awake — keep the uptime pinger on ``/health``.

Environment
-----------
  RUN_CATALYST_SCHEDULER   enable the scheduler            (default: off)
  CATALYST_CHECK_INTERVAL  seconds between checks          (default: 900 = 15 min)
  CATALYST_RUN_HOUR_ET     ET hour after which the daily run fires (default: 8)
  CATALYST_SCHED_LLM       use the LLM for scheduled runs  (default: true; set
                           false for free, quantitative-only runs)

Cost note: a scheduled LLM run spends Anthropic credits once per trading day.
Set CATALYST_SCHED_LLM=false to build the track record for free (quantitative),
or point CATALYST_MODEL at a cheaper model.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time as dtime, timezone
from typing import Any

log = logging.getLogger("middleware.catalyst_scheduler")


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


def _should_run(now: datetime, latest_generated_at: Any, run_hour_et: int) -> bool:
    """
    True iff a new ranking should be generated: it's a trading day, we're past
    the configured ET run-hour, and no run has been generated yet today. (Runs
    once per trading day at the first check after the run-hour.)
    """
    from market_calendar import is_trading_day, ET

    now_et = now.astimezone(ET)
    today = now_et.date()
    if not is_trading_day(today):
        return False
    run_after = datetime.combine(today, dtime(run_hour_et, 0), tzinfo=ET).astimezone(timezone.utc)
    if now < run_after:
        return False
    if isinstance(latest_generated_at, datetime) and latest_generated_at.astimezone(ET).date() == today:
        return False  # already ran today
    return True


async def _load_tuned_weights(meta: Any) -> Any:
    """Auto-tuned pre-score weights from catalyst_meta, or None (→ defaults)."""
    if meta is None:
        return None
    try:
        doc = await meta.find_one({"_id": "weights"}, {"_id": 0, "weights": 1})
    except Exception:  # noqa: BLE001
        return None
    if doc and isinstance(doc.get("weights"), dict):
        return {k: float(v) for k, v in doc["weights"].items()}
    return None


async def _tick(app: Any) -> None:
    news = getattr(app.state, "news_collection", None)
    coll = getattr(app.state, "rankings_collection", None)
    meta = getattr(app.state, "catalyst_meta_collection", None)
    if news is None or coll is None:
        return

    from catalyst_ranker import (
        CATALYST_PROFILES,
        get_latest_ranking,
        grade_run,
        rank_catalysts,
        save_ranking,
    )

    now = datetime.now(tz=timezone.utc)
    run_hour = _env_int("CATALYST_RUN_HOUR_ET", 8)
    use_llm = _env_flag("CATALYST_SCHED_LLM", True)

    # 1) Pre-market run, once per trading day, per profile (combined + regulatory).
    for profile in CATALYST_PROFILES:
        try:
            latest = await get_latest_ranking(coll, profile=profile)
            latest_gen = latest.get("generated_at") if latest else None
            if _should_run(now, latest_gen, run_hour):
                log.info("catalyst scheduler: generating pre-market ranking (profile=%s llm=%s)",
                         profile, use_llm)
                result = await rank_catalysts(
                    news, use_llm=use_llm, trigger="scheduled",
                    weights=await _load_tuned_weights(meta), profile=profile,
                )
                await save_ranking(coll, result)
                log.info("catalyst scheduler: ranking saved (profile=%s run_id=%s used_llm=%s)",
                         profile, result.get("run_id"), result.get("used_llm"))
        except Exception as exc:  # noqa: BLE001
            log.error("catalyst scheduler run step failed [profile=%s]: %s", profile, exc)

    # 2) Auto-grade ungraded runs whose session has now closed.
    try:
        ungraded = await (
            coll.find({"metrics": {"$exists": False}}, {"_id": 0})
            .sort("generated_at", -1)
            .limit(10)
            .to_list(length=10)
        )
        for run_doc in ungraded:
            metrics = await grade_run(coll, run_doc, now=now)
            if metrics is not None:
                log.info("catalyst scheduler: graded run %s (hit_rate=%s)",
                         run_doc.get("run_id"), metrics.get("direction_hit_rate"))
    except Exception as exc:  # noqa: BLE001
        log.error("catalyst scheduler grade step failed: %s", exc)


async def _loop(app: Any, interval: float) -> None:
    await asyncio.sleep(10)  # let DB / model init settle first
    while True:
        await _tick(app)
        await asyncio.sleep(interval)


async def start_catalyst_scheduler(app: Any) -> None:
    """Start the scheduler background task when RUN_CATALYST_SCHEDULER is set."""
    app.state.catalyst_scheduler_task = None
    if not _env_flag("RUN_CATALYST_SCHEDULER"):
        log.info("RUN_CATALYST_SCHEDULER not set — catalyst scheduler disabled")
        return
    interval = _env_float("CATALYST_CHECK_INTERVAL", 900.0)
    app.state.catalyst_scheduler_task = asyncio.create_task(
        _loop(app, interval), name="catalyst-scheduler"
    )
    log.info("catalyst scheduler started — interval=%ss run_hour_et=%s llm=%s",
             interval, _env_int("CATALYST_RUN_HOUR_ET", 8), _env_flag("CATALYST_SCHED_LLM", True))


async def stop_catalyst_scheduler(app: Any) -> None:
    task = getattr(app.state, "catalyst_scheduler_task", None)
    if task is not None:
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            log.error("error stopping catalyst scheduler: %s", exc)
