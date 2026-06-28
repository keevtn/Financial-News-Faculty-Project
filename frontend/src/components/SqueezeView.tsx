"use client";

import { useEffect, useState } from "react";
import {
  Direction,
  SqueezeItem,
  SqueezeRanking,
  SqueezeTrackRecord,
  fetchLatestSqueeze,
  fetchSqueezeTrackRecord,
} from "@/lib/api";
import { formatDistanceToNow } from "@/lib/time";
import ChartIconButton from "./ChartIconButton";

// ── Visual config ───────────────────────────────────────────────────────────

const DIR: Record<Direction, { icon: string; text: string; bg: string; border: string }> = {
  bullish: { icon: "▲", text: "text-emerald-400", bg: "bg-emerald-500/10", border: "border-emerald-800" },
  bearish: { icon: "▼", text: "text-red-400",     bg: "bg-red-500/10",     border: "border-red-900" },
  neutral: { icon: "◆", text: "text-slate-400",   bg: "bg-slate-700/30",   border: "border-slate-700" },
};

function fmtShortFloat(n: number | null): string {
  return n == null ? "—" : `${(n * 100).toFixed(1)}%`;
}

function fmtDays(n: number | null): string {
  return n == null ? "—" : `${n.toFixed(1)}d`;
}

function fmtShares(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(0)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return String(n);
}

