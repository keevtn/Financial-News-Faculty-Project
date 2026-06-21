"use client";

import { useEffect, useState } from "react";
import {
  CatalystItem,
  CatalystRanking,
  Direction,
  fetchLatestCatalystRanking,
} from "@/lib/api";
import { formatDistanceToNow } from "@/lib/time";

// ── Visual config ───────────────────────────────────────────────────────────

const DIR: Record<
  Direction,
  { icon: string; text: string; bg: string; border: string; bar: string }
> = {
  bullish: { icon: "▲", text: "text-emerald-400", bg: "bg-emerald-500/10", border: "border-emerald-800", bar: "bg-emerald-500" },
  bearish: { icon: "▼", text: "text-red-400",     bg: "bg-red-500/10",     border: "border-red-900",     bar: "bg-red-500" },
  neutral: { icon: "◆", text: "text-slate-400",   bg: "bg-slate-700/30",   border: "border-slate-700",   bar: "bg-slate-500" },
};

const SOURCE_TYPE_CHIP: Record<string, string> = {
  sec:    "bg-amber-500/10 text-amber-400 border-amber-800",
  fda:    "bg-rose-500/10 text-rose-400 border-rose-900",
  rss:    "bg-slate-600/20 text-slate-400 border-slate-700",
  social: "bg-violet-500/10 text-violet-400 border-violet-900",
};

