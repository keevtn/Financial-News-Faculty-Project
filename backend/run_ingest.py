import argparse
import asyncio
import os

# Load .env from the project root if python-dotenv is available;
# otherwise fall back to whatever is already in the environment.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

from IngestionModule import CSVHandler, IngestionAgent, NewsItem
from UnstructuredModule import UnstructuredAgent
from storage_handlers import attach_storage


def _build_handler(analyzer=None):
    """Return a print handler that optionally appends a sentiment line."""
    async def handler(item: NewsItem) -> None:
        print(item)
        if analyzer is not None:
            result = await asyncio.to_thread(analyzer.analyze, item)
            label_icon = {"bullish": "▲", "bearish": "▼", "neutral": "◆"}.get(result.label, "?")
            print(
                f"  Sentiment : {label_icon} {result.label.upper()}"
                f"  score={result.score:+.4f}"
                f"  confidence={result.confidence:.2f}"
            )
        print("-" * 80)
    return handler


def _build_analyzer(name: str):
    """Instantiate the requested SentimentAnalyzer by short name."""
    if name == "lm":
        from sentiment import LoughranMcDonaldAnalyzer
        return LoughranMcDonaldAnalyzer()
    if name == "finbert":
        from sentiment import FinBERTAnalyzer
        return FinBERTAnalyzer()
    raise ValueError(f"Unknown analyzer: {name!r}")


async def main(
    enable_rss: bool,
    enable_sec: bool,
    enable_fda: bool,
    enable_stocktwits: bool,
    enable_bluesky: bool,
    csv_path: str | None,
    rss_interval: float,
    sec_interval: float,
    fda_interval: float,
    analyzer_name: str | None,
    mongo_uri: str | None,
    redis_url: str | None,
) -> None:
    analyzer = _build_analyzer(analyzer_name) if analyzer_name else None

    # --- Structured ingestion (RSS newswires, SEC, FDA) ---
    agent = IngestionAgent(
        rss_poll_interval=rss_interval,
        sec_poll_interval=sec_interval,
        fda_poll_interval=fda_interval,
        enable_rss=enable_rss,
        enable_sec=enable_sec,
        enable_fda=enable_fda,
    )
    agent.dispatcher.register(_build_handler(analyzer))

    if csv_path:
        agent.dispatcher.register(CSVHandler(csv_path, enabled=True))

    storage = {}
    if mongo_uri or redis_url:
        from IngestionModule import FILTER_KEYWORDS
        storage = attach_storage(
            agent,
            enable_mongo=bool(mongo_uri),
            mongo_kwargs={"uri": mongo_uri} if mongo_uri else None,
            enable_redis=bool(redis_url),
            analyzer=analyzer,
            redis_kwargs={
                "url": redis_url,
                "track_keywords": FILTER_KEYWORDS,
                "window_seconds": 3600,
            } if redis_url else None,
        )

    # --- Unstructured ingestion (StockTwits, Bluesky) ---
    # Shares the same DispatchRouter so social posts flow through the same
    # handlers (MongoDB, print, CSV) without duplicating wiring.
    social_agent = UnstructuredAgent(
        dispatcher=agent.dispatcher,
        keywords=[],  # match structured agent's keyword filter setting
        enable_stocktwits=enable_stocktwits,
        enable_bluesky=enable_bluesky,
    )

    await agent.start()
    if enable_stocktwits or enable_bluesky:
        await social_agent.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await agent.stop()
        await social_agent.stop()
    finally:
        for h in storage.values():
            await h.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Run the financial news ingestion agent")
    p.add_argument("--rss", action="store_true", default=False, help="Enable RSS feeds")
    p.add_argument("--sec", action="store_true", default=False, help="Enable SEC EDGAR filings")
    p.add_argument("--fda", action="store_true", default=False, help="Enable FDA news & enforcement")
    p.add_argument("--csv", metavar="FILE", default=None, help="Write dispatched items to a CSV file")
    p.add_argument("--rss-interval", type=float, default=60.0, metavar="SECS",
                   help="RSS poll interval in seconds (default: 60)")
    p.add_argument("--sec-interval", type=float, default=300.0, metavar="SECS",
                   help="SEC EDGAR poll interval in seconds (default: 300)")
    p.add_argument("--fda-interval", type=float, default=180.0, metavar="SECS",
                   help="FDA poll interval in seconds (default: 180)")
    p.add_argument(
        "--sentiment", metavar="ANALYZER", nargs="?", const="lm",
        choices=["lm", "finbert"],
        help=(
            "Attach sentiment scoring to each item. "
            "Choices: lm (Loughran-McDonald, default), finbert. "
            "Omit to disable sentiment output."
        ),
    )
    p.add_argument(
        "--redis", metavar="URL", nargs="?", const=os.environ.get("REDIS_URL"),
        default=os.environ.get("REDIS_URL"),
        help=(
            "Store rolling sentiment in Redis. Reads REDIS_URL from .env by default. "
            "Pass a URL explicitly to override."
        ),
    )
    p.add_argument(
        "--mongo", metavar="URI", nargs="?", const=os.environ.get("MONGODB_URI"),
        default=os.environ.get("MONGODB_URI"),
        help=(
            "Archive items to MongoDB. Reads MONGODB_URI from .env by default. "
            "Pass a URI explicitly to override."
        ),
    )
    p.add_argument("--stocktwits", action="store_true", default=False,
                   help="Enable StockTwits social feed (22-ticker watchlist)")
    p.add_argument("--bluesky", action="store_true", default=False,
                   help="Enable Bluesky social search (27 financial hashtags)")
    args = p.parse_args()

    # Structured: if none of --rss/--sec/--fda given, enable all three
    any_structured = args.rss or args.sec or args.fda
    enable_rss = args.rss if any_structured else True
    enable_sec = args.sec if any_structured else True
    enable_fda = args.fda if any_structured else True

    # Social: opt-in only (no credentials needed but adds traffic)
    enable_stocktwits = args.stocktwits
    enable_bluesky = args.bluesky

    asyncio.run(main(
        enable_rss, enable_sec, enable_fda,
        enable_stocktwits, enable_bluesky,
        args.csv,
        args.rss_interval, args.sec_interval, args.fda_interval,
        args.sentiment,
        args.mongo,
        args.redis,
    ))
