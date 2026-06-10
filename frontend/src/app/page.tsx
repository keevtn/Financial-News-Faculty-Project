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
  search: "",
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

  const filtered = useMemo(() => {
    return items.filter((item) => {
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
  }, [items, filters]);

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <Header itemCount={filtered.length} scoringPending={scoringPending} />
      <StatsBar items={filtered} />
      <div className="flex flex-1 overflow-hidden">
        <FilterSidebar filters={filters} onChange={setFilters} />
        <NewsFeed items={filtered} scoringPending={scoringPending} />
      </div>
    </div>
  );
}
