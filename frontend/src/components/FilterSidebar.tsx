"use client";

import { useState, useEffect } from "react";
import { FilterState, SentimentLabel, SortBy, SourceType } from "@/types/news";
import { ALL_TOPICS, ALL_SENTIMENTS, STRUCTURED_SOURCE_TYPES } from "@/lib/mockData";

interface FilterSidebarProps {
  filters: FilterState;
  onChange: (f: FilterState) => void;
  tickerCounts: Map<string, number>;
}

const TOPIC_COLORS: Record<string, string> = {
  Crypto:      "text-purple-400",
  Energy:      "text-orange-400",
  Equities:    "text-blue-400",
  Macro:       "text-green-400",
  Regulatory:  "text-red-400",
  Bonds:       "text-yellow-400",
  Commodities: "text-amber-400",
  Technology:  "text-cyan-400",
  General:     "text-slate-400",
};

const SOURCE_LABELS: Record<SourceType, string> = {
  rss:    "RSS Feeds",
  sec:    "SEC EDGAR",
  fda:    "FDA",
  social: "Social",
};

const SENTIMENT_CONFIG: Record<SentimentLabel, { icon: string; cls: string }> = {
  bullish: { icon: "▲", cls: "text-emerald-400" },
  bearish: { icon: "▼", cls: "text-red-400" },
  neutral: { icon: "◆", cls: "text-slate-400" },
};

const SORT_OPTIONS: { value: SortBy; label: string }[] = [
  { value: "latest",     label: "Latest first"  },
  { value: "score_desc", label: "▲ Most bullish" },
  { value: "score_asc",  label: "▼ Most bearish" },
];

function toggle<T>(set: Set<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) { next.delete(value); } else { next.add(value); }
  return next;
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-5">
      <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-600 mb-2">
        {label}
      </p>
      <div className="space-y-1.5">{children}</div>
    </div>
  );
}

function FilterRow({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: () => void;
  label: React.ReactNode;
}) {
  return (
    <label className="flex items-center gap-2 cursor-pointer group">
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        className="accent-[#00d4aa] w-3 h-3 shrink-0"
      />
      <span className="text-xs group-hover:text-slate-200 transition-colors">{label}</span>
    </label>
  );
}

const MAX_LIMIT = 500;

