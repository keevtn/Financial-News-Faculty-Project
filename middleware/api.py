"""
api.py
======
FastAPI middleware layer — bridges the Python ingestion backend with the
frontend dashboard.

This is a skeleton. Endpoints return placeholder responses until the backend
storage layer (MongoDB / Redis) is wired in via storage_handlers.py.

Run with:
    uvicorn middleware.api:app --reload --port 8000

Dependencies:
    pip install fastapi uvicorn[standard]
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

# Put the project root and backend/ on sys.path BEFORE importing the route
# modules. catalyst.py imports catalyst_ranker / market_calendar from backend/
# at module load, so the path must be ready first — regardless of the directory
# uvicorn is launched from (e.g. Render runs `uvicorn middleware.api:app`).
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_backend_dir = os.path.join(_project_root, "backend")
for _p in (_project_root, _backend_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from middleware.limiter import limiter
from middleware.routes import (
    news, sentiment, agent, tickers, catalyst, screener, squeeze, gossip, alerts, options, trends,
    validation,
)

log = logging.getLogger("middleware")

# Load .env from the project root so MONGODB_URI is available without
# the caller having to export it manually in their shell.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load FinBERT and open the MongoDB connection once at startup."""
    # LM keyword scorer (zero deps, instant — used for social/unstructured items)
    try:
        from backend.sentiment import LoughranMcDonaldAnalyzer
        app.state.lm_analyzer = LoughranMcDonaldAnalyzer()
        log.info("LoughranMcDonald analyzer ready")
    except Exception as exc:
        log.error("Failed to load LM analyzer: %s", exc)
        app.state.lm_analyzer = None

    # FinBERT (used for structured items only)
    try:
        from backend.sentiment import FinBERTAnalyzer
        analyzer = FinBERTAnalyzer()
        log.info("Loading FinBERT model — this may take a moment on first run...")
        analyzer._load()
        app.state.sentiment_analyzer = analyzer
        log.info("FinBERT ready")
    except Exception as exc:
        log.error("Failed to load FinBERT: %s", exc)
        app.state.sentiment_analyzer = None

    # MongoDB
    mongo_uri = os.environ.get("MONGODB_URI")
    if mongo_uri:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            client = AsyncIOMotorClient(mongo_uri, tz_aware=True)
            app.state.mongo_client = client
            app.state.news_collection = client["financial_news"]["news_items"]
            app.state.rankings_collection = client["financial_news"]["catalyst_rankings"]
            app.state.squeeze_collection = client["financial_news"]["squeeze_rankings"]
            app.state.universe_collection = client["financial_news"]["catalyst_universe"]
            app.state.catalyst_meta_collection = client["financial_news"]["catalyst_meta"]
            app.state.catalyst_calendar_collection = client["financial_news"]["catalyst_calendar"]
            log.info("MongoDB connected")
        except Exception as exc:
            log.error("Failed to connect to MongoDB: %s", exc)
            app.state.mongo_client = None
            app.state.news_collection = None
            app.state.rankings_collection = None
            app.state.squeeze_collection = None
            app.state.universe_collection = None
            app.state.catalyst_meta_collection = None
            app.state.catalyst_calendar_collection = None
    else:
        log.warning("MONGODB_URI not set — /api/news will return empty results")
        app.state.mongo_client = None
        app.state.news_collection = None
        app.state.rankings_collection = None
        app.state.squeeze_collection = None
        app.state.universe_collection = None
        app.state.catalyst_meta_collection = None
        app.state.catalyst_calendar_collection = None

    # In-process ingestion (only when RUN_INGESTION=true — see ingestion_runner).
    # Keeps the feeds fresh on the hosted deployment without a separate worker.
    try:
        from middleware.ingestion_runner import start_ingestion
        await start_ingestion(app)
    except Exception as exc:
        log.error("Failed to start in-process ingestion: %s", exc)

    # Catalyst scheduler (only when RUN_CATALYST_SCHEDULER=true). Auto-runs the
    # ranker pre-market and auto-grades closed runs to bank a track record.
    try:
        from middleware.catalyst_scheduler import start_catalyst_scheduler
        await start_catalyst_scheduler(app)
    except Exception as exc:
        log.error("Failed to start catalyst scheduler: %s", exc)

    # Squeeze scheduler (only when RUN_SQUEEZE_SCHEDULER=true). Own lane —
    # social-driven short-squeeze ranking, separate from the catalyst run.
    try:
        from middleware.squeeze_scheduler import start_squeeze_scheduler
        await start_squeeze_scheduler(app)
    except Exception as exc:
        log.error("Failed to start squeeze scheduler: %s", exc)

    # Catalyst universe scheduler (only when RUN_CATALYST_UNIVERSE_SCHEDULER=true).
    # Slow cadence (default 12h): accumulates sub-threshold candidate tickers and
    # optionally auto-tunes the pre-score weights. No LLM cost.
    try:
        from middleware.catalyst_universe_scheduler import start_catalyst_universe_scheduler
        await start_catalyst_universe_scheduler(app)
    except Exception as exc:
        log.error("Failed to start catalyst universe scheduler: %s", exc)

    yield

    try:
        from middleware.ingestion_runner import stop_ingestion
        await stop_ingestion(app)
    except Exception as exc:
        log.error("Failed to stop in-process ingestion: %s", exc)

    try:
        from middleware.catalyst_scheduler import stop_catalyst_scheduler
        await stop_catalyst_scheduler(app)
    except Exception as exc:
        log.error("Failed to stop catalyst scheduler: %s", exc)

    try:
        from middleware.squeeze_scheduler import stop_squeeze_scheduler
        await stop_squeeze_scheduler(app)
    except Exception as exc:
        log.error("Failed to stop squeeze scheduler: %s", exc)

    try:
        from middleware.catalyst_universe_scheduler import stop_catalyst_universe_scheduler
        await stop_catalyst_universe_scheduler(app)
    except Exception as exc:
        log.error("Failed to stop catalyst universe scheduler: %s", exc)

    if getattr(app.state, "mongo_client", None):
        app.state.mongo_client.close()


app = FastAPI(
    title="Financial News API",
    description="Middleware layer for the Financial News Dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Allow the Next.js dev server (localhost OR 127.0.0.1, any port) + Vercel prod.
# The exact-origin list missed 127.0.0.1 / non-3000 ports, which 400'd the
# sentiment-batch CORS preflight in local dev; the regex covers both dev hosts.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_origin_regex=r"https://.*\.vercel\.app|http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(news.router, prefix="/api/news", tags=["news"])
app.include_router(sentiment.router, prefix="/api/sentiment", tags=["sentiment"])
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
app.include_router(tickers.router, prefix="/api/tickers", tags=["tickers"])
app.include_router(catalyst.router, prefix="/api/catalyst", tags=["catalyst"])
app.include_router(screener.router, prefix="/api/screener", tags=["screener"])
app.include_router(squeeze.router, prefix="/api/squeeze", tags=["squeeze"])
app.include_router(gossip.router, prefix="/api/gossip", tags=["gossip"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["alerts"])
app.include_router(options.router, prefix="/api/options", tags=["options"])
app.include_router(trends.router, prefix="/api/trends", tags=["trends"])
app.include_router(validation.router, prefix="/api/validation", tags=["validation"])


@app.get("/health", tags=["meta"])
async def health_check() -> dict:
    return {"status": "ok", "version": "0.1.0"}
