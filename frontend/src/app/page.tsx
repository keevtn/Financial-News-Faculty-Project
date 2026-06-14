"use client";

import { useState, useMemo, useEffect } from "react";
import { MOCK_NEWS, ALL_TOPICS, ALL_SENTIMENTS, STRUCTURED_SOURCE_TYPES } from "@/lib/mockData";
import { fetchNews, scoreSentimentBatch } from "@/lib/api";
import { FilterState, NewsItem, SentimentLabel, SourceType } from "@/types/news";
import Header from "@/components/Header";
import FilterSidebar from "@/components/FilterSidebar";
import NewsFeed from "@/components/NewsFeed";
import StatsBar from "@/components/StatsBar";
import TabNav, { TabId } from "@/components/TabNav";
import UnstructuredView from "@/components/UnstructuredView";

const DEFAULT_FILTERS: FilterState = {
  topics: new Set(ALL_TOPICS),
  sourceTypes: new Set(STRUCTURED_SOURCE_TYPES),
  sentiments: new Set(ALL_SENTIMENTS),
  tickers: new Set(),
  search: "",
  sortBy: "latest",
  limit: 100,
};

export default function HomePage() {
  const [items, setItems] = useState<NewsItem[]>([]);
  const [scoringPending, setScoringPending] = useState(true);
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
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

  // Fetch news from MongoDB (falls back to mock), then score unscored items with FinBERT.
  // Re-runs when committed limit changes.
  useEffect(() => {
    let cancelled = false;

    async function load() {
      const fetched = await fetchNews(filters.limit ?? 100);
      if (cancelled) return;
      setItems(fetched);

      // Only run FinBERT on structured items — social items use fast-path scoring
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
      setScoringPending(false);
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [filters.limit]);

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

  // Count how many pre-filtered items each ticker appears in.
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

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <Header itemCount={filtered.length} scoringPending={scoringPending} />
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
        <UnstructuredView items={socialItems} scoringPending={scoringPending} />
      )}
    </div>
  );
}
