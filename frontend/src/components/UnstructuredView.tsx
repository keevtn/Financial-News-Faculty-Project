"use client";

import { useState, useEffect } from "react";
import { NewsItem, SentimentLabel, SocialFilterState, SortBy } from "@/types/news";
import { ALL_SENTIMENTS, ALL_PLATFORMS } from "@/lib/mockData";
import SocialFeed from "@/components/SocialFeed";
import StatsBar from "@/components/StatsBar";
import GossipPanel from "@/components/GossipPanel";

// ── Constants ──────────────────────────────────────────────────────────────

const MAX_LIMIT = 500;

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

const PLATFORM_COLOR: Record<string, string> = {
  Reddit:     "text-orange-400",
  StockTwits: "text-sky-400",
  Bluesky:    "text-violet-400",
};

// ── Shared sub-components ──────────────────────────────────────────────────

function toggle<T>(set: Set<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) { next.delete(value); } else { next.add(value); }
  return next;
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-5">
      <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-400 mb-2">
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

// ── Social filter sidebar ──────────────────────────────────────────────────

interface SidebarProps {
  filters: SocialFilterState;
  onChange: (f: SocialFilterState) => void;
  tickerCounts: Map<string, number>;
}

function SocialFilterSidebar({ filters, onChange, tickerCounts }: SidebarProps) {
  const sortedTickers = Array.from(tickerCounts.entries()).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0])
  );

  const [pendingLimit, setPendingLimit] = useState(String(filters.limit ?? 100));

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
    <aside aria-label="Social filters" className="w-52 shrink-0 bg-[#0a0e1a] border-r border-[#1e2d4a] overflow-y-auto scrollbar-thin py-4 px-3">
      {/* Search */}
      <div className="mb-5">
        <input
          type="text"
          aria-label="Search posts"
          placeholder="Search posts…"
          value={filters.search}
          onChange={(e) => onChange({ ...filters, search: e.target.value })}
          className="w-full bg-[#0f1629] border border-[#1e2d4a] rounded px-2.5 py-1.5 text-xs text-slate-300 placeholder-slate-400 focus:outline-none focus:border-[#00d4aa] transition-colors"
        />
      </div>

      {/* Limit */}
      <div className="mb-5">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-400 mb-2">
          Show recent
        </p>
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={1}
            aria-label="Show recent: number of posts to load"
            placeholder="100"
            value={pendingLimit}
            onChange={(e) => setPendingLimit(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && commitLimit()}
            className="w-full bg-[#0f1629] border border-[#1e2d4a] rounded px-2.5 py-1.5 text-xs text-slate-300 placeholder-slate-400 focus:outline-none focus:border-[#00d4aa] transition-colors"
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
              name="social-sortBy"
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

      <Section label="Platform">
        {ALL_PLATFORMS.map((platform) => (
          <FilterRow
            key={platform}
            checked={filters.platforms.has(platform)}
            onChange={() => onChange({ ...filters, platforms: toggle(filters.platforms, platform) })}
            label={
              <span className={PLATFORM_COLOR[platform] ?? "text-slate-300"}>
                {platform}
              </span>
            }
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
                aria-label={`Show only ${s} posts`}
                title={`Show only ${s}`}
                className="text-[10px] text-slate-400 hover:text-[#00d4aa] transition-colors px-1 shrink-0"
              >
                only
              </button>
            </div>
          );
        })}
      </Section>

      <Section label="Tickers">
        {sortedTickers.length === 0 ? (
          <p className="text-[10px] text-slate-400 italic">No tickers detected</p>
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
                  <span className="text-slate-400 text-[10px]">({count})</span>
                </span>
              }
            />
          ))
        )}
        {filters.tickers.size > 0 && (
          <button
            onClick={() => onChange({ ...filters, tickers: new Set() })}
            className="text-[10px] text-slate-400 hover:text-[#00d4aa] transition-colors pt-1"
          >
            Clear ticker filter
          </button>
        )}
      </Section>

      <button
        onClick={() =>
          onChange({
            search: "",
            sentiments: new Set(ALL_SENTIMENTS),
            tickers: new Set(),
            platforms: new Set(ALL_PLATFORMS),
            sortBy: "latest",
            limit: 100,
          })
        }
        className="w-full text-[11px] text-slate-400 hover:text-[#00d4aa] transition-colors py-1"
      >
        Reset all filters
      </button>
    </aside>
  );
}

