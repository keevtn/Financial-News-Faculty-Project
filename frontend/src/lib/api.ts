import { NewsItem, SentimentResult, SentimentSummary } from "@/types/news";
import { MOCK_NEWS } from "./mockData";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const USE_MOCK = process.env.NEXT_PUBLIC_USE_MOCK !== "false";

export async function fetchNews(limit: number | null = 100): Promise<NewsItem[]> {
  try {
    const url = limit !== null ? `${API_BASE}/api/news?limit=${limit}` : `${API_BASE}/api/news`;
    const res = await fetch(url);
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

/**
 * Score a batch of items with FinBERT via the middleware.
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
    // Middleware not running — degrade gracefully, cards show no badge
    return {};
  }
}
