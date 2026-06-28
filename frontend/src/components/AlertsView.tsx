"use client";

import { useEffect, useState } from "react";
import { AlertItem, AlertSeverity, AlertsResult, fetchAlerts } from "@/lib/api";
import { formatDistanceToNow } from "@/lib/time";
import ChartIconButton from "./ChartIconButton";

const SEV: Record<AlertSeverity, { label: string; bar: string; text: string; chip: string }> = {
  critical: { label: "Critical", bar: "bg-rose-500", text: "text-rose-400", chip: "bg-rose-500/10 text-rose-400 border-rose-900" },
  high:     { label: "High",     bar: "bg-amber-500", text: "text-amber-400", chip: "bg-amber-500/10 text-amber-400 border-amber-800" },
  medium:   { label: "Medium",   bar: "bg-sky-500",   text: "text-sky-400",   chip: "bg-sky-500/10 text-sky-400 border-sky-900" },
};

const SIGNAL_CHIP: Record<string, string> = {
  squeeze:  "bg-rose-500/10 text-rose-400 border-rose-900",
  gossip:   "bg-amber-500/10 text-amber-400 border-amber-800",
  catalyst: "bg-[#00d4aa]/10 text-[#00d4aa] border-[#00d4aa]/40",
};

function AlertCard({ a }: { a: AlertItem }) {
  const sev = SEV[a.severity];
  return (
    <article className="flex bg-[#0f1629] border border-[#1e2d4a] rounded-lg overflow-hidden hover:border-[#2d4470] transition-colors">
      <div className={`w-1 shrink-0 ${sev.bar}`} aria-hidden="true" />
      <div className="flex-1 p-4 flex flex-col gap-2 min-w-0">
        <div className="flex items-center gap-2.5 flex-wrap">
          <span className="inline-flex items-center gap-1.5">
            <a
              href={`https://finance.yahoo.com/quote/${a.ticker}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-base font-bold font-mono text-slate-100 hover:text-[#00d4aa] transition-colors"
            >
              {a.ticker}
            </a>
            <ChartIconButton ticker={a.ticker} />
          </span>
          <span className={`text-[10px] font-semibold uppercase tracking-wider ${sev.text}`}>
            {sev.label}
          </span>
          <span className="flex gap-1 ml-auto">
            {a.signals.map((s) => (
              <span
                key={s}
                className={`px-1.5 py-0.5 rounded border text-[10px] uppercase font-semibold tracking-wider ${SIGNAL_CHIP[s] ?? SIGNAL_CHIP.catalyst}`}
              >
                {s}
              </span>
            ))}
          </span>
        </div>
        <p className="text-sm text-slate-200 font-semibold">{a.title}</p>
        <p className="text-xs text-slate-400 font-mono">{a.detail}</p>
      </div>
    </article>
  );
}

export default function AlertsView() {
  const [result, setResult] = useState<AlertsResult | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    setResult(await fetchAlerts());
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  const alerts = result?.alerts ?? [];
  const c = result?.counts;

  return (
    <div className="flex-1 overflow-y-auto scrollbar-thin">
      {/* header strip */}
      <div className="bg-[#0f1629] border-b border-[#1e2d4a] px-6 py-3 flex items-center gap-4 flex-wrap sticky top-0 z-10">
        <div className="flex flex-col">
          <h2 className="text-sm font-bold text-slate-100">Alerts</h2>
          <span className="text-[10px] text-slate-400">
            Tickers crossing a signal threshold — squeeze firing, social spike, or strong catalyst
          </span>
        </div>

        {c && (
          <div className="flex items-center gap-2 text-[10px] flex-wrap">
            <span className="text-rose-400 font-mono">{c.critical} critical</span>
            <span className="text-slate-400">·</span>
            <span className="text-amber-400 font-mono">{c.high} high</span>
            <span className="text-slate-400">·</span>
            <span className="text-sky-400 font-mono">{c.medium} medium</span>
            {result?.generated_at && (
              <>
                <span className="text-slate-400">·</span>
                <span className="text-slate-400">
                  {formatDistanceToNow(new Date(result.generated_at * 1000).toISOString())}
                </span>
              </>
            )}
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
          <p className="text-sm text-slate-400">Loading alerts…</p>
        ) : alerts.length === 0 ? (
          <div className="max-w-lg mx-auto text-center py-16">
            <p className="text-sm text-slate-300 font-semibold">All quiet</p>
            <p className="text-xs text-slate-400 mt-2 leading-relaxed">
              No tickers are currently crossing a squeeze / gossip / catalyst threshold.
              Alerts derive from the latest squeeze &amp; catalyst rankings and live gossip —
              they populate as those signals fire.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-2.5 max-w-3xl mx-auto">
            {alerts.map((a) => (
              <AlertCard key={`${a.ticker}-${a.tab}`} a={a} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
