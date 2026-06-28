"use client";

import { useEffect, useState } from "react";
import { GossipItem, GossipResult, fetchGossip } from "@/lib/api";
import ChartIconButton from "./ChartIconButton";

// Velocity is the headline — hotter color the more it accelerates.
function velColor(v: number): string {
  if (v >= 5) return "text-amber-300";
  if (v >= 2) return "text-amber-400";
  return "text-slate-300";
}

const DIR_COLOR: Record<string, string> = {
  bullish: "text-emerald-400",
  bearish: "text-red-400",
  neutral: "text-slate-400",
};

function GossipRow({ item }: { item: GossipItem }) {
  return (
    <li className="flex items-center gap-2 px-3 py-2 border-b border-[#1e2d4a]/50 last:border-0 hover:bg-[#0f1629] transition-colors">
      <span className="w-4 text-[10px] font-mono text-slate-500 shrink-0">{item.rank}</span>
      <span className="inline-flex items-center gap-1 min-w-0">
        <a
          href={`https://finance.yahoo.com/quote/${item.ticker}`}
          target="_blank"
          rel="noopener noreferrer"
          className="font-mono font-bold text-sky-400 hover:text-[#00d4aa] transition-colors truncate"
        >
          {item.ticker}
        </a>
        <ChartIconButton ticker={item.ticker} />
      </span>
      <span className="ml-auto text-right shrink-0">
        <span className={`font-mono font-bold text-sm ${velColor(item.velocity)}`}>
          {item.velocity.toFixed(1)}×
        </span>
        <span className="block text-[10px] text-slate-400">
          {item.recent_count} recent · <span className={DIR_COLOR[item.direction]}>{item.direction}</span>
        </span>
      </span>
    </li>
  );
}

function About({ params }: { params?: GossipResult["params"] }) {
  const rh = params ? `${params.recent_hours}h` : "~6h";
  const bd = params ? `${params.baseline_days}d` : "7d";
  return (
    <div className="px-3 py-3 text-[10px] text-slate-400 leading-relaxed border-b border-[#1e2d4a] bg-[#0a0f1e]">
      <p className="mb-2">
        Surfaces tickers whose social chatter is <span className="text-slate-200 font-semibold">accelerating</span> —
        not just loud. A name that&apos;s <em>always</em> discussed (e.g. BTC) has high volume but low velocity; a name
        <span className="text-slate-200"> suddenly</span> trending has high velocity. That spike is the gossip.
      </p>
      <ul className="space-y-1.5">
        <li>
          <span className="font-mono text-amber-400">N×</span>{" "}
          <span className="text-slate-300 font-semibold">Velocity</span> — recent mention rate vs this
          ticker&apos;s own {bd} baseline. <span className="font-mono">24×</span> = mentioned 24× more than normal.
        </li>
        <li>
          <span className="text-slate-300 font-semibold">Recent</span> — mentions in the last {rh}.
        </li>
        <li>
          <span className="text-slate-300 font-semibold">Direction</span> — bullish / bearish lean of that recent chatter.
        </li>
        <li>Source: Bluesky cashtag mentions, spam-filtered (a 20-ticker post counts ~nothing).</li>
        <li>Feeds the Squeeze tab&apos;s ignition as the social-acceleration signal.</li>
      </ul>
    </div>
  );
}

export default function GossipPanel() {
  const [result, setResult] = useState<GossipResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [showAbout, setShowAbout] = useState(false);
  const [recentHours, setRecentHours] = useState(6);
  const [baselineDays, setBaselineDays] = useState(7);
  const [tick, setTick] = useState(0); // manual refresh

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchGossip(recentHours, baselineDays).then((r) => {
      if (!cancelled) {
        setResult(r);
        setLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [recentHours, baselineDays, tick]);

  const items = result?.items ?? [];

  return (
    <aside
      aria-label="Gossip — accelerating social chatter"
      className="hidden lg:flex flex-col w-72 shrink-0 bg-[#0a0e1a] border-l border-[#1e2d4a] overflow-y-auto scrollbar-thin"
    >
      <div className="sticky top-0 z-10 bg-[#0a0e1a] border-b border-[#1e2d4a] px-3 py-2.5">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-bold text-slate-100">Gossip</h3>
          <button
            onClick={() => setShowAbout((s) => !s)}
            aria-expanded={showAbout}
            aria-label="What is this panel?"
            className="text-[10px] w-4 h-4 inline-flex items-center justify-center rounded-full border border-[#1e2d4a] text-slate-400 hover:text-[#00d4aa] hover:border-[#2d4470] transition-colors"
          >
            ?
          </button>
          <button
            onClick={() => setTick((t) => t + 1)}
            disabled={loading}
            aria-label="Refresh"
            className="ml-auto text-[10px] uppercase tracking-wider text-slate-400 hover:text-[#00d4aa] transition-colors disabled:opacity-50"
          >
            {loading ? "…" : "↻"}
          </button>
        </div>
        {/* recency window: recent vs baseline */}
        <div className="flex items-center gap-1.5 mt-1.5 text-[10px] text-slate-400">
          <span>Recent</span>
          <select
            aria-label="Recent window"
            value={recentHours}
            onChange={(e) => setRecentHours(Number(e.target.value))}
            className="bg-[#0f1629] border border-[#1e2d4a] rounded px-1 py-0.5 text-slate-200 focus:outline-none focus:border-[#00d4aa]"
          >
            {[1, 2, 4, 6, 12, 24].map((h) => (
              <option key={h} value={h}>{h}h</option>
            ))}
          </select>
          <span>vs</span>
          <select
            aria-label="Baseline window"
            value={baselineDays}
            onChange={(e) => setBaselineDays(Number(e.target.value))}
            className="bg-[#0f1629] border border-[#1e2d4a] rounded px-1 py-0.5 text-slate-200 focus:outline-none focus:border-[#00d4aa]"
          >
            {[3, 7, 14, 30].map((d) => (
              <option key={d} value={d}>{d}d</option>
            ))}
          </select>
          <span>baseline</span>
        </div>
      </div>

      {showAbout && <About params={result?.params} />}

      {loading && !result ? (
        <p className="text-[10px] text-slate-400 px-3 py-4">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-[10px] text-slate-400 px-3 py-4 leading-relaxed">
          No accelerating chatter right now. Needs social ingestion (<span className="font-mono">RUN_SOCIAL</span>)
          banking posts — fills in as the stream grows.
        </p>
      ) : (
        <ul>
          {items.map((it) => (
            <GossipRow key={it.ticker} item={it} />
          ))}
        </ul>
      )}
    </aside>
  );
}
