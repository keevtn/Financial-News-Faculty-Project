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

export async function fetchNews(
  limit: number | null = 100,
  secLimit = 25,
  fdaLimit = 25,
): Promise<NewsItem[]> {
  try {
    const params = new URLSearchParams({ sec_limit: String(secLimit), fda_limit: String(fdaLimit) });
    if (limit !== null) params.set("limit", String(limit));
    const res = await fetch(`${API_BASE}/api/news/?${params}`);
    if (!res.ok) return MOCK_NEWS;
    const data = await res.json();
    // Fall back to mock data if MongoDB has no items yet
    if (!data.items?.length) return MOCK_NEWS;
    return data.items as NewsItem[];
  } catch {
    return MOCK_NEWS;
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
