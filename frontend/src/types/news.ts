export type SourceType = "rss" | "sec" | "fda";

export type SentimentLabel = "bullish" | "bearish" | "neutral";

export type TopicLabel =
  | "Crypto"
  | "Energy"
  | "Equities"
  | "Macro"
  | "Regulatory"
  | "Bonds"
  | "Commodities"
  | "Technology"
  | "General";

export interface SentimentResult {
  score: number;       // continuous [-1.0, 1.0]; negative = bearish
  label: SentimentLabel;
  confidence: number;  // [0.0, 1.0]
}

/** Mirrors the Python NewsItem dataclass produced by IngestionModule.py */
export interface NewsItem {
  id: string;
  source: string;
  source_type: SourceType;
  title: string;
  published_at: string;  // ISO 8601 UTC
  description: string;
  url: string;
  topic: string;         // comma-separated topic labels (TopicClassifier output)
  extra?: Record<string, unknown>;
  sentiment?: SentimentResult;
}

export interface SentimentSummary {
  scope: string;
  count: number;
  mean: number | null;
  min: number | null;
  max: number | null;
  label_counts: Record<SentimentLabel, number>;
  dominant_label: SentimentLabel | null;
  window_seconds: number;
}

export interface FilterState {
  topics: Set<string>;
  sourceTypes: Set<SourceType>;
  sentiments: Set<SentimentLabel>;
  search: string;
}