// ── Empty state ────────────────────────────────────────────────────────────

const PLANNED_SOURCES = [
  {
    label: "Reddit",
    sub: "r/wallstreetbets · r/investing · r/stocks · r/SecurityAnalysis · and 7 more",
    color: "text-orange-400",
    border: "border-orange-400/20",
    bg: "bg-orange-400/5",
    status: "Live (RSS)",
  },
  {
    label: "StockTwits",
    sub: "22-ticker watchlist — SPY, QQQ, NVDA, BTC.X, ETH.X and more",
    color: "text-sky-400",
    border: "border-sky-400/20",
    bg: "bg-sky-400/5",
    status: "Live",
  },
  {
    label: "Bluesky",
    sub: "27 financial hashtags — #stocks, #crypto, #earnings, #inflation and more",
    color: "text-violet-400",
    border: "border-violet-400/20",
    bg: "bg-violet-400/5",
    status: "Live",
  },
];

function EmptyState() {
  return (
    <div className="flex-1 overflow-y-auto bg-[#080d1a] px-8 py-10">
      <div className="max-w-3xl mx-auto">
        <div className="mb-8">
          <h2 className="text-sm font-bold text-slate-100 tracking-wide uppercase mb-1">
            Unstructured News
          </h2>
          <p className="text-xs text-slate-400 leading-relaxed">
            Social media sources — Reddit threads, StockTwits messages, and Bluesky posts.
            Start the ingestion pipeline with{" "}
            <code className="text-[#00d4aa] bg-[#0f1629] px-1 py-0.5 rounded text-[10px]">
              --stocktwits --bluesky
            </code>{" "}
            to see live social data here.
          </p>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-8">
          {PLANNED_SOURCES.map((src) => (
            <div
              key={src.label}
              className={`rounded-lg border ${src.border} ${src.bg} px-4 py-3 flex flex-col gap-1`}
            >
              <div className="flex items-center gap-2">
                <span className={`text-xs font-semibold ${src.color} tracking-wide`}>
                  {src.label}
                </span>
                <span
                  className={`ml-auto text-[10px] font-medium px-1.5 py-0.5 rounded ${
                    src.status.startsWith("Live")
                      ? "text-[#00d4aa] bg-[#00d4aa]/10"
                      : "text-slate-400 bg-[#1e2d4a]"
                  }`}
                >
                  {src.status}
                </span>
              </div>
              <p className="text-[11px] text-slate-400 leading-snug">{src.sub}</p>
            </div>
          ))}
        </div>

        <div className="rounded-lg border border-[#1e2d4a] bg-[#0f1629] px-5 py-4">
          <p className="text-xs text-slate-400 leading-relaxed">
            <span className="text-slate-300 font-semibold">Run command</span> —{" "}
            from <code className="text-[10px] text-slate-400">backend/</code>:
          </p>
          <pre className="mt-2 text-[11px] text-[#00d4aa] bg-[#080d1a] rounded px-3 py-2 overflow-x-auto">
            python run_ingest.py --rss --stocktwits --bluesky --mongo
          </pre>
        </div>
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────

interface UnstructuredViewProps {
  items: NewsItem[];
  totalCount: number;
  filters: SocialFilterState;
  onChange: (f: SocialFilterState) => void;
  tickerCounts: Map<string, number>;
  scoringPending?: boolean;
}

export default function UnstructuredView({
  items,
  totalCount,
  filters,
  onChange,
  tickerCounts,
  scoringPending,
}: UnstructuredViewProps) {
  if (totalCount === 0) {
    return <EmptyState />;
  }

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      <StatsBar items={items} />
      <div className="flex flex-1 overflow-hidden">
        <SocialFilterSidebar filters={filters} onChange={onChange} tickerCounts={tickerCounts} />
        <SocialFeed items={items} scoringPending={scoringPending} />
        <GossipPanel />
      </div>
    </div>
  );
}
