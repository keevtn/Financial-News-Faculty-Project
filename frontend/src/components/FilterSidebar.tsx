"use client";

import { useState, useEffect } from "react";
import { FilterState, SentimentLabel, SortBy, SourceType } from "@/types/news";
import { ALL_TOPICS, ALL_SENTIMENTS, STRUCTURED_SOURCE_TYPES } from "@/lib/mockData";

interface FilterSidebarProps {
  filters: FilterState;
  onChange: (f: FilterState) => void;
  tickerCounts: Map<string, number>;
  sourceCounts: Map<string, number>;
  /** Mobile drawer open state. Ignored at md+ where the sidebar is always shown. */
  open?: boolean;
  onClose?: () => void;
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

const MAX_LIMIT = 500;

/**
 * Compact multi-select: a fixed-height scroll box with a mini search input,
 * so long lists (48 feeds, dozens of tickers) never blow up sidebar height.
 * Empty selection means "no filter" (show everything).
 */
function ScrollBoxSection({
  label,
  counts,
  selected,
  onToggle,
  onClear,
  renderName,
  searchPlaceholder,
  emptyText,
}: {
  label: string;
  counts: Map<string, number>;
  selected: Set<string>;
  onToggle: (value: string) => void;
  onClear: () => void;
  renderName: (name: string) => React.ReactNode;
  searchPlaceholder: string;
  emptyText: string;
}) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const entries = Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .filter(([name]) => !q || name.toLowerCase().includes(q));

  return (
    <div className="mb-5">
      <div className="flex items-center justify-between mb-2">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-400">
          {label}
          {selected.size > 0 && (
            <span className="ml-1.5 text-[#00d4aa] normal-case tracking-normal">
              ({selected.size})
            </span>
          )}
        </p>
        {selected.size > 0 && (
          <button
            onClick={onClear}
            className="text-[10px] text-slate-400 hover:text-[#00d4aa] transition-colors shrink-0"
          >
            clear
          </button>
        )}
      </div>
      <input
        type="text"
        aria-label={`Filter ${label.toLowerCase()} list`}
        placeholder={searchPlaceholder}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        className="w-full mb-1.5 bg-[#0f1629] border border-[#1e2d4a] rounded px-2 py-1 text-[11px] text-slate-300 placeholder-slate-500 focus:outline-none focus:border-[#00d4aa] transition-colors"
      />
      <div className="max-h-36 overflow-y-auto scrollbar-thin border border-[#1e2d4a] rounded bg-[#0f1629]/50 px-2 py-1.5 space-y-1">
        {counts.size === 0 ? (
          <p className="text-[10px] text-slate-400 italic">{emptyText}</p>
        ) : entries.length === 0 ? (
          <p className="text-[10px] text-slate-400 italic">No match for “{query}”</p>
        ) : (
          entries.map(([name, count]) => (
            <FilterRow
              key={name}
              checked={selected.has(name)}
              onChange={() => onToggle(name)}
              label={
                <span className="flex items-center gap-1.5 min-w-0">
                  {renderName(name)}
                  <span className="text-slate-400 text-[10px] shrink-0">({count})</span>
                </span>
              }
            />
          ))
        )}
      </div>
    </div>
  );
}

export default function FilterSidebar({
  filters,
  onChange,
  tickerCounts,
  sourceCounts,
  open = false,
  onClose,
}: FilterSidebarProps) {
  const [pendingLimit, setPendingLimit] = useState(String(filters.limit ?? 100));

  // Sync input when filters are reset externally (e.g. "Reset all filters").
  useEffect(() => {
    setPendingLimit(String(filters.limit ?? 100));
  }, [filters.limit]);

  // Close the mobile drawer on Escape.
  useEffect(() => {
    if (!open || !onClose) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

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
    <>
      {/* Mobile backdrop — tap to dismiss the drawer. */}
      {open && (
        <div
          onClick={onClose}
          aria-hidden="true"
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
        />
      )}
      <aside
        aria-label="News filters"
        className={[
          // Mobile: off-canvas drawer that slides in from the left.
          "fixed inset-y-0 left-0 z-50 w-72 max-w-[85vw] transform transition-transform duration-200 ease-out",
          open ? "translate-x-0" : "-translate-x-full",
          // md+: static in-flow column, always visible.
          "md:static md:z-auto md:w-52 md:max-w-none md:translate-x-0 md:transition-none",
          "shrink-0 bg-[#0a0e1a] border-r border-[#1e2d4a] overflow-y-auto scrollbar-thin py-4 px-3",
        ].join(" ")}
      >
      {/* Drawer header (mobile only) */}
      <div className="flex items-center justify-between mb-4 md:hidden">
        <span className="text-xs font-semibold uppercase tracking-widest text-slate-300">
          Filters
        </span>
        <button
          onClick={onClose}
          aria-label="Close filters"
          className="text-slate-400 hover:text-[#00d4aa] text-xl leading-none px-2 -mr-1"
        >
          <span aria-hidden="true">×</span>
        </button>
      </div>

      {/* Search */}
      <div className="mb-5">
        <input
          type="text"
          aria-label="Search headlines"
          placeholder="Search headlines…"
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
            aria-label="Show recent: number of articles to load"
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
                aria-label={`Show only ${s} items`}
                title={`Show only ${s}`}
                className="text-[10px] text-slate-400 hover:text-[#00d4aa] transition-colors px-1 shrink-0"
              >
                only
              </button>
            </div>
          );
        })}
      </Section>

      <ScrollBoxSection
        label="Feeds"
        counts={sourceCounts}
        selected={filters.sources}
        onToggle={(name) =>
          onChange({ ...filters, sources: toggle(filters.sources, name) })
        }
        onClear={() => onChange({ ...filters, sources: new Set() })}
        renderName={(name) => (
          <span className="text-slate-300 text-[11px] truncate">{name}</span>
        )}
        searchPlaceholder="Find feed…"
        emptyText="No feeds loaded"
      />

      <ScrollBoxSection
        label="Tickers"
        counts={tickerCounts}
        selected={filters.tickers}
        onToggle={(name) =>
          onChange({ ...filters, tickers: toggle(filters.tickers, name) })
        }
        onClear={() => onChange({ ...filters, tickers: new Set() })}
        renderName={(name) => (
          <span className="font-mono text-sky-400 text-[10px]">{name}</span>
        )}
        searchPlaceholder="Find ticker…"
        emptyText="No tickers detected"
      />

      <button
        onClick={() =>
          onChange({
            topics: new Set(ALL_TOPICS),
            sourceTypes: new Set(STRUCTURED_SOURCE_TYPES),
            sentiments: new Set(ALL_SENTIMENTS),
            sources: new Set(),
            tickers: new Set(),
            search: "",
            sortBy: "latest",
            limit: 100,
          })
        }
        className="w-full text-[11px] text-slate-400 hover:text-[#00d4aa] transition-colors py-1"
      >
        Reset all filters
      </button>
      </aside>
    </>
  );
}
