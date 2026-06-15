"use client";

import { useState, useMemo, useEffect } from "react";
import { ALL_TOPICS, ALL_SENTIMENTS, STRUCTURED_SOURCE_TYPES, ALL_PLATFORMS } from "@/lib/mockData";
import { fetchNews, scoreSentimentBatch, scoreSocialSentimentBatch } from "@/lib/api";
import { FilterState, NewsItem, SentimentLabel, SocialFilterState, SourceType } from "@/types/news";
import Header from "@/components/Header";
import FilterSidebar from "@/components/FilterSidebar";
import NewsFeed from "@/components/NewsFeed";
import StatsBar from "@/components/StatsBar";
import TabNav, { TabId } from "@/components/TabNav";
import UnstructuredView from "@/components/UnstructuredView";
import TickerTape from "@/components/TickerTape";

const DEFAULT_FILTERS: FilterState = {
  topics: new Set(ALL_TOPICS),
  sourceTypes: new Set(STRUCTURED_SOURCE_TYPES),
  sentiments: new Set(ALL_SENTIMENTS),
  tickers: new Set(),
  search: "",
  sortBy: "latest",
  limit: 100,
};

const DEFAULT_SOCIAL_FILTERS: SocialFilterState = {
  search: "",
  sentiments: new Set(ALL_SENTIMENTS),
  tickers: new Set(),
  platforms: new Set(ALL_PLATFORMS),
  sortBy: "latest",
  limit: 100,
};

function getPlatform(source: string): string {
  if (source.startsWith("Reddit")) return "Reddit";
  if (source.startsWith("StockTwits")) return "StockTwits";
  if (source.startsWith("Bluesky")) return "Bluesky";
  return "Other";
}