function fmtMarketCap(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1e12) return `$${(n / 1e12).toFixed(2)}T`;
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(0)}M`;
  return `$${n}`;
}

function shortDateTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SubscoreBar({ label, value }: { label: string; value: number | null }) {
  const pct = value == null ? 0 : Math.round(value * 100);
  return (
    <div className="flex items-center gap-2">
      <span className="w-16 text-[10px] uppercase tracking-wider text-slate-500 shrink-0">{label}</span>
      <div className="flex-1 h-1 bg-[#1e2d4a] rounded-full overflow-hidden">
        <div className="h-full bg-[#00d4aa]/70" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-7 text-right text-[10px] font-mono text-slate-500">{value == null ? "–" : pct}</span>
    </div>
  );
}

function CatalystCard({ item }: { item: CatalystItem }) {
  const [open, setOpen] = useState(false);
  const dir = DIR[item.direction];
  const score = Math.max(0, Math.min(100, item.catalyst_score));

  return (
    <article className="bg-[#0f1629] border border-[#1e2d4a] rounded-lg overflow-hidden hover:border-[#2d4470] transition-colors">
      <div className="flex">
        {/* rank rail */}
        <div className="flex flex-col items-center justify-center w-12 shrink-0 bg-[#0a0f1e] border-r border-[#1e2d4a] py-4">
          <span className="text-[10px] uppercase tracking-widest text-slate-600">Rank</span>
          <span className="text-xl font-bold text-slate-200 leading-none mt-1">{item.rank}</span>
        </div>

        <div className="flex-1 p-4 flex flex-col gap-3 min-w-0">
          {/* header: ticker + direction + score */}
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-2.5">
              <a
                href={`https://finance.yahoo.com/quote/${item.ticker}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-lg font-bold font-mono text-slate-100 hover:text-[#00d4aa] transition-colors"
              >
                {item.ticker}
              </a>
              <span
                className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded border ${dir.bg} ${dir.text} ${dir.border}`}
              >
                {dir.icon} {item.direction}
              </span>
              <span className="text-[10px] text-slate-500">
                conf {(item.confidence * 100).toFixed(0)}%
              </span>
            </div>

            <div className="flex flex-col items-end shrink-0">
              <span className="text-[10px] uppercase tracking-widest text-slate-600">Catalyst</span>
              <span className={`text-xl font-bold font-mono leading-none ${dir.text}`}>
                {item.catalyst_score.toFixed(0)}
              </span>
            </div>
          </div>

          {/* score bar */}
          <div className="h-1.5 bg-[#1e2d4a] rounded-full overflow-hidden">
            <div className={`h-full ${dir.bar} transition-all duration-500`} style={{ width: `${score}%` }} />
          </div>

          {/* rationale */}
          <p className="text-xs text-slate-300 leading-relaxed">{item.rationale}</p>

          {/* meta chips */}
          <div className="flex flex-wrap items-center gap-2 text-[10px]">
            <span className="text-slate-500">
              <span className="font-mono text-slate-300">{item.n_stories}</span> stories
            </span>
            <span className="text-slate-700">·</span>
            <span className="text-slate-500">
              <span className="font-mono text-slate-300">{item.n_sources}</span> sources
            </span>
            <span className="text-slate-700">·</span>
            <span className="text-slate-500">
              <span className="font-mono text-slate-300">{item.abnormal_attention}×</span> normal attention
            </span>
            {item.market_cap != null && (
              <>
                <span className="text-slate-700">·</span>
                <span className="text-slate-500">
                  <span className="font-mono text-slate-300">{fmtMarketCap(item.market_cap)}</span> mkt cap
                </span>
              </>
            )}
            <span className="flex gap-1 ml-1">
              {item.source_types.map((st) => (
                <span
                  key={st}
                  className={`px-1.5 py-0.5 rounded border uppercase font-semibold tracking-wider ${SOURCE_TYPE_CHIP[st] ?? SOURCE_TYPE_CHIP.rss}`}
                >
                  {st}
                </span>
              ))}
            </span>
          </div>

          {/* LLM sub-scores */}
          {item.llm_subscores && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-1.5 pt-1">
              <SubscoreBar label="Material" value={item.llm_subscores.materiality} />
              <SubscoreBar label="Surprise" value={item.llm_subscores.surprise} />
              <SubscoreBar label="Sentiment" value={item.llm_subscores.sentiment_strength} />
              <SubscoreBar label="Breadth" value={item.llm_subscores.breadth} />
            </div>
          )}

          {/* sources toggle */}
          {item.sample_articles.length > 0 && (
            <div className="pt-1">
              <button
                onClick={() => setOpen((o) => !o)}
                className="text-[10px] uppercase tracking-wider text-slate-500 hover:text-[#00d4aa] transition-colors"
              >
                {open ? "▾ Hide" : "▸ Show"} {item.sample_articles.length} source
                {item.sample_articles.length === 1 ? "" : "s"}
              </button>
              {open && (
                <ul className="mt-2 flex flex-col gap-2 border-l border-[#1e2d4a] pl-3">
                  {item.sample_articles.map((a, i) => {
                    const isLink = a.url && a.url !== "#";
                    return (
                      <li key={i} className="flex flex-col gap-0.5">
                        <div className="flex items-center gap-2 text-[10px]">
                          <span
                            className={`px-1 rounded border uppercase font-semibold tracking-wider ${SOURCE_TYPE_CHIP[a.source_type] ?? SOURCE_TYPE_CHIP.rss}`}
                          >
                            {a.source_type}
                          </span>
                          <span className="text-slate-500 truncate">{a.source}</span>
                          {a.reprints > 1 && (
                            <span className="text-slate-600">+{a.reprints - 1} reprints</span>
                          )}
                        </div>
                        {isLink ? (
                          <a
                            href={a.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-xs text-slate-300 hover:text-[#00d4aa] transition-colors leading-snug"
                          >
                            {a.title}
                          </a>
                        ) : (
                          <span className="text-xs text-slate-300 leading-snug">{a.title}</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>
    </article>
  );
}

// ── Main view ─────────────────────────────────────────────────────────────────

export default function CatalystView() {
  const [ranking, setRanking] = useState<CatalystRanking | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    const r = await fetchLatestCatalystRanking();
    setRanking(r);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <div className="flex-1 overflow-y-auto scrollbar-thin">
      {/* header strip */}
      <div className="bg-[#0f1629] border-b border-[#1e2d4a] px-6 py-3 flex items-center gap-4 flex-wrap sticky top-0 z-10">
        <div className="flex flex-col">
          <h2 className="text-sm font-bold text-slate-100">Pre-Market Catalyst Ranking</h2>
          <span className="text-[10px] text-slate-500">
            Tickers ranked by the strength of overnight news catalysts
          </span>
        </div>

        {ranking && (
          <div className="flex items-center gap-3 text-[10px] text-slate-500 flex-wrap">
            <span
              className={`px-1.5 py-0.5 rounded border font-semibold uppercase tracking-wider ${
                ranking.used_llm
                  ? "bg-[#00d4aa]/10 text-[#00d4aa] border-[#00d4aa]/40"
                  : "bg-slate-700/30 text-slate-400 border-slate-700"
              }`}
              title={ranking.llm_status ?? undefined}
            >
              {ranking.used_llm ? `AI · ${ranking.model}` : "Quantitative"}
            </span>
            <span>
              window {shortDateTime(ranking.window_start)} → {shortDateTime(ranking.window_end)}
            </span>
            <span className="text-slate-700">·</span>
            <span>
              <span className="font-mono text-slate-300">{ranking.candidate_count}</span> candidates from{" "}
              <span className="font-mono text-slate-300">{ranking.doc_count}</span> docs
            </span>
            <span className="text-slate-700">·</span>
            <span>generated {formatDistanceToNow(ranking.generated_at)}</span>
          </div>
        )}

        <button
          onClick={load}
          disabled={loading}
          className="ml-auto text-[10px] uppercase tracking-wider px-2.5 py-1 rounded border border-[#1e2d4a] text-slate-400 hover:text-[#00d4aa] hover:border-[#2d4470] transition-colors disabled:opacity-50"
        >
          {loading ? "Loading…" : "↻ Refresh"}
        </button>
      </div>

      {/* body */}
      <div className="p-6">
        {loading && !ranking ? (
          <p className="text-sm text-slate-500">Loading latest ranking…</p>
        ) : !ranking ? (
          <div className="max-w-lg mx-auto text-center py-16">
            <p className="text-sm text-slate-300 font-semibold">No ranking generated yet</p>
            <p className="text-xs text-slate-500 mt-2 leading-relaxed">
              Catalyst rankings are produced on demand by the backend. Trigger one with a
              <span className="font-mono text-slate-400"> POST /api/catalyst/run</span> (key-protected),
              ideally before the market opens, then refresh this view.
            </p>
          </div>
        ) : ranking.items.length === 0 ? (
          <p className="text-sm text-slate-500">
            The latest run found no qualifying catalysts in the overnight window.
          </p>
        ) : (
          <div className="flex flex-col gap-3 max-w-3xl mx-auto">
            {ranking.items.map((item) => (
              <CatalystCard key={item.ticker} item={item} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
