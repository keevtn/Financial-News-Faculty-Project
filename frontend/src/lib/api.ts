import { NewsItem, SentimentResult, SentimentSummary } from "@/types/news";
import { MOCK_NEWS } from "./mockData";

export interface TickerPrice {
  symbol: string;
  price: number | null;
  change: number | null;
  change_pct: number | null;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const USE_MOCK = process.env.NEXT_PUBLIC_USE_MOCK !== "false";

export type FeedSourceType = "structured" | "social";

function filterMock(sourceType?: FeedSourceType): NewsItem[] {
  if (sourceType === "social") return MOCK_NEWS.filter((i) => i.source_type === "social");
  if (sourceType === "structured") return MOCK_NEWS.filter((i) => i.source_type !== "social");
  return MOCK_NEWS;
}

/**
 * Fetch news items. Pass `sourceType` so the Structured and Social tabs each
 * query their own feed independently — otherwise the high-volume social feed
 * crowds RSS out of a shared recency window (and "Show recent" stops mattering
 * for structured). "structured" = rss+sec+fda server-side.
 */
export async function fetchNews(
  limit: number | null = 100,
  sourceType?: FeedSourceType,
  secLimit = 25,
  fdaLimit = 25,
): Promise<NewsItem[]> {
  try {
    const params = new URLSearchParams({ sec_limit: String(secLimit), fda_limit: String(fdaLimit) });
    if (limit !== null) params.set("limit", String(limit));
    if (sourceType) params.set("source_type", sourceType);
    const res = await fetch(`${API_BASE}/api/news/?${params}`);
    if (!res.ok) return filterMock(sourceType);
    const data = await res.json();
    // Fall back to mock data if MongoDB has no items yet
    if (!data.items?.length) return filterMock(sourceType);
    return data.items as NewsItem[];
  } catch {
    return filterMock(sourceType);
  }
}

export async function fetchSentimentSummary(
  scope = "global"
): Promise<SentimentSummary | null> {
  if (USE_MOCK) return null;
  const res = await fetch(`${API_BASE}/api/sentiment?scope=${scope}`);
  if (!res.ok) return null;
  return res.json();
}

export async function fetchTickerPrices(
  symbols: string[]
): Promise<Record<string, TickerPrice>> {
  if (!symbols.length) return {};
  try {
    const res = await fetch(
      `${API_BASE}/api/tickers/prices?symbols=${symbols.join(",")}`
    );
    if (!res.ok) return {};
    const data = await res.json();
    return (data.prices ?? {}) as Record<string, TickerPrice>;
  } catch {
    return {};
  }
}

export interface TickerQuote {
  symbol: string;
  price: number | null;
  change: number | null;
  change_pct: number | null;
  volume: number | null;
  market_cap: number | null;
  day_high: number | null;
  day_low: number | null;
  prev_close: number | null;
}

/** Richer live quotes (price/%chg/volume/market cap/day range) for a few symbols. */
export async function fetchTickerQuotes(
  symbols: string[]
): Promise<Record<string, TickerQuote>> {
  if (!symbols.length) return {};
  try {
    const res = await fetch(
      `${API_BASE}/api/tickers/quotes?symbols=${symbols.join(",")}`
    );
    if (!res.ok) return {};
    const data = await res.json();
    return (data.quotes ?? {}) as Record<string, TickerQuote>;
  } catch {
    return {};
  }
}

export interface OHLCVBar {
  time: number;   // UTC epoch seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TickerHistory {
  symbol: string;
  range: string;
  interval: string;
  prev_close: number | null;
  bars: OHLCVBar[];
  status: string | null;
}

export type ChartRange = "1D" | "5D" | "2W" | "1M" | "3M" | "1Y";

export interface SentimentPoint {
  time: number;                      // UTC epoch seconds (day start)
  news_sentiment: number | null;     // -1..1, mean of structured (rss/sec/fda) that day
  news_count: number;
  social_sentiment: number | null;   // -1..1, mean of social that day
  social_count: number;
}

export interface SentimentHistory {
  symbol: string;
  days: number;
  points: SentimentPoint[];
  status: string | null;
}

/** Daily mean sentiment + mention count for a ticker (news + social), for the
 *  sentiment chart shown alongside price. */
export async function fetchTickerSentimentHistory(
  symbol: string,
  days = 30,
): Promise<SentimentHistory | null> {
  try {
    const res = await fetch(
      `${API_BASE}/api/tickers/sentiment-history?symbol=${encodeURIComponent(symbol)}&days=${days}`
    );
    if (!res.ok) return null;
    return (await res.json()) as SentimentHistory;
  } catch {
    return null;
  }
}

/** OHLCV candlestick history for one ticker over a range. */
export async function fetchTickerHistory(
  symbol: string,
  range: ChartRange = "1M",
): Promise<TickerHistory | null> {
  try {
    const res = await fetch(
      `${API_BASE}/api/tickers/history?symbol=${encodeURIComponent(symbol)}&range=${range}`
    );
    if (!res.ok) return null;
    return (await res.json()) as TickerHistory;
  } catch {
    return null;
  }
}

/**
 * Score a batch of structured items via the middleware (FinBERT if available,
 * otherwise the Loughran-McDonald keyword scorer).
 * Returns a map of item id → SentimentResult, or an empty object if the
 * API is unavailable (e.g. middleware not running).
 */
export async function scoreSentimentBatch(
  items: Array<{ id: string; title: string; description: string }>
): Promise<Record<string, SentimentResult>> {
  try {
    const res = await fetch(`${API_BASE}/api/sentiment/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    if (!res.ok) return {};
    const data = await res.json();
    return data.results ?? {};
  } catch {
    return {};
  }
}

/**
 * Score a batch of social/unstructured items with the LM keyword scorer (~1 ms/item).
 * Call this only for items that don't already have sentiment and don't have a
 * StockTwits human label — those should be resolved client-side before calling here.
 */
export async function scoreSocialSentimentBatch(
  items: Array<{ id: string; title: string; description: string }>
): Promise<Record<string, SentimentResult>> {
  if (!items.length) return {};
  try {
    const res = await fetch(`${API_BASE}/api/sentiment/social/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    if (!res.ok) return {};
    const data = await res.json();
    return data.results ?? {};
  } catch {
    return {};
  }
}

// ---------------------------------------------------------------------------
// Catalyst ranking — pre-market ranked tickers (read-only; generated server-side)
// ---------------------------------------------------------------------------

export type Direction = "bullish" | "bearish" | "neutral";

export interface CatalystArticle {
  source: string;
  source_type: string;
  title: string;
  description: string;
  url: string;
  published_at: string;
  reprints: number;
}

export interface CatalystItem {
  rank: number;
  ticker: string;
  catalyst_score: number;
  direction: Direction;
  confidence: number;
  rationale: string;
  n_docs: number;
  n_stories: number;
  n_sources: number;
  source_types: string[];
  mean_sentiment: number;
  abnormal_attention: number;
  market_cap: number | null;
  size_factor: number;
  // Pre-market move from Finviz Elite (null when Elite isn't configured or the
  // ticker had no usable pre-market data). gap_pct = signed % vs prev close;
  // rel_volume = relative-volume ratio (>1 = heavier than normal).
  premarket: {
    gap_pct: number | null;
    rel_volume: number | null;
    price: number | null;
    prev_close: number | null;
    change_pct: number | null;
  } | null;
  confirmation_factor: number;  // pre-market boost applied to the pre-score (1.0–1.2)
  pre_score: number;
  sample_articles: CatalystArticle[];
  llm_subscores: {
    materiality: number | null;
    surprise: number | null;
    sentiment_strength: number | null;
    breadth: number | null;
  } | null;
}

export interface CatalystRanking {
  run_id: string;
  generated_at: string;
  window_start: string;
  window_end: string;
  model: string | null;
  used_llm: boolean;
  llm_status: string | null;
  params: { top_k: number; min_sources: number; baseline_days: number };
  candidate_count: number;
  doc_count: number;
  items: CatalystItem[];
}

/**
 * Fetch the most recent persisted catalyst ranking, or null if none exists yet
 * or the API is unreachable. Generation happens server-side (POST /api/catalyst/run,
 * which is key-protected) — this endpoint is a public read.
 */
export async function fetchLatestCatalystRanking(): Promise<CatalystRanking | null> {
  try {
    const res = await fetch(`${API_BASE}/api/catalyst/latest`);
    if (!res.ok) return null;
    const data = await res.json();
    return (data.ranking ?? null) as CatalystRanking | null;
  } catch {
    return null;
  }
}

export interface CatalystTrackRecord {
  summary: {
    graded_runs: number;
    avg_direction_hit_rate: number | null;   // 0..1
    avg_reaction_separation: number | null;   // signed return delta (top half vs bottom)
    positive_separation_rate: number | null;  // 0..1
  };
  runs: Array<{
    run_id: string;
    generated_at: string;
    used_llm: boolean;
    direction_hit_rate: number | null;
    reaction_separation: number | null;
  }>;
}

/** The catalyst ranker's measured performance across graded runs (public read). */
export async function fetchCatalystTrackRecord(): Promise<CatalystTrackRecord | null> {
  try {
    const res = await fetch(`${API_BASE}/api/catalyst/track-record`);
    if (!res.ok) return null;
    return (await res.json()) as CatalystTrackRecord;
  } catch {
    return null;
  }
}

export type CatalystRunResult =
  | { ok: true }
  | { ok: false; rateLimited: true; retryAfterSeconds: number }
  | { ok: false; rateLimited: false; error: string };

/**
 * Trigger a fresh full-Opus catalyst run via the **same-origin** Next.js proxy
 * (`/api/catalyst/run`), which holds the secret API key server-side. The
 * backend caps manual runs to once per hour and returns 429 on cooldown.
 */
export async function triggerCatalystRun(): Promise<CatalystRunResult> {
  try {
    const res = await fetch("/api/catalyst/run", { method: "POST" });
    if (res.ok) return { ok: true };

    if (res.status === 429) {
      let secs = 3600;
      try {
        const data = await res.json();
        secs = Number(
          data?.detail?.retry_after_seconds ?? data?.retry_after_seconds ?? secs,
        );
      } catch {
        const h = res.headers.get("Retry-After");
        if (h) secs = Number(h);
      }
      return {
        ok: false,
        rateLimited: true,
        retryAfterSeconds: Number.isFinite(secs) ? secs : 3600,
      };
    }

    let msg = `Run failed (HTTP ${res.status}).`;
    try {
      const data = await res.json();
      if (typeof data?.error === "string") msg = data.error;
      else if (typeof data?.detail === "string") msg = data.detail;
    } catch {
      // non-JSON error body — keep the generic message
    }
    return { ok: false, rateLimited: false, error: msg };
  } catch {
    return { ok: false, rateLimited: false, error: "Network error triggering the run." };
  }
}

// ---------------------------------------------------------------------------
// Catalyst candidate universe — accumulated sub-threshold tickers (12h job)
// ---------------------------------------------------------------------------

export interface CatalystUniverseItem {
  ticker: string;
  n_docs: number;
  n_stories: number;
  n_sources: number;
  sources: string[];
  source_types: string[];
  mean_sentiment: number;
  direction: Direction;
  cycles: number;
  first_seen: string;
  last_seen: string;
  promoted: boolean;           // graduated past the standard volume floor
  sample_articles: CatalystArticle[];
}

export interface CatalystUniverseResult {
  items: CatalystUniverseItem[];
  count: number;
}

/**
 * The growing watchlist of emerging candidate tickers — names accumulating
 * coverage over time that don't (yet) clear the standard ranker's floor.
 * Public read; produced by the 12h universe job.
 */
export async function fetchCatalystUniverse(
  limit = 50,
  promotedOnly = false,
): Promise<CatalystUniverseResult> {
  try {
    const params = new URLSearchParams({ limit: String(limit) });
    if (promotedOnly) params.set("promoted_only", "true");
    const res = await fetch(`${API_BASE}/api/catalyst/universe?${params}`);
    if (!res.ok) return { items: [], count: 0 };
    return (await res.json()) as CatalystUniverseResult;
  } catch {
    return { items: [], count: 0 };
  }
}

// ---------------------------------------------------------------------------
// Squeeze ranking — short fuel × social ignition (read-only; generated server-side)
// ---------------------------------------------------------------------------

export interface SqueezePost {
  text: string;
  likes: number;
  replies: number;
  handle: string;
  created_at: string;
}

export interface SqueezeItem {
  rank: number;
  ticker: string;
  short_pct_float: number | null;   // fraction (0.289 = 28.9%)
  short_ratio: number | null;       // days to cover
  float_shares: number | null;
  n_posts: number;
  breadth: number;                  // distinct social authors (propagation)
  focus_score: number;
  social_sentiment: number;         // -1..1
  social_velocity: number | null;   // gossip mention acceleration (× baseline)
  search_velocity: number | null;   // Google-Trends search acceleration (× baseline)
  search_clock: string | null;      // 'fast' | 'slow' fuel clock
  divergence: string | null;        // early | mainstream | search-led | aligned
  engagement: number;
  fuel_score: number;               // 0..1
  ignition_score: number;           // 0..1
  squeeze_score: number;            // 0..100
  direction: Direction;
  sources: string[];
  components: Record<string, number>;
  sample_posts: SqueezePost[];
}

export interface SqueezeRanking {
  run_id: string;
  generated_at: string;
  params: { top_k: number; min_short_float: number; max_fueled: number; social_limit: number };
  universe_count: number;
  fueled_count: number;
  social_count: number;
  items: SqueezeItem[];
}

/** Most recent persisted squeeze ranking, or null. Public read (generated server-side). */
export async function fetchLatestSqueeze(): Promise<SqueezeRanking | null> {
  try {
    const res = await fetch(`${API_BASE}/api/squeeze/latest`);
    if (!res.ok) return null;
    const data = await res.json();
    return (data.ranking ?? null) as SqueezeRanking | null;
  } catch {
    return null;
  }
}

export interface SqueezeTrackRecord {
  summary: {
    graded_runs: number;
    avg_squeeze_hit_rate: number | null;    // 0..1
    avg_reaction_separation: number | null;  // top-half vs bottom-half peak gain
    avg_close_return: number | null;
  };
  runs: Array<{
    run_id: string;
    generated_at: string;
    squeeze_hit_rate: number | null;
    reaction_separation: number | null;
  }>;
}

/** The squeeze ranker's measured performance across graded runs (public read). */
export async function fetchSqueezeTrackRecord(): Promise<SqueezeTrackRecord | null> {
  try {
    const res = await fetch(`${API_BASE}/api/squeeze/track-record`);
    if (!res.ok) return null;
    return (await res.json()) as SqueezeTrackRecord;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Validation — which signals actually predict the move (read-only)
// ---------------------------------------------------------------------------

export interface ValidationSignal {
  signal: string;
  n: number;
  correlation: number | null;       // Spearman vs realized forward move
  top_minus_bottom: number | null;  // top-third minus bottom-third mean outcome
  verdict: string;                  // predictive | weak | no edge | insufficient data | no variance
}

export interface ValidationGroup {
  n_runs: number;
  outcome: string;
  signals: ValidationSignal[];
}

export interface ValidationResult {
  squeeze: ValidationGroup;
  catalyst: ValidationGroup;
  note: string;
}

/** Per-signal predictive value across graded runs (squeeze + catalyst). */
export async function fetchValidation(): Promise<ValidationResult | null> {
  try {
    const res = await fetch(`${API_BASE}/api/validation`);
    if (!res.ok) return null;
    return (await res.json()) as ValidationResult;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Options flow — per-ticker put/call + implied vol (yfinance, read-only)
// ---------------------------------------------------------------------------

export interface OptionsSignal {
  ticker: string;
  spot: number | null;
  put_call_ratio: number | null;     // by volume
  put_call_oi_ratio: number | null;  // by open interest
  atm_iv: number | null;             // fraction (0.39 = 39%)
  lean: Direction;
  call_volume: number;
  put_volume: number;
  call_oi: number;
  put_oi: number;
  expiries: string[];
}

/** Per-ticker options signal (put/call ratio, ATM IV, lean), or null if no chain. */
export async function fetchOptions(symbol: string): Promise<OptionsSignal | null> {
  try {
    const res = await fetch(`${API_BASE}/api/options?symbol=${encodeURIComponent(symbol)}`);
    if (!res.ok) return null;
    const data = await res.json();
    return (data.signal ?? null) as OptionsSignal | null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Alerts — signal thresholds crossed (squeeze / gossip / catalyst), read-only
// ---------------------------------------------------------------------------

export type AlertSeverity = "critical" | "high" | "medium";

export interface AlertItem {
  ticker: string;
  severity: AlertSeverity;
  title: string;
  detail: string;
  signals: string[];   // which fired: squeeze | gossip | catalyst
  value: number;
  tab: string;         // where to look
}

export interface AlertsResult {
  alerts: AlertItem[];
  counts: { critical: number; high: number; medium: number };
  total: number;
  generated_at: number;
  cached?: boolean;
}

/** Current alerts: tickers that crossed a signal threshold, ranked by confluence. */
export async function fetchAlerts(): Promise<AlertsResult | null> {
  try {
    const res = await fetch(`${API_BASE}/api/alerts`);
    if (!res.ok) return null;
    return (await res.json()) as AlertsResult;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Gossip — rolling-window mention velocity over the social stream (read-only)
// ---------------------------------------------------------------------------

export interface GossipItem {
  rank: number;
  ticker: string;
  recent_count: number;
  breadth: number;           // distinct authors in the recent window (propagation)
  baseline_rate: number;
  velocity: number;          // recent rate ÷ trailing baseline (>1 = accelerating)
  mean_sentiment: number;    // -1..1
  direction: Direction;
  gossip_score: number;      // 0..100
}

export interface GossipResult {
  generated_at: string;
  params: { recent_hours: number; baseline_days: number; min_recent: number };
  ticker_count: number;
  post_count: number;
  items: GossipItem[];
  cached?: boolean;
}

/** Tickers whose social chatter is accelerating vs their own baseline. Public read.
 *  `recentHours` = the recent window; `baselineDays` = the trailing baseline it's
 *  compared against (e.g. last 4h vs a 7-day baseline). */
export async function fetchGossip(
  recentHours = 6,
  baselineDays = 7,
): Promise<GossipResult | null> {
  try {
    const params = new URLSearchParams({
      recent_hours: String(recentHours),
      baseline_days: String(baselineDays),
    });
    const res = await fetch(`${API_BASE}/api/gossip?${params}`);
    if (!res.ok) return null;
    return (await res.json()) as GossipResult;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Numeric screener — market-wide movers from Finviz (read-only)
// ---------------------------------------------------------------------------

export interface ScreenerRow {
  ticker: string;
  company: string;
  sector: string;
  industry: string;
  country: string;
  market_cap: number | null;
  pe: number | null;
  price: number | null;
  change_pct: number | null;
  volume: number | null;
}

export interface ScreenerResult {
  rows: ScreenerRow[];
  count: number;
  preset: string;
  status: string | null;     // non-null only when the source was unreachable/unparseable
  source?: string;           // "finviz_elite" | "yahoo"
  cached: boolean;
  fetched_at?: number;
}

export interface ScreenerPreset {
  id: string;
  label: string;
}

/** List the available screen presets (Top Gainers, Most Active, …). */
export async function fetchScreenerPresets(): Promise<ScreenerPreset[]> {
  try {
    const res = await fetch(`${API_BASE}/api/screener/presets`);
    if (!res.ok) return [];
    const data = await res.json();
    return (data.presets ?? []) as ScreenerPreset[];
  } catch {
    return [];
  }
}

/** Run a screen; returns an empty result (with a status reason) on failure. */
export async function fetchScreener(
  preset = "top_gainers",
  limit = 30,
): Promise<ScreenerResult> {
  const empty: ScreenerResult = {
    rows: [], count: 0, preset, status: "request failed", cached: false,
  };
  try {
    const params = new URLSearchParams({ preset, limit: String(limit) });
    const res = await fetch(`${API_BASE}/api/screener?${params}`);
    if (!res.ok) return { ...empty, status: `HTTP ${res.status}` };
    return (await res.json()) as ScreenerResult;
  } catch {
    return empty;
  }
}
