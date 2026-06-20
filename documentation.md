# Documentation — Feature Log

A running, human-readable log of features added to the Financial News project.
Newest entries first. Each entry records **when** it was added, **which files**
it touched, **what** it does, and the **specifics/caveats** worth knowing.

> Maintained by Claude. This complements (does not replace) git history — git
> tells you *what changed line-by-line*; this tells you *why a feature exists
> and how it works at a glance*.

---

## 2026-06-20 15:10 EDT — Pre-market Catalyst Ranking System

**Type:** Feature (backend + API)

**Files added**
- `backend/market_calendar.py` — US-equity trading calendar (stdlib only).
- `backend/catalyst_ranker.py` — the ranking engine.
- `middleware/routes/catalyst.py` — REST endpoints.

**Files revised**
- `backend/ticker_extractor.py` — recall + precision improvements to the company map.
- `middleware/api.py` — registered the catalyst router; added `rankings_collection` to app state.
- `middleware/requirements.txt` — added `anthropic` (optional at runtime).

**What it does**

Ranks the tickers with the strongest overnight **news catalyst** before the
market opens — framed as *catalyst ranking*, not price prediction (an honest,
defensible claim). Output is a ranked list with a direction, a confidence, a
rationale, and a persisted, auditable record of every run.

**Pipeline (in `catalyst_ranker.rank_catalysts`)**
1. **Window** — `market_calendar.overnight_window()` returns the prev-close →
   next-open span, skipping weekends/holidays (clamped so it never reads the future).
2. **Authoritative re-extraction** — re-extracts tickers from each doc with the
   *current* extractor rather than trusting `doc["tickers"]` stored at ingestion
   under an older map. Precision/recall fixes therefore apply to already-stored
   data with **no backfill**.
3. **Near-duplicate clustering** — collapses a story syndicated across many
   outlets into one "story" via title token-Jaccard (+ a sequence-ratio tiebreak),
   so reprints don't inflate attention. Breadth is counted as *independent sources*.
4. **Hybrid candidate selection** — a volume floor (default ≥2 independent
   sources) kills single-source noise, then a transparent composite **pre-score**
   ranks them: `attention × abnormal-attention × sentiment-magnitude ×
   materiality × source-credibility`, each component bounded to [0,1].
   - *Abnormal attention* = today's mentions ÷ trailing daily baseline (default
     14 days), so a stock that is *unusually* in the news today outranks one
     that is *always* in the news (kills mega-cap bias).
   - *Materiality* weights SEC/FDA above RSS (regulatory news moves expectations).
5. **LLM deep-read (optional)** — one temperature-0 Anthropic tool-use call
   (`claude-opus-4-8`) scores the shortlist against an explicit rubric
   (materiality / surprise / sentiment_strength / breadth) and writes a
   one-sentence rationale. **Degrades gracefully** to the quantitative pre-score
   when `ANTHROPIC_API_KEY` or the `anthropic` package is absent.
6. **Persistence** — each run stores `run_id`, window, params, the exact prompt,
   the raw LLM output, and the ranked items in the `catalyst_rankings` collection
   (reproducible + later gradeable).

**API endpoints (`/api/catalyst`)**
- `GET /latest` — most recent persisted ranking (public, cheap; what the dashboard reads).
- `GET /runs?limit=N` — recent run metadata.
- `POST /run` — generate + persist a ranking. **Protected** by `X-API-Key`
  (reuses `AGENT_API_KEY`) because it can spend LLM tokens. Rate-limited 6/hour.
- `POST /grade/{run_id}` — direction-agnostic **evaluation**: pulls the next
  session's open→close move (yfinance) for the ranked tickers and reports
  whether the top half moved more than the bottom half, plus a directional
  hit-rate. **Protected**; refuses until that session has actually closed.

**Specifics / caveats**
- Ticker map expanded ~80 → ~280 names (recall). Finance-homographs guarded for
  precision: `target`→requires "target corp", `block`/`square`→requires
  "block inc", `nasdaq`→requires "nasdaq inc" (bare "Nasdaq" is the index, not
  Nasdaq Inc). Removed an invalid `stripe→STRP` entry (Stripe is private).
- Verified end-to-end against live Mongo on the **quantitative path** (no key
  needed): 822 docs → 191 tickers → 17 qualifying → a clean, corroborated top-10.
- The **LLM path is not yet tested live** — set `ANTHROPIC_API_KEY` +
  `AGENT_API_KEY` and `POST /api/catalyst/run` to exercise it.
- **No frontend surface yet** — backend + API only. A "Catalysts" dashboard tab
  is the natural next step.
- Catalyst ranking is phase 1; **price prediction** is a deliberate later phase
  that will reuse the persisted runs + the `grade_ranking` eval as training signal.

---

## 2026-06-20 — In-process Ingestion on the Hosted Deployment

**Type:** Feature (deployment / backend)

**Files**
- `backend/ingestion_runner.py` (added) — starts/stops the ingestion agent inside the API process.
- `middleware/api.py` (revised) — calls the runner in the FastAPI lifespan.
- `backend/sentiment.py` (revised) — guarded the `IngestionModule` import under
  `TYPE_CHECKING` so the LM analyzer loads in the lean middleware.

**What it does**

Lets the always-on web service also poll RSS/SEC/FDA and write to MongoDB, so
feeds stay fresh **without a local machine running**. Gated by the
`RUN_INGESTION` env var (set `true` on Render; leave unset locally where
`start.ps1` already runs ingestion separately). Structured items are scored with
the Loughran-McDonald analyzer at write time. Relaxed poll intervals
(RSS 120s / SEC 600s / FDA 600s) since it shares one free-tier process.

**Caveat:** Render's free web tier sleeps after ~15 min without inbound HTTP,
which also pauses ingestion — keep it awake with an external uptime pinger
(cron-job.org / UptimeRobot) hitting `/health` every ~10 min.

---

## 2026-06-20 — Model-agnostic Sentiment Labels (Frontend)

**Type:** Fix (frontend copy)

**Files**
- `frontend/src/components/Header.tsx` — "Scoring with FinBERT…" → "Scoring sentiment…".
- `frontend/src/components/NewsCard.tsx`, `frontend/src/app/page.tsx`,
  `frontend/src/lib/api.ts`, `frontend/src/lib/mockData.ts` — stale FinBERT
  references in comments/labels updated.

**Why:** The deployment scores with Loughran-McDonald (FinBERT needs torch,
which isn't installed on the free tier), so the UI shouldn't name a specific
model. The batch endpoint already auto-falls-back, so the label is now accurate
in every environment.
