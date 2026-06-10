# Financial News Faculty Project

A real-time financial news monitoring platform that ingests headlines from multiple sources, classifies sentiment using a finance-tuned transformer model, and displays results in a live dashboard.

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

- **RSS Extractor** — polls 14 financial newswires (Bloomberg, CNBC, WSJ, FT, MarketWatch, CoinDesk, Yahoo Finance, Federal Reserve, BLS, and others) on a configurable interval (default 60 s)
- **SEC EDGAR Extractor** — polls EDGAR for 8-K, 10-K, 10-Q, S-1, and 6-K filings (default 300 s)
- **FDA Extractor** — collects FDA press releases and drug enforcement/recall actions from the openFDA API (default 180 s)
- **Deduplication** — SHA-256 content hash cache prevents the same article from being dispatched twice across restarts
- **Keyword filter** — items are only dispatched if they match a configurable list of financial keywords (inflation, earnings, FDA, bitcoin, etc.)
- **Topic classifier** — assigns a topic label (Crypto, Energy, Equities, Macro, Regulatory, Bonds, Commodities, Technology) based on keyword matching
- **CSV export** — optional handler writes every dispatched item to a CSV file

### Sentiment Analysis

- **FinBERT** (`ProsusAI/finbert`) — primary analyzer; BERT fine-tuned on ~10k financial sentences. Outputs a continuous score P(positive) − P(negative) ∈ [−1, 1] and a bullish / bearish / neutral label
- **Loughran-McDonald lexicon** — lightweight fallback (~150 built-in terms, no ML model required); supports loading the full ~3,500-word master dictionary CSV
- All analyzers share a common `SentimentAnalyzer` protocol and return a `SentimentResult(score, label, confidence)`

### Storage

- **MongoDB** (`MongoHandler`) — durable archive of every ingested article; idempotent upsert on `content_hash` so re-seen articles are never duplicated. Indexed on `content_hash`, `published_at`, and `source_type`
- **Redis** (`RedisHandler`) — rolling time-windowed sentiment store; maintains per-scope sorted sets (global, per source type, per source, per keyword) for "what is the mood about X right now?" queries

### Middleware — FastAPI

- `GET  /api/news` — paginated article feed from MongoDB, filterable by source type, topic, and full-text search
- `GET  /api/news/topics` — aggregated topic list with article counts
- `GET  /api/news/sources` — all known sources and their types
- `POST /api/sentiment/batch` — scores a batch of articles with FinBERT; returns score, label, and confidence per item
- `GET  /api/sentiment/` — aggregated sentiment statistics (wired to Redis when available)
- `GET  /health` — liveness check
- FinBERT model is loaded eagerly at startup and held in `app.state` so the first request is not slow
- MongoDB connection opened at startup via `app.state`; gracefully degrades to empty responses if unavailable

### Security & Rate Limiting

- No credentials in source code — all secrets via `.env` / environment variables
- `.env` excluded from git via `.gitignore`
- CORS restricted to `http://localhost:3000`
- User-supplied search strings escaped with `re.escape` before use in MongoDB regex queries (ReDoS prevention)
- Redis connection URL password masked in logs
- Batch sentiment endpoint capped at 100 items per request
- Per-IP rate limits enforced via `slowapi`:
  - `POST /api/sentiment/batch` — 20 requests/minute
  - `GET /api/news` — 60 requests/minute
  - `GET /api/sentiment/` — 60 requests/minute
  - `GET /api/news/topics`, `/api/news/sources` — 30 requests/minute

### Frontend — Next.js Dashboard

- Fetches live articles from MongoDB via the middleware on page load; falls back to mock data if the API is unavailable
- FinBERT sentiment scored client-side via `POST /api/sentiment/batch` on mount — cards show a pulsing skeleton while scoring is in progress
- Sentiment badge displays label (bullish / bearish / neutral) and numeric FinBERT score (e.g. `▲ BULLISH +0.87`)
- Filter sidebar — filter by source type (RSS / SEC / FDA), topic, sentiment label, and free-text search
- Stats bar showing article counts per source type and sentiment breakdown

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
```

### Run everything

```powershell
.\start.ps1
```

This opens three terminal windows:

| Window | Service | URL |
|---|---|---|
| Middleware | FastAPI + FinBERT | http://localhost:8000/docs |
| Frontend | Next.js dashboard | http://localhost:3000 |
| Ingestion | RSS + SEC + FDA pipeline | writes to MongoDB |

Or start services individually:

```bash
# Middleware (from project root)
uvicorn middleware.api:app --reload --port 8000

# Frontend
cd frontend && npm run dev

# Ingestion (from backend/)
python run_ingest.py --rss --sec --fda
```

---

## Project Structure

```
Financial-News-Faculty-Project/
├── backend/
│   ├── IngestionModule.py      # extractors, dedup cache, topic classifier, dispatch router
│   ├── sentiment.py            # FinBERT, Loughran-McDonald analyzers
│   ├── storage_handlers.py     # MongoHandler, RedisHandler
│   └── run_ingest.py           # CLI entry point
├── middleware/
│   ├── api.py                  # FastAPI app, lifespan (FinBERT + MongoDB init)
│   ├── limiter.py              # shared slowapi rate limiter
│   └── routes/
│       ├── news.py             # /api/news endpoints
│       └── sentiment.py        # /api/sentiment endpoints
├── frontend/
│   └── src/
│       ├── app/page.tsx        # main page (fetch + sentiment scoring)
│       ├── components/         # Header, NewsFeed, NewsCard, SentimentBadge, ...
│       ├── lib/
│       │   ├── api.ts          # fetchNews, scoreSentimentBatch
│       │   └── mockData.ts     # fallback data (no pre-set sentiment)
│       └── types/news.ts       # shared TypeScript types
├── .env                        # credentials (git-ignored)
├── .gitignore
└── start.ps1                   # launches all three services
```