export default function HomePage() {
  const [items, setItems] = useState<NewsItem[]>([]);
  const [scoringPending, setScoringPending] = useState(true);
  const [socialScoringPending, setSocialScoringPending] = useState(true);
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const [socialFilters, setSocialFilters] = useState<SocialFilterState>(DEFAULT_SOCIAL_FILTERS);
  const [activeTab, setActiveTab] = useState<TabId>("structured");

  // Split at source_type so structured and social tabs never share items.
  const structuredItems = useMemo(
    () => items.filter((i) => i.source_type !== "social"),
    [items]
  );
  const socialItems = useMemo(
    () => items.filter((i) => i.source_type === "social"),
    [items]
  );

  // Fetch news from MongoDB (falls back to mock), then score any unscored items.
  // Re-runs when committed limit changes.
  useEffect(() => {
    let cancelled = false;

    async function load() {
      const fetched = await fetchNews(filters.limit ?? 100);
      if (cancelled) return;
      setItems(fetched);

      // Run structured and social scoring in parallel so structured-scoring
      // latency doesn't block social sentiment from appearing.
      await Promise.all([

        // ── Structured: middleware scorer (FinBERT if available, else LM) ────
        (async () => {
          const unscored = fetched.filter((i) => !i.sentiment && i.source_type !== "social");
          if (unscored.length > 0) {
            const CHUNK = 100;
            for (let i = 0; i < unscored.length; i += CHUNK) {
              const batch = unscored.slice(i, i + CHUNK);
              const scored = await scoreSentimentBatch(
                batch.map((it) => ({ id: it.id, title: it.title, description: it.description }))
              );
              if (cancelled) return;
              if (Object.keys(scored).length > 0) {
                setItems((prev) =>
                  prev.map((item) => ({
                    ...item,
                    sentiment: scored[item.id] ?? item.sentiment,
                  }))
                );
              }
            }
          }
          if (!cancelled) setScoringPending(false);
        })(),

        // ── Social: StockTwits labels first, then LM keyword scorer ──────────
        (async () => {
          const unscoredSocial = fetched.filter((i) => !i.sentiment && i.source_type === "social");
          if (unscoredSocial.length > 0) {
            // Pass 1: StockTwits human labels — instant, no API call
            const stLabelled: Record<string, { score: number; label: string; confidence: number }> = {};
            const needsLM: typeof unscoredSocial = [];
            for (const item of unscoredSocial) {
              const st = item.extra?.st_sentiment as string | undefined;
              if (st === "Bullish") {
                stLabelled[item.id] = { score: 0.5, label: "bullish", confidence: 0.8 };
              } else if (st === "Bearish") {
                stLabelled[item.id] = { score: -0.5, label: "bearish", confidence: 0.8 };
              } else {
                needsLM.push(item);
              }
            }
            if (Object.keys(stLabelled).length > 0 && !cancelled) {
              setItems((prev) =>
                prev.map((item) => ({
                  ...item,
                  sentiment: (stLabelled[item.id] as typeof item.sentiment) ?? item.sentiment,
                }))
              );
            }
            // Pass 2: LM keyword scoring for Reddit/Bluesky items (~1 ms/item)
            if (needsLM.length > 0) {
              const CHUNK = 100;
              for (let i = 0; i < needsLM.length; i += CHUNK) {
                const batch = needsLM.slice(i, i + CHUNK);
                const scored = await scoreSocialSentimentBatch(
                  batch.map((it) => ({ id: it.id, title: it.title, description: it.description }))
                );
                if (cancelled) return;
                if (Object.keys(scored).length > 0) {
                  setItems((prev) =>
                    prev.map((item) => ({
                      ...item,
                      sentiment: scored[item.id] ?? item.sentiment,
                    }))
                  );
                }
              }
            }
          }
          if (!cancelled) setSocialScoringPending(false);
        })(),

      ]);
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [filters.limit]);

  // All unique tickers from all articles (structured + social), sorted by mention frequency.
  // Used by the ticker tape — independent of filter state so the tape is stable.
  const allSymbols = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of items) {
      for (const t of item.tickers ?? []) {
        counts.set(t, (counts.get(t) ?? 0) + 1);
      }
    }
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([t]) => t);
  }, [items]);

  // ── Structured pipeline: three-stage filter ──────────────────────────────

  // All filters except tickers, plus sort — runs only on structured items.
  const preTickerFiltered = useMemo(() => {
    const result = structuredItems.filter((item) => {
      if (!filters.sourceTypes.has(item.source_type as SourceType)) return false;
      if (item.sentiment && !filters.sentiments.has(item.sentiment.label as SentimentLabel))
        return false;
      const itemTopics = item.topic.split(",").map((t) => t.trim());
      if (!itemTopics.some((t) => filters.topics.has(t))) return false;
      if (filters.search) {
        const q = filters.search.toLowerCase();
        if (
          !item.title.toLowerCase().includes(q) &&
          !item.description.toLowerCase().includes(q)
        )
          return false;
      }
      return true;
    });
    if (filters.sortBy === "score_desc") {
      return [...result].sort(
        (a, b) => (b.sentiment?.score ?? -2) - (a.sentiment?.score ?? -2)
      );
    }
    if (filters.sortBy === "score_asc") {
      return [...result].sort(
        (a, b) => (a.sentiment?.score ?? 2) - (b.sentiment?.score ?? 2)
      );
    }
    return result;
  }, [structuredItems, filters]);

  // Count how many pre-filtered structured items each ticker appears in.
  const tickerCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of preTickerFiltered) {
      for (const ticker of item.tickers ?? []) {
        counts.set(ticker, (counts.get(ticker) ?? 0) + 1);
      }
    }
    return counts;
  }, [preTickerFiltered]);

  // Apply ticker filter then limit last so counts above stay accurate.
  const filtered = useMemo(() => {
    let result = preTickerFiltered;
    if (filters.tickers.size > 0) {
      result = result.filter((item) =>
        item.tickers?.some((t) => filters.tickers.has(t))
      );
    }
    return filters.limit !== null ? result.slice(0, filters.limit) : result;
  }, [preTickerFiltered, filters.tickers, filters.limit]);

  // ── Social pipeline: three-stage filter ──────────────────────────────────

  const socialPreTickerFiltered = useMemo(() => {
    const result = socialItems.filter((item) => {
      if (!socialFilters.platforms.has(getPlatform(item.source))) return false;
      if (item.sentiment && !socialFilters.sentiments.has(item.sentiment.label as SentimentLabel))
        return false;
      if (socialFilters.search) {
        const q = socialFilters.search.toLowerCase();
        if (
          !item.title.toLowerCase().includes(q) &&
          !item.description.toLowerCase().includes(q)
        )
          return false;
      }
      return true;
    });
    if (socialFilters.sortBy === "score_desc") {
      return [...result].sort(
        (a, b) => (b.sentiment?.score ?? -2) - (a.sentiment?.score ?? -2)
      );
    }
    if (socialFilters.sortBy === "score_asc") {
      return [...result].sort(
        (a, b) => (a.sentiment?.score ?? 2) - (b.sentiment?.score ?? 2)
      );
    }
    return result;
  }, [socialItems, socialFilters]);

  // Count how many pre-filtered social items each ticker appears in.
  const socialTickerCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of socialPreTickerFiltered) {
      for (const ticker of item.tickers ?? []) {
        counts.set(ticker, (counts.get(ticker) ?? 0) + 1);
      }
    }
    return counts;
  }, [socialPreTickerFiltered]);

  // Apply ticker filter then limit last so counts above stay accurate.
  const socialFiltered = useMemo(() => {
    let result = socialPreTickerFiltered;
    if (socialFilters.tickers.size > 0) {
      result = result.filter((item) =>
        item.tickers?.some((t) => socialFilters.tickers.has(t))
      );
    }
    return socialFilters.limit !== null ? result.slice(0, socialFilters.limit) : result;
  }, [socialPreTickerFiltered, socialFilters.tickers, socialFilters.limit]);

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <Header itemCount={filtered.length} scoringPending={scoringPending} />
      <TickerTape symbols={allSymbols} pollIntervalMs={60_000} />
      <TabNav active={activeTab} onChange={setActiveTab} />
      {activeTab === "structured" ? (
        <>
          <StatsBar items={filtered} />
          <div className="flex flex-1 overflow-hidden">
            <FilterSidebar filters={filters} onChange={setFilters} tickerCounts={tickerCounts} />
            <NewsFeed items={filtered} scoringPending={scoringPending} />
          </div>
        </>
      ) : (
        <UnstructuredView
          items={socialFiltered}
          totalCount={socialItems.length}
          filters={socialFilters}
          onChange={setSocialFilters}
          tickerCounts={socialTickerCounts}
          scoringPending={socialScoringPending}
        />
      )}
    </div>
  );
}
