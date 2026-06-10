"use client";

import { FilterState, SentimentLabel, SourceType } from "@/types/news";
import { ALL_TOPICS, ALL_SOURCE_TYPES, ALL_SENTIMENTS } from "@/lib/mockData";

interface FilterSidebarProps {
  filters: FilterState;
  onChange: (f: FilterState) => void;
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
  rss: "RSS Feeds",
  sec: "SEC EDGAR",
  fda: "FDA",
};

const SENTIMENT_CONFIG: Record<SentimentLabel, { icon: string; cls: string }> = {
  bullish: { icon: "▲", cls: "text-emerald-400" },
  bearish: { icon: "▼", cls: "text-red-400" },
  neutral: { icon: "◆", cls: "text-slate-400" },
};

function toggle<T>(set: Set<T>, value: T): Set<T> {
  const next = new Set(set);
  next.has(value) ? next.delete(value) : next.add(value);
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

export default function FilterSidebar({ filters, onChange }: FilterSidebarProps) {
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
        {ALL_SOURCE_TYPES.map((st) => (
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
            <FilterRow
              key={s}
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
          );
        })}
      </Section>

      <button
        onClick={() =>
          onChange({
            topics: new Set(ALL_TOPICS),
            sourceTypes: new Set(ALL_SOURCE_TYPES),
            sentiments: new Set(ALL_SENTIMENTS),
            search: "",
          })
        }
        className="w-full text-[11px] text-slate-600 hover:text-[#00d4aa] transition-colors py-1"
      >
        Reset all filters
      </button>
    </aside>
  );
}
