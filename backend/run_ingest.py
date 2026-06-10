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
    csv_path: str | None,
    rss_interval: float,
    sec_interval: float,
    fda_interval: float,
    analyzer_name: str | None,
    redis_url: str | None,
    mongo_uri: str | None,
) -> None:
    analyzer = _build_analyzer(analyzer_name) if analyzer_name else None

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
    use_mongo = bool(mongo_uri)
    use_redis = bool(redis_url)

    if use_mongo or use_redis:
        from IngestionModule import FILTER_KEYWORDS
        storage = attach_storage(
            agent,
            enable_mongo=use_mongo,
            enable_redis=use_redis,
            analyzer=analyzer,
            mongo_kwargs={"uri": mongo_uri} if use_mongo else None,
            redis_kwargs={
                "url": redis_url,
                "track_keywords": FILTER_KEYWORDS,
                "window_seconds": 3600,
            } if use_redis else None,
        )

    await agent.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await agent.stop()
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
        "--redis", metavar="URL", nargs="?", const="redis://localhost:6379/0",
        help=(
            "Store sentiment in Redis. Optionally pass a custom URL "
            "(default: redis://localhost:6379/0)."
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
    args = p.parse_args()

    any_specified = args.rss or args.sec or args.fda
    enable_rss = args.rss if any_specified else True
    enable_sec = args.sec if any_specified else True
    enable_fda = args.fda if any_specified else True

    asyncio.run(main(
        enable_rss, enable_sec, enable_fda,
        args.csv,
        args.rss_interval, args.sec_interval, args.fda_interval,
        args.sentiment,
        args.redis,
        args.mongo,
    ))
