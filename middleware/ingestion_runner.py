"""
ingestion_runner.py
===================
Runs the backend ingestion pipeline as a background task *inside* the FastAPI
web service, so the feeds stay fresh without a separate worker process or a
local machine left running.

Gated by the ``RUN_INGESTION`` environment variable (default: off). This is
deliberate:

  * On the hosted deployment (e.g. Render) set ``RUN_INGESTION=true`` so the
    one always-on web process also polls RSS / SEC / FDA and writes to MongoDB.
  * Locally, ``start.ps1`` already launches the ingestion processes in their own
    windows. Leave ``RUN_INGESTION`` unset there so you don't double-poll the
    same feeds into the same database.

Free-tier note: a Render free web service is paused after ~15 minutes with no
inbound HTTP, which also suspends this background task. Keep it awake with an
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
    Build and start the ingestion agent, stashing handles on ``app.state`` so
    the lifespan shutdown can stop it cleanly.

    No-ops (and records ``None`` handles) when RUN_INGESTION is off or no
    MONGODB_URI is configured, so the API still serves reads normally.
    """
    app.state.ingestion_agent = None
    app.state.ingestion_storage = {}

    if not _env_flag("RUN_INGESTION"):
        log.info("RUN_INGESTION not set — skipping in-process ingestion")
        return

    mongo_uri = os.environ.get("MONGODB_URI")
    if not mongo_uri:
        log.warning("RUN_INGESTION set but MONGODB_URI is missing — ingestion disabled")
        return

    try:
        from IngestionModule import IngestionAgent
        from storage_handlers import attach_storage
        from sentiment import LoughranMcDonaldAnalyzer
    except Exception as exc:  # noqa: BLE001
        log.error("Ingestion deps unavailable (%s) — ingestion disabled", exc)
        return

    # LM scores structured items at write time. Relaxed poll intervals because
    # this shares one free-tier process with the API; tune via env vars.
    analyzer = LoughranMcDonaldAnalyzer()
    agent = IngestionAgent(
        rss_poll_interval=_env_float("RSS_POLL_INTERVAL", 120.0),
        sec_poll_interval=_env_float("SEC_POLL_INTERVAL", 600.0),
        fda_poll_interval=_env_float("FDA_POLL_INTERVAL", 600.0),
        enable_rss=True,
        enable_sec=True,
        enable_fda=True,
    )
    storage = attach_storage(
        agent,
        enable_mongo=True,
        mongo_kwargs={"uri": mongo_uri},
        analyzer=analyzer,
    )

    try:
        await agent.start()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to start ingestion agent: %s", exc)
        for h in storage.values():
            await h.close()
        return

    app.state.ingestion_agent = agent
    app.state.ingestion_storage = storage
    log.info("In-process ingestion started — RSS + SEC + FDA, LM sentiment")


async def stop_ingestion(app: Any) -> None:
    """Gracefully stop the agent and close its storage handlers."""
    agent = getattr(app.state, "ingestion_agent", None)
    if agent is not None:
        try:
            await agent.stop()
        except Exception as exc:  # noqa: BLE001
            log.error("Error stopping ingestion agent: %s", exc)
    for h in getattr(app.state, "ingestion_storage", {}).values():
        try:
            await h.close()
        except Exception as exc:  # noqa: BLE001
            log.error("Error closing storage handler: %s", exc)