export default function FilterSidebar({ filters, onChange, tickerCounts }: FilterSidebarProps) {
  const sortedTickers = Array.from(tickerCounts.entries()).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0])
  );

  const [pendingLimit, setPendingLimit] = useState(String(filters.limit ?? 100));

  // Sync input when filters are reset externally (e.g. "Reset all filters").
  useEffect(() => {
    setPendingLimit(String(filters.limit ?? 100));
  }, [filters.limit]);

  const parsedLimit = parseInt(pendingLimit, 10);
  const limitWarning =
    !isNaN(parsedLimit) && parsedLimit > MAX_LIMIT
      ? `Maximum is ${MAX_LIMIT} — will fetch ${MAX_LIMIT}`
      : !isNaN(parsedLimit) && parsedLimit < 1 && pendingLimit !== ""
      ? "Must be at least 1"
      : null;

  function commitLimit() {
    if (pendingLimit === "" || isNaN(parsedLimit)) {
      onChange({ ...filters, limit: null });
    } else if (parsedLimit < 1) {
      // invalid — warning shown, do nothing
    } else {
      const clamped = Math.min(parsedLimit, MAX_LIMIT);
      onChange({ ...filters, limit: clamped });
      setPendingLimit(String(clamped));
    }
  }
  return (
    <aside className="w-52 shrink-0 bg-[#0a0e1a] border-r border-[#1e2d4a] overflow-y-auto scrollbar-thin py-4 px-3">
      {/* Search */}
      <div className="mb-5">
        <input
          type="text"
          placeholder="Search headlines…"
          value={filters.search}
          onChange={(e) => onChange({ ...filters, search: e.target.value })}
          className="w-full bg-[#0f1629] border border-[#1e2d4a] rounded px-2.5 py-1.5 text-xs text-slate-300 placeholder-slate-600 focus:outline-none focus:border-[#00d4aa] transition-colors"
        />
      </div>

      {/* Limit */}
      <div className="mb-5">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-600 mb-2">
          Show recent
        </p>
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={1}
            placeholder="100"
            value={pendingLimit}
            onChange={(e) => setPendingLimit(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && commitLimit()}
            className="w-full bg-[#0f1629] border border-[#1e2d4a] rounded px-2.5 py-1.5 text-xs text-slate-300 placeholder-slate-600 focus:outline-none focus:border-[#00d4aa] transition-colors"
          />
          <button
            onClick={commitLimit}
            className="shrink-0 text-[10px] px-2 py-1.5 rounded bg-[#0f1629] border border-[#1e2d4a] text-[#00d4aa] hover:bg-[#00d4aa]/10 transition-colors"
          >
            Apply
          </button>
        </div>
        {limitWarning && (
          <p className="text-[10px] text-amber-400 mt-1">{limitWarning}</p>
        )}
      </div>

      <Section label="Sort By">
        {SORT_OPTIONS.map(({ value, label }) => (
          <label key={value} className="flex items-center gap-2 cursor-pointer group">
            <input
              type="radio"
              name="sortBy"
              checked={filters.sortBy === value}
              onChange={() => onChange({ ...filters, sortBy: value })}
              className="accent-[#00d4aa] w-3 h-3 shrink-0"
            />
            <span className="text-xs group-hover:text-slate-200 transition-colors">
              {label}
            </span>
          </label>
        ))}
      </Section>

      <Section label="Topics">
        {ALL_TOPICS.map((topic) => (
          <FilterRow
            key={topic}
            checked={filters.topics.has(topic)}
            onChange={() => onChange({ ...filters, topics: toggle(filters.topics, topic) })}
            label={<span className={TOPIC_COLORS[topic]}>{topic}</span>}
          />
        ))}
      </Section>

      <Section label="Source Type">
        {STRUCTURED_SOURCE_TYPES.map((st) => (
          <FilterRow
            key={st}
            checked={filters.sourceTypes.has(st)}
            onChange={() =>
              onChange({ ...filters, sourceTypes: toggle(filters.sourceTypes, st) })
            }
            label={<span className="text-slate-300">{SOURCE_LABELS[st]}</span>}
          />
        ))}
      </Section>

      <Section label="Sentiment">
        {ALL_SENTIMENTS.map((s) => {
          const { icon, cls } = SENTIMENT_CONFIG[s];
          return (
            <div key={s} className="flex items-center justify-between">
              <FilterRow
                checked={filters.sentiments.has(s)}
                onChange={() =>
                  onChange({ ...filters, sentiments: toggle(filters.sentiments, s) })
                }
                label={
                  <span className={`flex items-center gap-1 ${cls}`}>
                    <span className="text-[10px]">{icon}</span>
                    <span className="capitalize">{s}</span>
                  </span>
                }
              />
              <button
                onClick={() => onChange({ ...filters, sentiments: new Set([s]) })}
                title={`Show only ${s}`}
                className="text-[9px] text-slate-600 hover:text-slate-400 transition-colors px-1 shrink-0"
              >
                only
              </button>
            </div>
          );
        })}
      </Section>

      <Section label="Tickers">
        {sortedTickers.length === 0 ? (
          <p className="text-[10px] text-slate-600 italic">No tickers detected</p>
        ) : (
          sortedTickers.map(([ticker, count]) => (
            <FilterRow
              key={ticker}
              checked={filters.tickers.has(ticker)}
              onChange={() =>
                onChange({ ...filters, tickers: toggle(filters.tickers, ticker) })
              }
              label={
                <span className="flex items-center gap-1.5">
                  <span className="font-mono text-sky-400 text-[10px]">{ticker}</span>
                  <span className="text-slate-600 text-[10px]">({count})</span>
                </span>
              }
            />
          ))
        )}
        {filters.tickers.size > 0 && (
          <button
            onClick={() => onChange({ ...filters, tickers: new Set() })}
            className="text-[10px] text-slate-600 hover:text-[#00d4aa] transition-colors pt-1"
          >
            Clear ticker filter
          </button>
        )}
      </Section>

      <button
        onClick={() =>
          onChange({
            topics: new Set(ALL_TOPICS),
            sourceTypes: new Set(STRUCTURED_SOURCE_TYPES),
            sentiments: new Set(ALL_SENTIMENTS),
            tickers: new Set(),
            search: "",
            sortBy: "latest",
            limit: 100,
          })
        }
        className="w-full text-[11px] text-slate-600 hover:text-[#00d4aa] transition-colors py-1"
      >
        Reset all filters
      </button>
    </aside>
  );
}
