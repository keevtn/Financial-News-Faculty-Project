"""
IngestionModule.py
==================
Real-time financial news ingestion agent.

Stage 1 — RSS Extraction
    Polls a configurable set of financial newswire RSS feeds on a defined
    interval and extracts: title, published time, and description for every
    new item.  Duplicate detection is handled via a content-hash cache so
    re-fetched feeds never emit the same article twice.

Stage 2 — SEC EDGAR Integration
    Polls the EDGAR full-text search / latest-filings RSS feed for 8-K, 10-K,
    10-Q, and S-1 filings.  Each item is enriched with filing type, accession
    number, and company name where available.

Stage 3 — FDA Integration
    Polls the openFDA drug-event and drug-enforcement endpoints (REST JSON)
    plus the official FDA news RSS feed.  Returns structured NewsItem objects
    alongside the raw FDA payload.

Architecture
------------
• Every source produces ``NewsItem`` dataclass objects.
• A shared ``asyncio.Queue`` receives all items from all sources.
• A ``DispatchRouter`` sits downstream of the queue and fans items out to
  registered handler callbacks (persist to DB, push to WebSocket, etc.).
• The ``IngestionAgent`` orchestrates lifecycle: start / stop / status.

Usage
-----
    import asyncio
    from IngestionModule import IngestionAgent, NewsItem

    async def my_handler(item: NewsItem) -> None:
        print(item)

    agent = IngestionAgent()
    agent.dispatcher.register(my_handler)

    asyncio.run(agent.run())          # blocks; Ctrl-C to stop
    # — or —
    asyncio.run(agent.start())        # non-blocking background tasks
    await asyncio.sleep(60)
    await agent.stop()

Dependencies
------------
    pip install aiohttp feedparser python-dateutil
"""

from __future__ import annotations

import asyncio
import calendar
import csv
import hashlib
import html
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

import aiohttp
import feedparser
from dateutil import parser as dateutil_parser

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ingestion_agent")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewsItem:
    """Normalised news item produced by every ingestion source."""

    source: str                          # human-readable source label
    source_type: str                     # "rss" | "sec" | "fda"
    title: str
    published_at: datetime               # always UTC-aware
    description: str
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    topic: str = ""                      # assigned by TopicClassifier at dispatch time
    tickers: tuple[str, ...] = field(default=(), hash=False, compare=False)

    # Stable identity hash for deduplication
    @property
    def content_hash(self) -> str:
        raw = f"{self.source}|{self.title}|{self.url}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def __str__(self) -> str:
        ts = self.published_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            f"[{self.source_type.upper()}] [{self.source}] {ts}\n"
            f"  Topic : {self.topic or 'Unclassified'}\n"
            f"  Title : {self.title}\n"
            f"  URL   : {self.url}\n"
            f"  Desc  : {self.description[:160]}{'…' if len(self.description) > 160 else ''}"
        )


# ---------------------------------------------------------------------------
# Duplicate-item cache
# ---------------------------------------------------------------------------

