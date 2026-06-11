"use client";

import { useState, useMemo, useEffect } from "react";
import { MOCK_NEWS, ALL_TOPICS, ALL_SOURCE_TYPES, ALL_SENTIMENTS } from "@/lib/mockData";
import { fetchNews, scoreSentimentBatch } from "@/lib/api";
import { FilterState, NewsItem, SentimentLabel, SourceType } from "@/types/news";
import Header from "@/components/Header";
import FilterSidebar from "@/components/FilterSidebar";
import NewsFeed from "@/components/NewsFeed";
import StatsBar from "@/components/StatsBar";

const DEFAULT_FILTERS: FilterState = {
  topics: new Set(ALL_TOPICS),
  sourceTypes: new Set(ALL_SOURCE_TYPES),
  sentiments: new Set(ALL_SENTIMENTS),
  tickers: new Set(),
  search: "",
  sortBy: "latest",
};

export default function HomePage() {
  const [items, setItems] = useState<NewsItem[]>([]);
  const [scoringPending, setScoringPending] = useState(true);
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);

  // On mount: fetch news from MongoDB (falls back to mock), then score with FinBERT.
  useEffect(() => {
    let cancelled = false;

    async function load() {
      const fetched = await fetchNews();
      if (cancelled) return;
      setItems(fetched);

      const scored = await scoreSentimentBatch(
        fetched.map((i) => ({ id: i.id, title: i.title, description: i.description }))
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
      setScoringPending(false);
    }

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  // All filters except tickers, plus sort — ticker counts are derived from this.
  const preTickerFiltered = useMemo(() => {
    const result = items.filter((item) => {
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
  }, [items, filters]);

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

  // Apply ticker filter last so counts above stay accurate.
  const filtered = useMemo(() => {
    if (filters.tickers.size === 0) return preTickerFiltered;
    return preTickerFiltered.filter((item) =>
      item.tickers?.some((t) => filters.tickers.has(t))
    );
  }, [preTickerFiltered, filters.tickers]);

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <Header itemCount={filtered.length} scoringPending={scoringPending} />
      <StatsBar items={filtered} />
      <div className="flex flex-1 overflow-hidden">
        <FilterSidebar filters={filters} onChange={setFilters} tickerCounts={tickerCounts} />
        <NewsFeed items={filtered} scoringPending={scoringPending} />
      </div>
    </div>
  );
}