function fmtSent(n: number): string {
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}`;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function MeterBar({ label, value, color }: { label: string; value: number; color: string }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="flex items-center gap-2">
      <span className="w-14 text-[10px] uppercase tracking-wider text-slate-400 shrink-0">{label}</span>
      <div className="flex-1 h-1.5 bg-[#1e2d4a] rounded-full overflow-hidden">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-7 text-right text-[10px] font-mono text-slate-400">{pct}</span>
    </div>
  );
}

function SqueezeCard({ item }: { item: SqueezeItem }) {
  const [open, setOpen] = useState(false);
  const dir = DIR[item.direction];
  const score = Math.max(0, Math.min(100, item.squeeze_score));
  // Ignition tells whether the loaded setup is actually being talked up.
  const status = item.ignition_score >= 0.4 ? "Firing" : "Primed";

  return (
    <article className="bg-[#0f1629] border border-[#1e2d4a] rounded-lg overflow-hidden hover:border-[#2d4470] transition-colors">
      <div className="flex">
        {/* rank rail */}
        <div className="flex flex-col items-center justify-center w-12 shrink-0 bg-[#0a0f1e] border-r border-[#1e2d4a] py-4">
          <span className="text-[10px] uppercase tracking-widest text-slate-400">Rank</span>
          <span className="text-xl font-bold text-slate-200 leading-none mt-1">{item.rank}</span>
        </div>

        <div className="flex-1 p-4 flex flex-col gap-3 min-w-0">
          {/* header: ticker + status + score */}
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-2.5">
              <span className="inline-flex items-center gap-1.5">
                <a
                  href={`https://finance.yahoo.com/quote/${item.ticker}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-lg font-bold font-mono text-slate-100 hover:text-[#00d4aa] transition-colors"
                >
                  {item.ticker}
                </a>
                <ChartIconButton ticker={item.ticker} />
              </span>
              <span
                className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded border ${dir.bg} ${dir.text} ${dir.border}`}
                title="Social sentiment direction"
              >
                {dir.icon} {item.direction}
              </span>
              <span
                className={`text-[10px] font-semibold uppercase tracking-wider ${item.ignition_score >= 0.4 ? "text-amber-400" : "text-slate-400"}`}
                title={status === "Firing" ? "Heavy bullish chatter on a loaded setup" : "Loaded short setup, chatter still quiet"}
              >
                {status}
              </span>
            </div>

            <div className="flex flex-col items-end shrink-0">
              <span className="text-[10px] uppercase tracking-widest text-slate-400">Squeeze</span>
              <span className="text-xl font-bold font-mono leading-none text-[#00d4aa]">
                {item.squeeze_score.toFixed(0)}
              </span>
            </div>
          </div>

          {/* overall score bar */}
          <div className="h-1.5 bg-[#1e2d4a] rounded-full overflow-hidden">
            <div className="h-full bg-[#00d4aa] transition-all duration-500" style={{ width: `${score}%` }} />
          </div>

          {/* fuel + ignition meters */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-1.5">
            <MeterBar label="Fuel" value={item.fuel_score} color="bg-rose-500/70" />
            <MeterBar label="Ignition" value={item.ignition_score} color="bg-amber-500/70" />
          </div>

          {/* short + social metrics */}
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px]">
            <span className="text-slate-400">
              <span className="font-mono text-slate-200">{fmtShortFloat(item.short_pct_float)}</span> short float
            </span>
            <span className="text-slate-400">·</span>
            <span className="text-slate-400">
              <span className="font-mono text-slate-200">{fmtDays(item.short_ratio)}</span> to cover
            </span>
            <span className="text-slate-400">·</span>
            <span className="text-slate-400">
              <span className="font-mono text-slate-200">{fmtShares(item.float_shares)}</span> float
            </span>
            <span className="text-slate-400">·</span>
            <span className="text-slate-400">
              <span className="font-mono text-slate-200">{item.n_posts}</span> posts
            </span>
            <span className="text-slate-400">·</span>
            <span className="text-slate-400">
              sent{" "}
              <span
                className={`font-mono ${item.social_sentiment > 0.05 ? "text-emerald-400" : item.social_sentiment < -0.05 ? "text-red-400" : "text-slate-300"}`}
              >
                {fmtSent(item.social_sentiment)}
              </span>
            </span>
            {item.social_velocity != null && item.social_velocity > 1 && (
              <>
                <span className="text-slate-400">·</span>
                <span className="font-mono text-amber-400" title="Mention acceleration vs trailing baseline (gossip)">
                  {item.social_velocity.toFixed(1)}× accel
                </span>
              </>
            )}
          </div>

          {/* posts toggle */}
          {item.sample_posts.length > 0 && (
            <div className="pt-1">
              <button
                onClick={() => setOpen((o) => !o)}
                className="text-[10px] uppercase tracking-wider text-slate-400 hover:text-[#00d4aa] transition-colors"
                aria-expanded={open}
              >
                {open ? "▾ Hide" : "▸ Show"} {item.sample_posts.length} post
                {item.sample_posts.length === 1 ? "" : "s"}
              </button>
              {open && (
                <ul className="mt-2 flex flex-col gap-2 border-l border-[#1e2d4a] pl-3">
                  {item.sample_posts.map((p, i) => (
                    <li key={i} className="flex flex-col gap-0.5">
                      <div className="flex items-center gap-2 text-[10px] text-slate-400">
                        <span className="px-1 rounded border border-violet-900 bg-violet-500/10 text-violet-400 uppercase font-semibold tracking-wider">
                          bluesky
                        </span>
                        <span className="truncate">@{p.handle}</span>
                        <span>♥ {p.likes}</span>
                      </div>
                      <span className="text-xs text-slate-300 leading-snug">{p.text}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>
    </article>
  );
}

// ── Track record ──────────────────────────────────────────────────────────────

function TrackRecordPanel({ tr }: { tr: SqueezeTrackRecord }) {
  const s = tr.summary;
  const pct = (n: number | null) => (n == null ? "—" : `${(n * 100).toFixed(0)}%`);
  const signed = (n: number | null) => (n == null ? "—" : `${n > 0 ? "+" : ""}${(n * 100).toFixed(1)}%`);

  return (
    <div className="max-w-3xl mx-auto mb-4 bg-[#0f1629] border border-[#1e2d4a] rounded-lg px-4 py-3">
      <div className="flex items-center justify-between mb-2.5">
        <span className="text-[10px] uppercase tracking-widest text-slate-400">Track Record</span>
        <span className="text-[10px] text-slate-400">
          {s.graded_runs} graded run{s.graded_runs === 1 ? "" : "s"}
        </span>
      </div>
      {s.graded_runs === 0 ? (
        <p className="text-xs text-slate-400 leading-relaxed">
          No graded runs yet — performance appears automatically once a ranking&apos;s
          5-session window closes and is graded against realized peak gains.
        </p>
      ) : (
        <div className="grid grid-cols-3 gap-4">
          <div>
            <div className="text-lg font-bold font-mono leading-none text-slate-200">{pct(s.avg_squeeze_hit_rate)}</div>
            <div className="text-[10px] text-slate-400 mt-1">Squeeze hit-rate</div>
            <div className="text-[9px] text-slate-400 leading-tight">ranked names that popped ≥ threshold</div>
          </div>
          <div>
            <div className={`text-lg font-bold font-mono leading-none ${s.avg_reaction_separation == null ? "text-slate-200" : s.avg_reaction_separation > 0 ? "text-emerald-400" : "text-red-400"}`}>
              {signed(s.avg_reaction_separation)}
            </div>
            <div className="text-[10px] text-slate-400 mt-1">Reaction separation</div>
            <div className="text-[9px] text-slate-400 leading-tight">top-half vs bottom-half peak gain</div>
          </div>
          <div>
            <div className={`text-lg font-bold font-mono leading-none ${s.avg_close_return == null ? "text-slate-200" : s.avg_close_return > 0 ? "text-emerald-400" : "text-red-400"}`}>
              {signed(s.avg_close_return)}
            </div>
            <div className="text-[10px] text-slate-400 mt-1">Avg window return</div>
            <div className="text-[9px] text-slate-400 leading-tight">mean close return over the window</div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Main view ─────────────────────────────────────────────────────────────────

export default function SqueezeView() {
  const [ranking, setRanking] = useState<SqueezeRanking | null>(null);
  const [trackRecord, setTrackRecord] = useState<SqueezeTrackRecord | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    const [r, tr] = await Promise.all([fetchLatestSqueeze(), fetchSqueezeTrackRecord()]);
    setRanking(r);
    setTrackRecord(tr);
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
          <h2 className="text-sm font-bold text-slate-100">Short-Squeeze Ranking</h2>
          <span className="text-[10px] text-slate-400">
            Heavily shorted names ranked by short fuel × bullish social ignition (Bluesky)
          </span>
        </div>

        {ranking && (
          <div className="flex items-center gap-3 text-[10px] text-slate-400 flex-wrap">
            <span>
              <span className="font-mono text-slate-300">{ranking.fueled_count}</span> fueled of{" "}
              <span className="font-mono text-slate-300">{ranking.universe_count}</span> scanned
            </span>
            <span className="text-slate-400">·</span>
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
        {trackRecord && <TrackRecordPanel tr={trackRecord} />}
        {loading && !ranking ? (
          <p className="text-sm text-slate-400">Loading latest squeeze ranking…</p>
        ) : !ranking ? (
          <div className="max-w-lg mx-auto text-center py-16">
            <p className="text-sm text-slate-300 font-semibold">No squeeze ranking generated yet</p>
            <p className="text-xs text-slate-400 mt-2 leading-relaxed">
              Squeeze rankings are produced server-side. Trigger one with a
              <span className="font-mono text-slate-400"> POST /api/squeeze/run</span> (key-protected),
              or enable the scheduler with <span className="font-mono text-slate-400">RUN_SQUEEZE_SCHEDULER</span>,
              then refresh.
            </p>
          </div>
        ) : ranking.items.length === 0 ? (
          <p className="text-sm text-slate-400">
            The latest run found no qualifying squeeze setups.
          </p>
        ) : (
          <div className="flex flex-col gap-3 max-w-3xl mx-auto">
            {ranking.items.map((item) => (
              <SqueezeCard key={item.ticker} item={item} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
