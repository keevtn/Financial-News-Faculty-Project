"use client";

import { useEffect, useState } from "react";
import { Direction, GossipItem, GossipResult, fetchGossip } from "@/lib/api";
import { formatDistanceToNow } from "@/lib/time";
import ChartIconButton from "./ChartIconButton";

const DIR: Record<Direction, { icon: string; cls: string }> = {
  bullish: { icon: "▲", cls: "text-emerald-400" },
  bearish: { icon: "▼", cls: "text-red-400" },
  neutral: { icon: "·", cls: "text-slate-400" },
};

// Velocity is the headline: hotter color the more it accelerates.
function velColor(v: number): string {
  if (v >= 5) return "text-amber-300";
  if (v >= 2) return "text-amber-400";
  return "text-slate-300";
}

function GossipRow({ item }: { item: GossipItem }) {
  const dir = DIR[item.direction];
  const score = Math.max(0, Math.min(100, item.gossip_score));
  return (
    <tr className="border-b border-[#1e2d4a]/60 last:border-0 hover:bg-[#0f1629] transition-colors">
      <td className="px-3 py-2 text-slate-400 font-mono">{item.rank}</td>
      <td className="px-3 py-2">
        <span className="inline-flex items-center gap-1.5">
          <a
            href={`https://finance.yahoo.com/quote/${item.ticker}`}
            target="_blank"
            rel="noopener noreferrer"
            className="font-mono font-bold text-sky-400 hover:text-[#00d4aa] transition-colors"
          >
            {item.ticker}
          </a>
          <ChartIconButton ticker={item.ticker} />
        </span>
      </td>
      <td className={`px-3 py-2 text-right font-mono font-bold ${velColor(item.velocity)}`}>
        {item.velocity.toFixed(1)}×
      </td>
      <td className="px-3 py-2 text-right font-mono text-slate-300">{item.recent_count}</td>
      <td className="px-3 py-2 text-right font-mono text-slate-400">{item.baseline_rate.toFixed(1)}</td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          <div className="flex-1 h-1.5 bg-[#1e2d4a] rounded-full overflow-hidden min-w-[40px]">
            <div className="h-full bg-[#00d4aa]" style={{ width: `${score}%` }} />
          </div>
          <span className="w-7 text-right font-mono text-[10px] text-slate-400">{score.toFixed(0)}</span>
        </div>
      </td>
      <td className={`px-3 py-2 text-center font-mono ${dir.cls}`} title={item.direction}>
        {dir.icon}
      </td>
    </tr>
  );
}

export default function GossipView() {
  const [result, setResult] = useState<GossipResult | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    setResult(await fetchGossip());
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  const items = result?.items ?? [];

  return (
    <div className="flex-1 overflow-y-auto scrollbar-thin">
      {/* header strip */}
      <div className="bg-[#0f1629] border-b border-[#1e2d4a] px-6 py-3 flex items-center gap-4 flex-wrap sticky top-0 z-10">
        <div className="flex flex-col">
          <h2 className="text-sm font-bold text-slate-100">Gossip — Accelerating Chatter</h2>
          <span className="text-[10px] text-slate-400">
            Tickers being mentioned far more in the last{" "}
            {result ? `${result.params.recent_hours}h` : "few hours"} than their{" "}
            {result ? `${result.params.baseline_days}d` : "trailing"} baseline (Bluesky)
          </span>
        </div>

        {result && (
          <div className="flex items-center gap-3 text-[10px] text-slate-400 flex-wrap">
            <span>
              <span className="font-mono text-slate-300">{result.post_count}</span> posts ·{" "}
              <span className="font-mono text-slate-300">{result.ticker_count}</span> tickers
            </span>
            <span className="text-slate-400">·</span>
            <span>{formatDistanceToNow(result.generated_at)}</span>
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
        {loading && !result ? (
          <p className="text-sm text-slate-400">Loading gossip…</p>
        ) : items.length === 0 ? (
          <div className="max-w-lg mx-auto text-center py-16">
            <p className="text-sm text-slate-300 font-semibold">No accelerating chatter right now</p>
            <p className="text-xs text-slate-400 mt-2 leading-relaxed">
              Nothing is currently being mentioned enough above its baseline to qualify.
              This needs social ingestion (<span className="font-mono text-slate-400">RUN_SOCIAL</span>)
              banking posts; check back as the stream fills.
            </p>
          </div>
        ) : (
          <div className="max-w-4xl mx-auto border border-[#1e2d4a] rounded-lg overflow-hidden">
            <table className="w-full text-xs">
              <thead className="bg-[#0a0f1e] border-b border-[#1e2d4a]">
                <tr className="text-[10px] uppercase tracking-wider text-slate-400">
                  <th scope="col" className="px-3 py-2 text-left w-8">#</th>
                  <th scope="col" className="px-3 py-2 text-left">Ticker</th>
                  <th scope="col" className="px-3 py-2 text-right">Velocity</th>
                  <th scope="col" className="px-3 py-2 text-right">Recent</th>
                  <th scope="col" className="px-3 py-2 text-right">Baseline</th>
                  <th scope="col" className="px-3 py-2 text-left w-40">Gossip score</th>
                  <th scope="col" className="px-3 py-2 text-center">Dir</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => (
                  <GossipRow key={it.ticker} item={it} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
