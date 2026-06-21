"""
ingestion_runner.py
===================
Runs the backend ingestion pipelines as background tasks *inside* the FastAPI
web service, so the feeds stay fresh without a separate worker process or a
local machine left running.

Two independent pipelines, each gated by its own env flag (both default off):

  * ``RUN_INGESTION``  — structured sources (RSS / SEC / FDA).
  * ``RUN_SOCIAL``     — social sources (StockTwits + Bluesky). Sub-toggles
    ``RUN_STOCKTWITS`` / ``RUN_BLUESKY`` (default on) disable one source.

They are decoupled: you can run either, both, or neither. On the hosted
deployment (e.g. Render) set the flags you want; locally leave them unset since
``start.ps1`` already launches ingestion in its own windows (avoids double-poll).

Free-tier note: a Render free web service is paused after ~15 minutes with no
inbound HTTP, which also suspends these background tasks. Keep it awake with an
external uptime pinger (cron-job.org, UptimeRobot, ...) hitting ``/health``
every ~10 minutes.

Sentiment: the lean deployment has no FinBERT (no torch), so structured items
are scored at write time with the zero-dependency Loughran-McDonald analyzer.
Social items are always scored by MongoHandler internally (StockTwits label →
LM fallback), independent of this choice.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("middleware.ingestion")


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a truthy/falsey environment variable."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to ``default`` on missing/invalid."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


async def start_ingestion(app: Any) -> None:
    """
    Build and start whichever ingestion pipelines are enabled, stashing handles
    on ``app.state`` so the lifespan shutdown can stop them cleanly.

    No-ops (recording ``None`` handles) when neither flag is set or no
    MONGODB_URI is configured, so the API still serves reads normally.
    """
    app.state.ingestion_agent = None
    app.state.ingestion_storage = {}
    app.state.social_agent = None
    app.state.social_storage = {}

    run_structured = _env_flag("RUN_INGESTION")
    run_social = _env_flag("RUN_SOCIAL")
    if not (run_structured or run_social):
        log.info("RUN_INGESTION / RUN_SOCIAL not set — skipping in-process ingestion")
        return

    mongo_uri = os.environ.get("MONGODB_URI")
    if not mongo_uri:
        log.warning("ingestion requested but MONGODB_URI is missing — ingestion disabled")
        return

    try:
        from storage_handlers import attach_storage
        from sentiment import LoughranMcDonaldAnalyzer
    except Exception as exc:  # noqa: BLE001
        log.error("Ingestion deps unavailable (%s) — ingestion disabled", exc)
        return

    # One LM analyzer shared by both pipelines' Mongo handlers.
    analyzer = LoughranMcDonaldAnalyzer()

    if run_structured:
        await _start_structured(app, mongo_uri, analyzer, attach_storage)
    if run_social:
        await _start_social(app, mongo_uri, analyzer, attach_storage)


async def _start_structured(app: Any, mongo_uri: str, analyzer: Any, attach_storage: Any) -> None:
    """RSS / SEC / FDA pipeline. Relaxed intervals — shares one free-tier process."""
    try:
        from IngestionModule import IngestionAgent
    except Exception as exc:  # noqa: BLE001
        log.error("Structured ingestion deps unavailable (%s) — skipped", exc)
        return

    agent = IngestionAgent(
        rss_poll_interval=_env_float("RSS_POLL_INTERVAL", 120.0),
        sec_poll_interval=_env_float("SEC_POLL_INTERVAL", 600.0),
        fda_poll_interval=_env_float("FDA_POLL_INTERVAL", 600.0),
        enable_rss=True,
        enable_sec=True,
        enable_fda=True,
    )
    storage = attach_storage(
        agent, enable_mongo=True, mongo_kwargs={"uri": mongo_uri}, analyzer=analyzer
    )
    try:
        await agent.start()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to start structured ingestion agent: %s", exc)
        for h in storage.values():
            await h.close()
        return

    app.state.ingestion_agent = agent
    app.state.ingestion_storage = storage
    log.info("In-process ingestion started — RSS + SEC + FDA, LM sentiment")


async def _start_social(app: Any, mongo_uri: str, analyzer: Any, attach_storage: Any) -> None:
    """StockTwits + Bluesky pipeline (writes social items to the same collection)."""
    try:
        from UnstructuredModule import UnstructuredAgent
    except Exception as exc:  # noqa: BLE001
        log.error("Social ingestion deps unavailable (%s) — skipped", exc)
        return

    enable_st = _env_flag("RUN_STOCKTWITS", True)
    enable_bsky = _env_flag("RUN_BLUESKY", True)
    if not (enable_st or enable_bsky):
        log.info("RUN_SOCIAL set but both sources disabled — skipping social")
        return

    social = UnstructuredAgent(
        enable_stocktwits=enable_st,
        enable_bluesky=enable_bsky,
        stocktwits_interval=_env_float("STOCKTWITS_POLL_INTERVAL", 480.0),
        bluesky_interval=_env_float("BLUESKY_POLL_INTERVAL", 300.0),
    )
    storage = attach_storage(
        social, enable_mongo=True, mongo_kwargs={"uri": mongo_uri}, analyzer=analyzer
    )
    try:
        await social.start()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to start social ingestion agent: %s", exc)
        for h in storage.values():
            await h.close()
        return

    app.state.social_agent = social
    app.state.social_storage = storage
    log.info("In-process social ingestion started — stocktwits=%s bluesky=%s",
             enable_st, enable_bsky)


async def stop_ingestion(app: Any) -> None:
    """Gracefully stop both agents and close their storage handlers."""
    for attr in ("ingestion_agent", "social_agent"):
        agent = getattr(app.state, attr, None)
        if agent is not None:
            try:
                await agent.stop()
            except Exception as exc:  # noqa: BLE001
                log.error("Error stopping %s: %s", attr, exc)

    for attr in ("ingestion_storage", "social_storage"):
        for h in getattr(app.state, attr, {}).values():
            try:
                await h.close()
            except Exception as exc:  # noqa: BLE001
                log.error("Error closing storage handler: %s", exc)