class _SeenCache:
    """Thread-safe (asyncio-safe) LRU-style seen-hash cache."""

    def __init__(self, maxsize: int = 50_000) -> None:
        self._cache: dict[str, float] = {}
        self._maxsize = maxsize

    def is_new(self, item: NewsItem) -> bool:
        h = item.content_hash
        if h in self._cache:
            return False
        if len(self._cache) >= self._maxsize:
            # asyncio is single-threaded, so dict insertion order == arrival order;
            # islice evicts the oldest 25% in O(n) without a sort.
            to_evict = list(itertools.islice(self._cache, self._maxsize // 4))
            for k in to_evict:
                del self._cache[k]
        self._cache[h] = time.monotonic()
        return True


# ---------------------------------------------------------------------------
# Keyword filter
# ---------------------------------------------------------------------------

# ┌─────────────────────────────────────────────────────────────────────────┐
# │  FILTER_KEYWORDS — edit this list to control what gets dispatched.      │
# │                                                                         │
# │  • Each string is matched case-insensitively against an item's title    │
# │    and description.  An item passes if ANY keyword matches.             │
# │  • Add new keywords anywhere in the list, or add a new comment-grouped  │
# │    section following the pattern below.                                 │
# │  • To disable filtering entirely, pass keywords=[] to IngestionAgent.   │
# └─────────────────────────────────────────────────────────────────────────┘
FILTER_KEYWORDS: list[str] = [
    # Macro / monetary policy
    "inflation", "interest rate", "federal reserve", "fed",
    # Equities & corporate events
    "earnings", "revenue", "stock", "ipo", "merger", "acquisition", "buyback",
    # Regulatory / legal
    "sec filing", "lawsuit", "settlement", "investigation",
    # FDA / pharma
    "fda", "recall", "drug approval", "clinical trial",
    # Crypto / digital assets
    "bitcoin", "crypto", "ethereum", "blockchain",
]


class KeywordFilter:
    """
    Accepts a NewsItem only if at least one keyword appears in its title or
    description (case-insensitive).  An empty keyword list disables filtering
    and passes every item through.
    """

    def __init__(self, keywords: list[str]) -> None:
        # Lowercase once at construction so per-item matching is cheap
        self._keywords = [kw.lower() for kw in keywords]

    def accepts(self, item: NewsItem) -> bool:
        if not self._keywords:
            return True
        haystack = (item.title + " " + item.description).lower()
        return any(kw in haystack for kw in self._keywords)


# ---------------------------------------------------------------------------
# Topic classifier
# ---------------------------------------------------------------------------

# ┌─────────────────────────────────────────────────────────────────────────┐
# │  TOPIC_KEYWORDS — edit this dict to add, remove, or rename topics.      │
# │                                                                          │
# │  • Each key is the topic label that will appear on the NewsItem.        │
# │  • Each value is a list of keywords (case-insensitive) for that topic.  │
# │  • Topics are checked in order — the first match wins.                  │
# │  • Items that match no topic are labelled "General".                    │
# └─────────────────────────────────────────────────────────────────────────┘
TOPIC_KEYWORDS: dict[str, list[str]] = {
    # Crypto / digital assets
    "Crypto": ["bitcoin", "ethereum", "crypto", "blockchain", "defi", "nft", "altcoin"],
    # Energy & commodities
    "Energy": ["oil", "gas", "opec", "crude", "energy", "renewable", "solar", "pipeline"],
    # Equities & corporate events
    "Equities": ["earnings", "ipo", "stock", "shares", "dividend", "buyback", "merger", "acquisition"],
    # Macro / monetary policy
    "Macro": ["inflation", "interest rate", "federal reserve", "fed", "gdp", "recession", "cpi"],
    # Regulatory / legal
    "Regulatory": ["sec", "fda", "recall", "enforcement", "lawsuit", "settlement", "investigation"],
    # Fixed income
    "Bonds": ["treasury", "yield", "bond", "debt", "credit rating", "sovereign"],
    # Commodities
    "Commodities": ["gold", "silver", "copper", "wheat", "corn", "commodity", "futures"],
    # Technology
    "Technology": ["ai", "semiconductor", "chip", "software", "cloud", "tech", "cybersecurity"],
}


class TopicClassifier:
    """
    Assigns a topic label to a NewsItem by matching its title and description
    against TOPIC_KEYWORDS.  The first matching topic wins.  Items that match
    nothing are labelled "General".
    """

    def __init__(self, topics: dict[str, list[str]] | None = None) -> None:
        src = topics if topics is not None else TOPIC_KEYWORDS
        # Pre-lowercase all keywords once at construction
        self._topics: list[tuple[str, list[str]]] = [
            (label, [kw.lower() for kw in keywords])
            for label, keywords in src.items()
        ]

    def classify(self, item: NewsItem) -> str:
        haystack = (item.title + " " + item.description).lower()
        matches = [
            label for label, keywords in self._topics
            if any(kw in haystack for kw in keywords)
        ]
        return ", ".join(matches) if matches else "General"


# ---------------------------------------------------------------------------
# Dispatch router
# ---------------------------------------------------------------------------

Handler = Callable[[NewsItem], Coroutine[Any, Any, None]]


class DispatchRouter:
    """Fan-out dispatcher: delivers each NewsItem to all registered handlers."""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def register(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def dispatch(self, item: NewsItem) -> None:
        for handler in self._handlers:
            try:
                await handler(item)
            except Exception as exc:  # noqa: BLE001
                log.error("Handler %s raised: %s", handler, exc)


# ---------------------------------------------------------------------------
# Shared HTTP session helper
# ---------------------------------------------------------------------------

class _HttpClient:
    """Thin wrapper around aiohttp.ClientSession with shared headers."""

    _DEFAULT_HEADERS = {
        "User-Agent": (
            "FinancialNewsDashboard/1.0 "
            "(+https://example.com; financial-data-bot)"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*",
    }

    def __init__(self, timeout: int = 15) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "_HttpClient":
        self._session = aiohttp.ClientSession(
            headers=self._DEFAULT_HEADERS,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()

    async def get_text(self, url: str) -> str:
        if self._session is None:
            raise RuntimeError("_HttpClient must be used as an async context manager")
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def get_json(self, url: str, params: dict | None = None) -> Any:
        if self._session is None:
            raise RuntimeError("_HttpClient must be used as an async context manager")
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


# ---------------------------------------------------------------------------
# Utility: parse dates robustly
# ---------------------------------------------------------------------------

def _parse_dt(raw: Any) -> datetime:
    """Return a UTC-aware datetime from a feedparser time-struct or string."""
    if raw is None:
        return datetime.now(tz=timezone.utc)
    # feedparser exposes parsed time as a time.struct_time
    if hasattr(raw, "tm_year"):
        ts = calendar.timegm(raw)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        dt = dateutil_parser.parse(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.now(tz=timezone.utc)


def _strip_html(text: str) -> str:
    """Strip HTML tags and decode HTML entities. Used to clean Reddit RSS descriptions."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Stage 1 — RSS Extractor
# ---------------------------------------------------------------------------

#: Default financial newswire RSS feeds
DEFAULT_RSS_FEEDS: list[dict[str, str]] = [
    # ── Major wires ──────────────────────────────────────────────────────────
    {
        "label": "Bloomberg Markets",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
    },
    {
        "label": "Financial Times",
        "url": "https://www.ft.com/rss/home",
    },
    {
        "label": "Wall Street Journal Markets",
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    },
    {
        "label": "CNBC Top News",
        "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    },
    {
        "label": "MarketWatch Top Stories",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
    },
    {
        "label": "Seeking Alpha Market News",
        "url": "https://seekingalpha.com/market_currents.xml",
    },
    # ── Macro / economic data ────────────────────────────────────────────────
    {
        "label": "Federal Reserve Press Releases",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
    },
    {
        "label": "BLS Economic News",
        "url": "https://www.bls.gov/feed/bls_latest.rss",
    },
    # ── Crypto / digital assets ──────────────────────────────────────────────
    {
        "label": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    },
    {
        "label": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
    },
    # ── Equities / analysis ──────────────────────────────────────────────────
    {
        "label": "Yahoo Finance",
        "url": "https://finance.yahoo.com/rss/topfinstories",
    },
    # ── Press release wires ──────────────────────────────────────────────────
    {
        "label": "PR Newswire",
        "url": "https://www.prnewswire.com/rss/news-releases-list.rss",
    },
    {
        "label": "Business Wire",
        "url": "https://feed.businesswire.com/rss/home/?rss=G1&rssid=1",
    },
    {
        "label": "Benzinga",
        "url": "https://www.benzinga.com/feed",
    },
    # ── Reddit social feeds (unauthenticated public RSS, /new for recency) ──
    # source_type="social" routes these to the Unstructured tab in the frontend.
    {
        "label": "Reddit - WallStreetBets",
        "url": "https://www.reddit.com/r/wallstreetbets/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - Investing",
        "url": "https://www.reddit.com/r/investing/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - Stocks",
        "url": "https://www.reddit.com/r/stocks/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - SecurityAnalysis",
        "url": "https://www.reddit.com/r/SecurityAnalysis/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - Economics",
        "url": "https://www.reddit.com/r/economics/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - EconMonitor",
        "url": "https://www.reddit.com/r/econmonitor/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - StockMarket",
        "url": "https://www.reddit.com/r/StockMarket/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - Options",
        "url": "https://www.reddit.com/r/options/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - AlgoTrading",
        "url": "https://www.reddit.com/r/algotrading/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - CryptoCurrency",
        "url": "https://www.reddit.com/r/CryptoCurrency/new/.rss",
        "source_type": "social",
    },
    {
        "label": "Reddit - Bitcoin",
        "url": "https://www.reddit.com/r/Bitcoin/new/.rss",
        "source_type": "social",
    },
]

# ---------------------------------------------------------------------------
# Unstructured source feed configs (used by future UnstructuredModule.py)
# ---------------------------------------------------------------------------

#: StockTwits ticker watchlist — polled via public symbol stream endpoint.
#: Crypto uses the .X suffix convention StockTwits requires.
STOCKTWITS_WATCHLIST: list[str] = [
    # Broad market ETFs
    "SPY", "QQQ", "DIA",
    # Mega-cap equities
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # Financials
    "JPM", "BAC",
    # Energy
    "XOM", "CVX",
    # Technology / semiconductors
    "AMD", "INTC",
    # Bonds / macro proxies
    "TLT", "GLD",
    # Crypto
    "BTC.X", "ETH.X",
    # High-sentiment / retail-watched
    "GME", "AMC", "PLTR",
]

#: Bluesky keyword/hashtag search terms — queried via public AT Protocol API.
BLUESKY_SEARCH_TERMS: list[str] = [
    # Equities & general market
    "#stocks", "#investing", "#stockmarket", "#wallstreetbets", "#earnings",
    "#trading", "#options", "#ipo", "#merger",
    # Macro
    "#economy", "#inflation", "#federalreserve", "#gdp", "#cpi",
    # Crypto
    "#crypto", "#bitcoin", "#ethereum", "#defi",
    # Commodities / energy
    "#gold", "#oil", "#commodities",
    # Bonds
    "#bonds", "#treasury",
    # Tech / sector
    "#fintech", "#semiconductor", "#ai",
]


class RSSExtractor:
    """
    Polls a list of RSS feeds on a configurable interval.

    For each new feed entry it emits a ``NewsItem`` with:
        • title       — entry title
        • published_at — parsed publication timestamp (UTC)
        • description  — summary / content snippet
    """

    def __init__(
        self,
        feeds: list[dict[str, str]] | None = None,
        poll_interval: float = 60.0,
        queue: asyncio.Queue | None = None,
        seen_cache: _SeenCache | None = None,
        max_age_minutes: float | None = None,
        social_feed_delay: float = 2.0,
    ) -> None:
        self.feeds = feeds or DEFAULT_RSS_FEEDS
        self.poll_interval = poll_interval
        self.queue: asyncio.Queue = queue or asyncio.Queue()
        self._seen = seen_cache or _SeenCache()
        # Drop items published more than this many minutes ago; None = no limit
        self.max_age_minutes = max_age_minutes
        # Seconds to wait between sequential social-feed requests (avoids Reddit 429s)
        self.social_feed_delay = social_feed_delay
        self._running = False

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_description(entry: Any) -> str:
        """Return the best available description text from a feed entry."""
        for attr in ("summary", "description", "content"):
            val = getattr(entry, attr, None)
            if val:
                # feedparser wraps 'content' in a list of dicts
                if isinstance(val, list):
                    val = " ".join(v.get("value", "") for v in val)
                return str(val).strip()
        return ""

    async def _poll_feed(
        self, feed_cfg: dict[str, str], http: _HttpClient
    ) -> list[NewsItem]:
        label = feed_cfg["label"]
        url = feed_cfg["url"]
        items: list[NewsItem] = []
        try:
            raw_xml = await http.get_text(url)
            parsed = feedparser.parse(raw_xml)
            for entry in parsed.entries:
                title = getattr(entry, "title", "").strip() or "(no title)"
                link = getattr(entry, "link", "")
                pub_raw = getattr(entry, "published_parsed", None) or getattr(
                    entry, "updated_parsed", None
                )
                published_at = _parse_dt(pub_raw)
                description = self._extract_description(entry)
                if feed_cfg.get("source_type") == "social":
                    description = _strip_html(description)

                # feed_cfg may override source_type (e.g. Reddit feeds use "social")
                item = NewsItem(
                    source=label,
                    source_type=feed_cfg.get("source_type", "rss"),
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=link,
                )
                if self.max_age_minutes is not None:
                    age = (datetime.now(tz=timezone.utc) - published_at).total_seconds() / 60
                    if age > self.max_age_minutes:
                        continue
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("RSS poll failed [%s]: %s", label, exc)
        return items

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Run forever, polling all feeds every ``poll_interval`` seconds."""
        self._running = True
        standard = [f for f in self.feeds if f.get("source_type", "rss") != "social"]
        social = [f for f in self.feeds if f.get("source_type") == "social"]
        log.info(
            "RSSExtractor started — %d standard + %d social feeds, interval=%ss",
            len(standard), len(social), self.poll_interval,
        )
        async with _HttpClient() as http:
            while self._running:
                start = asyncio.get_running_loop().time()
                total = 0

                # Standard RSS: fire all concurrently (newswires tolerate it)
                if standard:
                    results = await asyncio.gather(
                        *[self._poll_feed(f, http) for f in standard],
                        return_exceptions=True,
                    )
                    for batch in results:
                        if isinstance(batch, list):
                            for item in batch:
                                await self.queue.put(item)
                                total += 1

                # Social RSS (Reddit etc.): sequential + delay to avoid burst 429s
                for feed in social:
                    if not self._running:
                        break
                    for item in await self._poll_feed(feed, http):
                        await self.queue.put(item)
                        total += 1
                    await asyncio.sleep(self.social_feed_delay)

                log.info("RSSExtractor — cycle complete, %d new items", total)
                elapsed = asyncio.get_running_loop().time() - start
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Stage 2 — SEC EDGAR Extractor
# ---------------------------------------------------------------------------

#: EDGAR latest-filings RSS (covers all filing types)
_EDGAR_RSS_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={filing_type}&dateb=&owner=include&count=40&search_text=&output=atom"

#: Filing types of primary interest to financial-news dashboards
DEFAULT_SEC_FILING_TYPES: list[str] = ["8-K", "10-K", "10-Q", "S-1", "6-K"]


class SECExtractor:
    """
    Polls SEC EDGAR for new regulatory filings.

    Each filing is normalised into a ``NewsItem`` (source_type="sec") with
    the filing type, accession number, and filer name embedded in
    ``extra``.
    """

    def __init__(
        self,
        filing_types: list[str] | None = None,
        poll_interval: float = 300.0,   # EDGAR asks for ≥ 10 s between requests
        queue: asyncio.Queue | None = None,
        seen_cache: _SeenCache | None = None,
    ) -> None:
        self.filing_types = filing_types or DEFAULT_SEC_FILING_TYPES
        self.poll_interval = poll_interval
        self.queue: asyncio.Queue = queue or asyncio.Queue()
        self._seen = seen_cache or _SeenCache()
        self._running = False

    async def _poll_filing_type(
        self, filing_type: str, http: _HttpClient
    ) -> list[NewsItem]:
        url = _EDGAR_RSS_URL.format(filing_type=filing_type)
        items: list[NewsItem] = []
        try:
            raw_xml = await http.get_text(url)
            parsed = feedparser.parse(raw_xml)
            for entry in parsed.entries:
                title = getattr(entry, "title", "").strip() or f"SEC {filing_type}"
                link = getattr(entry, "link", "")
                pub_raw = getattr(entry, "published_parsed", None) or getattr(
                    entry, "updated_parsed", None
                )
                published_at = _parse_dt(pub_raw)

                # EDGAR Atom feeds bury structured data in <content>
                description = (
                    getattr(entry, "summary", None)
                    or getattr(entry, "description", None)
                    or ""
                ).strip()

                # Extract accession number from URL (…/Archives/edgar/data/<cik>/<acc>-index.htm)
                accession = ""
                if link:
                    parts = link.rstrip("/").split("/")
                    # accession number typically penultimate segment
                    for part in reversed(parts):
                        if part.count("-") == 2 and len(part) == 20:
                            accession = part
                            break

                item = NewsItem(
                    source=f"SEC EDGAR — {filing_type}",
                    source_type="sec",
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=link,
                    extra={
                        "filing_type": filing_type,
                        "accession_number": accession,
                    },
                )
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("SEC poll failed [%s]: %s", filing_type, exc)
        return items

    async def run(self) -> None:
        self._running = True
        log.info("SECExtractor started — types=%s, interval=%ss",
                 self.filing_types, self.poll_interval)
        async with _HttpClient(timeout=20) as http:
            while self._running:
                start = asyncio.get_running_loop().time()
                # Stagger requests to respect EDGAR rate limits
                for filing_type in self.filing_types:
                    if not self._running:
                        break
                    items = await self._poll_filing_type(filing_type, http)
                    for item in items:
                        await self.queue.put(item)
                    await asyncio.sleep(1.0)   # EDGAR courtesy delay
                elapsed = asyncio.get_running_loop().time() - start
                log.info("SECExtractor — cycle complete")
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Stage 3 — FDA Extractor
# ---------------------------------------------------------------------------

_FDA_NEWS_RSS = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
_FDA_DRUG_ENFORCEMENT_URL = (
    "https://api.fda.gov/drug/enforcement.json"
    "?sort=report_date:desc&limit=20"
)
_FDA_DRUG_EVENT_URL = (
    "https://api.fda.gov/drug/event.json"
    "?sort=receivedate:desc&limit=10"
)


class FDAExtractor:
    """
    Collects FDA news from two complementary sources:

    1. **FDA Press-Release RSS** — official announcements, drug approvals,
       safety communications.
    2. **openFDA Drug Enforcement** — recent drug recalls / enforcement
       actions (REST JSON, ``/drug/enforcement.json``).

    Both are normalised into ``NewsItem`` objects (source_type="fda").
    """

    def __init__(
        self,
        poll_interval: float = 180.0,
        queue: asyncio.Queue | None = None,
        seen_cache: _SeenCache | None = None,
        include_drug_events: bool = False,  # high-volume; off by default
    ) -> None:
        self.poll_interval = poll_interval
        self.queue: asyncio.Queue = queue or asyncio.Queue()
        self._seen = seen_cache or _SeenCache()
        self.include_drug_events = include_drug_events
        self._running = False

    # ── RSS press releases ────────────────────────────────────────────── #

    async def _poll_press_releases(self, http: _HttpClient) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            raw_xml = await http.get_text(_FDA_NEWS_RSS)
            parsed = feedparser.parse(raw_xml)
            for entry in parsed.entries:
                title = getattr(entry, "title", "").strip() or "FDA Press Release"
                link = getattr(entry, "link", "")
                pub_raw = getattr(entry, "published_parsed", None)
                published_at = _parse_dt(pub_raw)
                description = (
                    getattr(entry, "summary", "")
                    or getattr(entry, "description", "")
                ).strip()

                item = NewsItem(
                    source="FDA Press Releases",
                    source_type="fda",
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=link,
                )
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("FDA RSS poll failed: %s", exc)
        return items

    # ── openFDA drug enforcement (REST JSON) ─────────────────────────── #

    async def _poll_enforcement(self, http: _HttpClient) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            data = await http.get_json(_FDA_DRUG_ENFORCEMENT_URL)
            for result in data.get("results", []):
                product = result.get("product_description", "Unknown product")
                firm = result.get("recalling_firm", "Unknown firm")
                reason = result.get("reason_for_recall", "")
                recall_class = result.get("classification", "")
                status = result.get("status", "")
                report_date_raw = result.get("report_date", "")
                recall_number = result.get("recall_number", "")
                voluntary_mandated = result.get("voluntary_mandated", "")

                title = f"[{recall_class}] {firm} — {product[:80]}"
                description = (
                    f"Reason: {reason} | "
                    f"Status: {status} | "
                    f"Class: {recall_class} | "
                    f"Voluntary/Mandated: {voluntary_mandated}"
                )
                published_at = _parse_dt(report_date_raw)

                item = NewsItem(
                    source="FDA Drug Enforcement",
                    source_type="fda",
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=f"https://www.accessdata.fda.gov/scripts/enforcement/enforce_rpt-Product-Tabs.cfm?action=select&recall_number={recall_number}",
                    extra={
                        "recall_number": recall_number,
                        "classification": recall_class,
                        "status": status,
                        "recalling_firm": firm,
                    },
                )
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("FDA Enforcement poll failed: %s", exc)
        return items

    # ── openFDA adverse drug events (optional, high volume) ──────────── #

    async def _poll_drug_events(self, http: _HttpClient) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            data = await http.get_json(_FDA_DRUG_EVENT_URL)
            for result in data.get("results", []):
                receive_date = result.get("receivedate", "")
                report_id = result.get("safetyreportid", "")
                serious = result.get("serious", 1)
                reactions = [
                    r.get("reactionmeddrapt", "")
                    for r in result.get("patient", {}).get("reaction", [])[:5]
                ]
                drugs = [
                    d.get("medicinalproduct", "")
                    for d in result.get("patient", {}).get("drug", [])[:3]
                ]
                title = (
                    f"Adverse Event [{report_id}] — "
                    f"{', '.join(d for d in drugs if d)[:80]}"
                )
                description = (
                    f"Reactions: {', '.join(r for r in reactions if r)} | "
                    f"Serious: {'Yes' if serious else 'No'}"
                )
                published_at = _parse_dt(receive_date)

                item = NewsItem(
                    source="FDA Adverse Events",
                    source_type="fda",
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=f"https://www.accessdata.fda.gov/scripts/cder/daf/",
                    extra={
                        "report_id": report_id,
                        "serious": bool(serious),
                        "drugs": drugs,
                        "reactions": reactions,
                    },
                )
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("FDA Drug Events poll failed: %s", exc)
        return items

    # ── Lifecycle ────────────────────────────────────────────────────── #

    async def run(self) -> None:
        self._running = True
        log.info("FDAExtractor started — interval=%ss", self.poll_interval)
        async with _HttpClient(timeout=20) as http:
            while self._running:
                start = asyncio.get_running_loop().time()
                batches = await asyncio.gather(
                    self._poll_press_releases(http),
                    self._poll_enforcement(http),
                    *(
                        [self._poll_drug_events(http)]
                        if self.include_drug_events
                        else []
                    ),
                    return_exceptions=True,
                )
                total = 0
                for batch in batches:
                    if isinstance(batch, list):
                        for item in batch:
                            await self.queue.put(item)
                            total += 1
                log.info("FDAExtractor — cycle complete, %d new items", total)
                elapsed = asyncio.get_running_loop().time() - start
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Ingestion Agent — top-level orchestrator
# ---------------------------------------------------------------------------

class IngestionAgent:
    """
    Orchestrates all extractors and routes items to registered handlers.

    Parameters
    ----------
    rss_feeds:
        Override the default RSS feed list (see ``DEFAULT_RSS_FEEDS``).
    rss_poll_interval:
        Seconds between RSS polling cycles. Default 60 s.
    sec_filing_types:
        Filing types to watch on EDGAR. Default: 8-K, 10-K, 10-Q, S-1, 6-K.
    sec_poll_interval:
        Seconds between SEC EDGAR cycles. Default 300 s.
    fda_poll_interval:
        Seconds between FDA cycles. Default 180 s.
    fda_include_drug_events:
        Enable high-volume adverse-event polling. Default False.
    queue_maxsize:
        Maximum items buffered in the internal queue. 0 = unbounded.

    Example
    -------
    ::

        agent = IngestionAgent(rss_poll_interval=30)
        agent.dispatcher.register(my_async_handler)
        asyncio.run(agent.run())
    """

    def __init__(
        self,
        rss_feeds: list[dict[str, str]] | None = None,
        rss_poll_interval: float = 60.0,
        sec_filing_types: list[str] | None = None,
        sec_poll_interval: float = 300.0,
        fda_poll_interval: float = 180.0,
        fda_include_drug_events: bool = False,
        queue_maxsize: int = 0,
        enable_rss: bool = True,
        enable_sec: bool = True,
        enable_fda: bool = True,
        keywords: list[str] | None = None,
        rss_max_age_minutes: float | None = None,
    ) -> None:
        self._queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=queue_maxsize)
        self._seen = _SeenCache()
        self.dispatcher = DispatchRouter()
        # Use FILTER_KEYWORDS by default; pass keywords=[] to disable filtering
        self._filter = KeywordFilter(keywords if keywords is not None else FILTER_KEYWORDS)
        self._classifier = TopicClassifier()
        from ticker_extractor import TickerExtractor
        self._ticker_extractor = TickerExtractor()

        self.enable_rss = enable_rss
        self.enable_sec = enable_sec
        self.enable_fda = enable_fda

        self.rss = RSSExtractor(
            feeds=rss_feeds,
            poll_interval=rss_poll_interval,
            queue=self._queue,
            seen_cache=self._seen,
            max_age_minutes=rss_max_age_minutes,
        ) if enable_rss else None
        self.sec = SECExtractor(
            filing_types=sec_filing_types,
            poll_interval=sec_poll_interval,
            queue=self._queue,
            seen_cache=self._seen,
        ) if enable_sec else None
        self.fda = FDAExtractor(
            poll_interval=fda_poll_interval,
            queue=self._queue,
            seen_cache=self._seen,
            include_drug_events=fda_include_drug_events,
        ) if enable_fda else None

        self._tasks: list[asyncio.Task] = []
        self._dispatch_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    #  Dispatch loop                                                       #
    # ------------------------------------------------------------------ #

    async def _dispatch_loop(self) -> None:
        """Drain the shared queue, apply keyword filter, classify topic, and fan-out to handlers."""
        while True:
            item = await self._queue.get()
            # SEC and FDA items are inherently financial — skip keyword gating so
            # no filing or enforcement notice is silently dropped just because
            # "Apple" or "Pfizer" don't appear in FILTER_KEYWORDS.
            is_regulatory = item.source_type in ("sec", "fda")
            if is_regulatory or self._filter.accepts(item):
                item = replace(
                    item,
                    topic=self._classifier.classify(item),
                    tickers=self._ticker_extractor.extract(item.title, item.description),
                )
                await self.dispatcher.dispatch(item)
            else:
                log.debug("Filtered out: [%s] %s", item.source_type, item.title[:80])
            self._queue.task_done()

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Launch enabled extractors and the dispatch loop as background tasks."""
        loop = asyncio.get_running_loop()
        self._tasks = []
        if self.rss is not None:
            self._tasks.append(loop.create_task(self.rss.run(), name="rss_extractor"))
        if self.sec is not None:
            self._tasks.append(loop.create_task(self.sec.run(), name="sec_extractor"))
        if self.fda is not None:
            self._tasks.append(loop.create_task(self.fda.run(), name="fda_extractor"))
        self._dispatch_task = loop.create_task(
            self._dispatch_loop(), name="dispatch_loop"
        )
        enabled = [
            s for s, on in (("rss", self.enable_rss), ("sec", self.enable_sec), ("fda", self.enable_fda)) if on
        ]
        log.info("IngestionAgent started — sources: %s (%d tasks)", enabled, len(self._tasks))

    async def stop(self) -> None:
        """Gracefully stop all enabled extractors and flush remaining items."""
        if self.rss is not None:
            self.rss.stop()
        if self.sec is not None:
            self.sec.stop()
        if self.fda is not None:
            self.fda.stop()
        for task in self._tasks:
            task.cancel()
        if self._dispatch_task:
            self._dispatch_task.cancel()
        await asyncio.gather(*self._tasks, self._dispatch_task or asyncio.sleep(0), return_exceptions=True)
        log.info("IngestionAgent stopped")

    async def run(self) -> None:
        """
        Start the agent and block until interrupted.

        Catches ``KeyboardInterrupt`` / ``CancelledError`` and shuts down
        cleanly.
        """
        await self.start()
        try:
            # Include the dispatch task so all long-running tasks are awaited
            # together; any one finishing (or raising) triggers the finally block.
            all_tasks = [
                *self._tasks,
                *([self._dispatch_task] if self._dispatch_task else []),
            ]
            await asyncio.gather(*all_tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Shutdown signal received")
        finally:
            await self.stop()

    # ------------------------------------------------------------------ #
    #  Status                                                              #
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        """Return a dict summarising the agent's current state."""
        def task_state(t: asyncio.Task | None) -> str:
            if t is None:
                return "not_started"
            if t.done():
                return "done" if not t.cancelled() else "cancelled"
            return "running"

        def source_state(name: str, enabled: bool) -> str:
            if not enabled:
                return "disabled"
            return task_state(next((t for t in self._tasks if t.get_name() == name), None))

        return {
            "queue_size": self._queue.qsize(),
            "seen_cache_size": len(self._seen._cache),
            "rss_task": source_state("rss_extractor", self.enable_rss),
            "sec_task": source_state("sec_extractor", self.enable_sec),
            "fda_task": source_state("fda_extractor", self.enable_fda),
            "dispatch_task": task_state(self._dispatch_task),
            "registered_handlers": len(self.dispatcher._handlers),
        }


# ---------------------------------------------------------------------------
# CSV export handler
# ---------------------------------------------------------------------------

# Column order written to every CSV file produced by CSVHandler
_CSV_COLUMNS = ["source", "source_type", "title", "published_at", "url", "description", "extra"]


class CSVHandler:
    """
    Async-compatible handler that appends each dispatched NewsItem as a row
    in a CSV file.

    Parameters
    ----------
    path:
        Destination file path.  Created on first write; appended to on restart.
    enabled:
        Set to False to make the handler a no-op without unregistering it.
        Useful for toggling CSV output without restarting the agent.

    Usage::

        handler = CSVHandler("output.csv", enabled=True)
        agent.dispatcher.register(handler)
    """

    def __init__(self, path: str, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        # Write the header only when creating a brand-new file
        self._write_header = not os.path.exists(path)

    async def __call__(self, item: NewsItem) -> None:
        # Short-circuit immediately when disabled — zero overhead
        if not self.enabled:
            return

        # File I/O is synchronous but fast for single-row appends; wrapping in
        # an executor would add overhead that isn't justified at this volume.
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
            if self._write_header:
                writer.writeheader()
                self._write_header = False
            writer.writerow({
                "source":       item.source,
                "source_type":  item.source_type,
                "title":        item.title,
                "published_at": item.published_at.isoformat(),
                "url":          item.url,
                "description":  item.description,
                "extra":        json.dumps(item.extra) if item.extra else "",
            })


# ---------------------------------------------------------------------------
# Default handler (stdout logging) — useful for dev/testing
# ---------------------------------------------------------------------------

async def log_handler(item: NewsItem) -> None:
    """Print every item to stdout; replace or supplement with your own handler."""
    print(item)
    print()


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Financial news ingestion agent")
    p.add_argument("--rss", action="store_true", default=False, help="Enable RSS feeds")
    p.add_argument("--sec", action="store_true", default=False, help="Enable SEC EDGAR filings")
    p.add_argument("--fda", action="store_true", default=False, help="Enable FDA news & enforcement")
    args = p.parse_args()

    # If none specified, default to all enabled
    any_specified = args.rss or args.sec or args.fda
    enable_rss = args.rss if any_specified else True
    enable_sec = args.sec if any_specified else True
    enable_fda = args.fda if any_specified else True

    agent = IngestionAgent(
        rss_poll_interval=60,
        sec_poll_interval=300,
        fda_poll_interval=180,
        enable_rss=enable_rss,
        enable_sec=enable_sec,
        enable_fda=enable_fda,
    )
    agent.dispatcher.register(log_handler)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        sys.exit(0)