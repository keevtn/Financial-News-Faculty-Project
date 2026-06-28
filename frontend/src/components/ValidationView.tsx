"use client";

import { useEffect, useState } from "react";
import { ValidationGroup, ValidationResult, ValidationSignal, fetchValidation } from "@/lib/api";

const VERDICT: Record<string, string> = {
  predictive: "text-emerald-400",
  weak: "text-amber-400",
  "no edge": "text-slate-400",
  "no variance": "text-slate-500",
  "insufficient data": "text-slate-500",
};

function corrColor(c: number | null): string {
  if (c == null) return "text-slate-500";
  const a = Math.abs(c);
  if (a >= 0.3) return c > 0 ? "text-emerald-400" : "text-red-400";
  if (a >= 0.15) return "text-amber-400";
  return "text-slate-400";
}

function fmtSignal(s: string): string {
  return s.replace(/_/g, " ");
}

function SignalRow({ s }: { s: ValidationSignal }) {
  return (
    <tr className="border-b border-[#1e2d4a]/60 last:border-0">
      <td className="px-3 py-2 text-slate-200 capitalize">{fmtSignal(s.signal)}</td>
      <td className="px-3 py-2 text-right font-mono text-slate-400">{s.n}</td>
      <td className={`px-3 py-2 text-right font-mono font-semibold ${corrColor(s.correlation)}`}>
        {s.correlation == null ? "—" : `${s.correlation > 0 ? "+" : ""}${s.correlation.toFixed(2)}`}
      </td>
      <td className="px-3 py-2 text-right font-mono text-slate-400">
        {s.top_minus_bottom == null ? "—" : `${s.top_minus_bottom > 0 ? "+" : ""}${(s.top_minus_bottom * 100).toFixed(2)}%`}
      </td>
      <td className={`px-3 py-2 text-[10px] uppercase tracking-wider font-semibold ${VERDICT[s.verdict] ?? "text-slate-400"}`}>
        {s.verdict}
      </td>
    </tr>
  );
}

function GroupTable({ title, group }: { title: string; group: ValidationGroup }) {
  return (
    <div className="mb-6">
      <div className="flex items-baseline gap-3 mb-2">
        <h3 className="text-sm font-bold text-slate-100">{title}</h3>
        <span className="text-[10px] text-slate-400">
          {group.n_runs} graded run{group.n_runs === 1 ? "" : "s"} · outcome:{" "}
          <span className="font-mono">{group.outcome}</span>
        </span>
      </div>
      <div className="border border-[#1e2d4a] rounded-lg overflow-hidden">
        <table className="w-full text-xs">
          <thead className="bg-[#0a0f1e] border-b border-[#1e2d4a]">
            <tr className="text-[10px] uppercase tracking-wider text-slate-400">
              <th scope="col" className="px-3 py-2 text-left">Signal</th>
              <th scope="col" className="px-3 py-2 text-right">n</th>
              <th scope="col" className="px-3 py-2 text-right" title="Spearman correlation with the realized move">Corr</th>
              <th scope="col" className="px-3 py-2 text-right" title="Top-third minus bottom-third mean outcome">Top−Bot</th>
              <th scope="col" className="px-3 py-2 text-left">Verdict</th>
            </tr>
          </thead>
          <tbody>
            {group.signals.map((s) => (
              <SignalRow key={s.signal} s={s} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function ValidationView() {
  const [result, setResult] = useState<ValidationResult | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    setResult(await fetchValidation());
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <div className="flex-1 overflow-y-auto scrollbar-thin">
      <div className="bg-[#0f1629] border-b border-[#1e2d4a] px-6 py-3 flex items-center gap-4 flex-wrap sticky top-0 z-10">
        <div className="flex flex-col">
          <h2 className="text-sm font-bold text-slate-100">Signal Validation</h2>
          <span className="text-[10px] text-slate-400">
            Does each signal actually predict the move? Spearman correlation vs realized forward returns across graded runs
          </span>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="ml-auto text-[10px] uppercase tracking-wider px-2.5 py-1 rounded border border-[#1e2d4a] text-slate-400 hover:text-[#00d4aa] hover:border-[#2d4470] transition-colors disabled:opacity-50"
        >
          {loading ? "Loading…" : "↻ Refresh"}
        </button>
      </div>

      <div className="p-6 max-w-3xl mx-auto">
        {loading && !result ? (
          <p className="text-sm text-slate-400">Loading validation…</p>
        ) : !result ? (
          <p className="text-sm text-slate-400">Validation unavailable.</p>
        ) : (
          <>
            <GroupTable title="Squeeze signals" group={result.squeeze} />
            <GroupTable title="Catalyst signals" group={result.catalyst} />
            <p className="text-[10px] text-slate-400 leading-relaxed mt-2">{result.note}</p>
          </>
        )}
      </div>
    </div>
  );
}
