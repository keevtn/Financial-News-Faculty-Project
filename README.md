# Financial News Faculty Project

A real-time financial news monitoring platform that ingests headlines from multiple sources, classifies sentiment using a finance-tuned transformer model, and displays results in a live dashboard.

> **Academic research project — not a commercial product.** For educational /
> research use only. **Not investment advice.** Market data is **delayed** and
> obtained via third-party APIs. See [Data Licensing & Compliance](#data-licensing--compliance).

---

## Architecture

The project is split into three layers that run as independent processes:

```
backend/   — Python ingestion pipeline (data collection + sentiment)
middleware/ — FastAPI REST layer (serves data to the frontend)
frontend/  — Next.js dashboard (real-time display)
```

MongoDB Atlas stores all ingested articles. The frontend fetches from MongoDB via the middleware and scores each article's sentiment with FinBERT on page load.

---

## Core Features

### Backend — Ingestion Pipeline

#### Structured sources
- **RSS Extractor** — polls 14 financial newswires (Bloomberg, CNBC, WSJ, FT, MarketWatch, CoinDesk, Yahoo Finance, Federal Reserve, BLS, and others) on a configurable interval (default 60 s)
- **SEC EDGAR Extractor** — polls EDGAR for 8-K, 10-K, 10-Q, S-1, and 6-K filings (default 300 s)
- **FDA Extractor** — collects FDA press releases and drug enforcement/recall actions from the openFDA API (default 180 s)

#### Unstructured / social sources (opt-in via `--stocktwits` / `--bluesky`)
- **Reddit RSS** — 11 subreddits (r/wallstreetbets, r/investing, r/stocks, r/CryptoCurrency, and others) polled sequentially with a 2 s delay to avoid rate limits; items tagged `source_type="social"`
- **StockTwits** — public symbol stream for a 22-ticker watchlist (SPY, QQQ, AAPL, MSFT, NVDA, BTC.X, ETH.X, and others); human Bullish/Bearish labels are preserved as `extra.st_sentiment`
- **Bluesky** — AT Protocol public search across 27 financial hashtags (#stocks, #inflation, #bitcoin, #federalreserve, and others)

#### Shared pipeline features
- **Deduplication** — SHA-256 content hash cache prevents the same article from being dispatched twice across restarts
- **Keyword filter** — items are only dispatched if they match a configurable list of financial keywords
- **Topic classifier** — assigns a topic label (Crypto, Energy, Equities, Macro, Regulatory, Bonds, Commodities, Technology) based on keyword matching
- **CSV export** — optional handler writes every dispatched item to a CSV file

### Sentiment Analysis

- **FinBERT** (`ProsusAI/finbert`) — primary analyzer for structured sources; BERT fine-tuned on ~10k financial sentences. Outputs a continuous score P(positive) − P(negative) ∈ [−1, 1] and a bullish / bearish / neutral label
- **Social fast-path** — social items never block on FinBERT; instead:
  1. StockTwits human label is used directly if present (zero latency)
  2. Loughran-McDonald keyword scorer is used as fallback (~1 ms, no GPU)
- **Social ticker tagging** — the same `TickerExtractor` used by the structured pipeline runs on every social item; StockTwits items additionally inherit the API-resolved symbol list from `extra["symbols"]` so the watched ticker is always tagged even when not mentioned by name
- **Loughran-McDonald lexicon** — lightweight fallback (~150 built-in terms, no ML model required); supports loading the full ~3,500-word master dictionary CSV
- All analyzers share a common `SentimentAnalyzer` protocol and return a `SentimentResult(score, label, confidence)`

### Storage

- **MongoDB** (`MongoHandler`) — durable archive of every ingested article; idempotent upsert on `content_hash` so re-seen articles are never duplicated. Indexed on `content_hash`, `published_at`, and `source_type`. Sentiment is scored and stored at ingestion time so subsequent fetches are instant
- **Redis** (`RedisHandler`) — rolling time-windowed sentiment store; maintains per-scope sorted sets (global, per source type, per source, per keyword) for "what is the mood about X right now?" queries

### Middleware — FastAPI

- `GET  /api/news` — paginated article feed from MongoDB, filterable by source type, topic, and full-text search
- `GET  /api/news/topics` — aggregated topic list with article counts
- `GET  /api/news/sources` — all known sources and their types
- `POST /api/sentiment/batch` — scores a batch of articles with FinBERT; returns score, label, and confidence per item (max 100 items per request)
- `GET  /api/sentiment/` — aggregated sentiment statistics (wired to Redis when available)
- `GET  /health` — liveness check
- FinBERT model is loaded eagerly at startup and held in `app.state` so the first request is not slow
- MongoDB connection opened at startup via `app.state`; gracefully degrades to empty responses if unavailable

### Security & Rate Limiting

- No credentials in source code — all secrets via `.env` / environment variables
- `.env` excluded from git via `.gitignore`
- CORS restricted to `http://localhost:3000`
- User-supplied search strings escaped with `re.escape` before use in MongoDB regex queries (ReDoS prevention)
- Per-IP rate limits enforced via `slowapi`:
  - `POST /api/sentiment/batch` — 20 requests/minute
  - `GET /api/news` — 60 requests/minute
  - `GET /api/sentiment/` — 60 requests/minute
  - `GET /api/news/topics`, `/api/news/sources` — 30 requests/minute

### Frontend — Next.js Dashboard

- Fetches live articles from MongoDB via the middleware on page load; falls back to mock data if the API is unavailable
- **Structured tab** — filtered view of RSS, SEC, and FDA articles
  - FinBERT sentiment scored client-side via `POST /api/sentiment/batch` on mount; cards show a pulsing skeleton while scoring is in progress
  - Sentiment badge displays label and numeric score (e.g. `▲ BULLISH +0.87`)
  - Filter sidebar — sort by sentiment score, filter by source type, topic, sentiment label, ticker (with article counts), and free-text search; configurable article limit (1–500, default 100)
  - Stats bar showing article counts per source type and sentiment breakdown
- **Unstructured tab** — social feed view for Reddit, StockTwits, and Bluesky posts with a full filter sidebar mirroring the structured tab: search, sort by sentiment score, platform filter (Reddit / StockTwits / Bluesky), sentiment filter (with "only" shortcut), ticker filter with mention counts and clear button, and configurable article limit (1–500)
- Source badges: RSS (blue), SEC (emerald), FDA (rose), SOCIAL (violet)

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- MongoDB Atlas account (free M0 tier is sufficient)

### Install dependencies

```bash
# Backend
pip install -r backend/requirements.txt

# Middleware
pip install -r middleware/requirements.txt

# Frontend
cd frontend && npm install
```

### Configure environment

Create a `.env` file in the project root:

```
MONGODB_URI=mongodb+srv://<user>:<password>@cluster.xxxxx.mongodb.net/?retryWrites=true&w=majority
REDIS_URL=redis://default:<password>@<host>:<port>
```

### Run everything

```powershell
.\start.ps1              # structured only (RSS, SEC, FDA)
.\start.ps1 -Social      # structured + social (Reddit RSS, StockTwits, Bluesky)
.\start.ps1 -RssOnly     # RSS only
.\start.ps1 -NoIngest    # middleware + frontend only (no ingestion)
```

This opens three terminal windows:

| Window | Service | URL |
|---|---|---|
| Middleware | FastAPI + FinBERT | http://localhost:8000/docs |
| Frontend | Next.js dashboard | http://localhost:3000 |
| Ingestion | pipeline | writes to MongoDB |

Or start services individually:

```bash
# Middleware (from project root)
uvicorn middleware.api:app --reload --port 8000

# Frontend
cd frontend && npm run dev

# Ingestion (from backend/)
python run_ingest.py --rss --sec --fda
python run_ingest.py --rss --sec --fda --stocktwits --bluesky  # with social
```

---

## Project Structure

```
Financial-News-Faculty-Project/
├── backend/
│   ├── IngestionModule.py      # extractors, dedup cache, topic classifier, dispatch router
│   ├── UnstructuredModule.py   # UnstructuredAgent (StockTwits + Bluesky pollers)
│   ├── sentiment.py            # FinBERT, Loughran-McDonald analyzers
│   ├── storage_handlers.py     # MongoHandler (w/ social fast-path), RedisHandler
│   └── run_ingest.py           # CLI entry point (--rss --sec --fda --stocktwits --bluesky)
├── middleware/
│   ├── api.py                  # FastAPI app, lifespan (FinBERT + MongoDB init)
│   ├── limiter.py              # shared slowapi rate limiter
│   └── routes/
│       ├── news.py             # /api/news endpoints
│       └── sentiment.py        # /api/sentiment endpoints
├── frontend/
│   └── src/
│       ├── app/page.tsx        # main page (fetch + sentiment scoring + tab routing)
│       ├── components/
│       │   ├── TabNav.tsx      # Structured / Unstructured tab switcher
│       │   ├── UnstructuredView.tsx  # social tab layout
│       │   ├── SocialFeed.tsx  # social post cards
│       │   ├── FilterSidebar.tsx     # structured filters (sort, topic, sentiment, tickers, limit)
│       │   ├── SourceBadge.tsx # RSS / SEC / FDA / SOCIAL badges
│       │   └── ...             # Header, NewsFeed, NewsCard, StatsBar, SentimentBadge
│       ├── lib/
│       │   ├── api.ts          # fetchNews, scoreSentimentBatch
│       │   └── mockData.ts     # fallback data (structured + social mock items)
│       └── types/news.ts       # shared TypeScript types (SourceType includes "social")
├── .env                        # credentials (git-ignored)
├── .gitignore
└── start.ps1                   # launches all three services
```

---

## Data Licensing & Compliance

This is a **non-commercial, academic research project** built for a faculty
advisor. It is **not** a commercial product or a public market-data service, and
nothing here constitutes **investment advice**.

### Data sources and their terms

| Source | Type | Notes |
|---|---|---|
| RSS newswires (Bloomberg, FT, WSJ, CNBC, PR Newswire, GlobeNewswire, Benzinga, …) | Official public feeds | Published for syndication; consumed as intended |
| SEC EDGAR | Official US-government API | Public; a contact User-Agent is sent per SEC fair-access policy (`SEC_CONTACT_EMAIL`) |
| FDA (openFDA) | Official US-government API | Public |
| Reddit | Public RSS feeds | Unauthenticated, low-volume |
| Bluesky | Official AT Protocol API | Unauthenticated read |
| Anthropic (Claude) | Licensed, paid API | Powers the catalyst ranker |
| **Yahoo Finance (via `yfinance`)** | **Unofficial library** | Prices, quotes, OHLCV charts, the screener, and market caps. Ubiquitous in research but **not an officially sanctioned API**, and subject to Yahoo's Terms of Service. Used here for **non-commercial, educational** purposes with **delayed** data only. |

Sources that require circumventing a deliberate access control (e.g. browser/TLS
impersonation to bypass a bot block) are **intentionally not used** — StockTwits
and Finviz scraping were removed for this reason.

### Compliance posture

- **Delayed, non-commercial, educational display only.** Real-time exchange-data
  redistribution requires exchange agreements and is out of scope.
- **Private / advisor-facing deployment.** The site is marked `noindex` and can be
  password-gated with HTTP Basic Auth by setting `BASIC_AUTH_USER` /
  `BASIC_AUTH_PASS` env vars on the host (see `frontend/src/middleware.ts`), so it
  runs as a private demo rather than a public page.
- **No data redistribution.** Data is shown for academic demonstration — not
  republished, resold, or offered as a service.
- For a future **public / commercial** deployment, replace `yfinance` with a
  licensed market-data provider whose terms permit public display of (delayed)
  data — e.g. Polygon, Financial Modeling Prep, Tiingo, or Finnhub — and source
  screener data from an authorized API (e.g. Finviz Elite, where its terms allow).

### Code license

The **source code** in this repository is provided for academic and educational
use. Third-party data accessed at runtime remains the property of its respective
providers and is governed by their terms. Provided **as-is, without warranty**;
use at your own risk. **Not investment advice.**
