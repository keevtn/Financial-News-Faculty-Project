"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ScreenerPreset,
  ScreenerResult,
  ScreenerRow,
  fetchScreener,
  fetchScreenerPresets,
} from "@/lib/api";
import { formatDistanceToNow } from "@/lib/time";
import ChartIconButton from "./ChartIconButton";

// Shown before /presets responds (and if it fails).
const FALLBACK_PRESETS: ScreenerPreset[] = [
  { id: "top_gainers", label: "Top Gainers" },
  { id: "top_losers", label: "Top Losers" },
  { id: "most_active", label: "Most Active" },
  { id: "small_cap_gainers", label: "Small-Cap Gainers" },
  { id: "aggressive_small", label: "Aggressive Small Caps" },
  { id: "growth_tech", label: "Growth Tech" },
  { id: "undervalued_growth", label: "Undervalued Growth" },
  { id: "most_shorted", label: "Most Shorted" },
];

// ── Formatters ────────────────────────────────────────────────────────────────

function fmtMarketCap(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1e12) return `$${(n / 1e12).toFixed(2)}T`;
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n}`;
}

function fmtVolume(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return String(n);
}

function fmtPrice(n: number | null): string {
  return n == null ? "—" : `$${n.toFixed(2)}`;
}

function fmtPct(n: number | null): string {
  return n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

// ── Sorting ───────────────────────────────────────────────────────────────────

type SortCol = "change_pct" | "market_cap" | "volume" | "price" | "ticker";
type SortDir = "asc" | "desc";

function sortRows(rows: ScreenerRow[], col: SortCol | null, dir: SortDir): ScreenerRow[] {
  if (!col) return rows;
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = a[col];
    const bv = b[col];
    // nulls always sink to the bottom regardless of direction
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string" || typeof bv === "string") {
      return String(av).localeCompare(String(bv)) * sign;
    }
    return (av - bv) * sign;
  });
}

// ── Component ──────────────────────────────────────────────────────────────────

const SORT_BTN = "text-[10px] uppercase tracking-wider text-slate-400 hover:text-[#00d4aa] transition-colors";

export default function ScreenerView() {
  const [presets, setPresets] = useState<ScreenerPreset[]>(FALLBACK_PRESETS);
  const [preset, setPreset] = useState("top_gainers");
  const [result, setResult] = useState<ScreenerResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [sortCol, setSortCol] = useState<SortCol | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const load = useCallback(async (p: string) => {
    setLoading(true);
    const res = await fetchScreener(p, 50);
    setResult(res);
    setLoading(false);
  }, []);

  // Load the preset list once.
  useEffect(() => {
    fetchScreenerPresets().then((p) => {
      if (p.length) setPresets(p);
    });
  }, []);

  // Re-fetch whenever the selected preset changes.
  useEffect(() => {
    load(preset);
  }, [preset, load]);

  function toggleSort(col: SortCol) {
    if (sortCol === col) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(col);
      setSortDir(col === "ticker" ? "asc" : "desc");
    }
  }

  const arrow = (col: SortCol) => (sortCol === col ? (sortDir === "asc" ? " ▲" : " ▼") : "");
  const ariaSort = (col: SortCol): "ascending" | "descending" | undefined =>
    sortCol === col ? (sortDir === "asc" ? "ascending" : "descending") : undefined;
  const rows = result ? sortRows(result.rows, sortCol, sortDir) : [];

  return (
    <div className="flex-1 overflow-y-auto scrollbar-thin">
      {/* header strip */}
      <div className="bg-[#0f1629] border-b border-[#1e2d4a] px-6 py-3 flex items-center gap-4 flex-wrap sticky top-0 z-10">
        <div className="flex flex-col">
          <h2 className="text-sm font-bold text-slate-100">Market Screener</h2>
          <span className="text-[10px] text-slate-400">
            Market-wide movers by market cap, change, and volume ·{" "}
            {result?.source === "finviz_elite"
              ? "via Finviz Elite (real-time)"
              : "via Yahoo Finance (delayed)"}
          </span>
        </div>

        {result && !result.status && (
          <div className="flex items-center gap-3 text-[10px] text-slate-400 flex-wrap">
            <span>
              <span className="font-mono text-slate-300">{result.count}</span> matches
            </span>
            {result.cached && <span className="text-slate-400">· cached</span>}
            {result.fetched_at && (
              <span className="text-slate-400">
                · {formatDistanceToNow(new Date(result.fetched_at * 1000).toISOString())}
              </span>
            )}
          </div>
        )}

        <button
          onClick={() => load(preset)}
          disabled={loading}
          className="ml-auto text-[10px] uppercase tracking-wider px-2.5 py-1 rounded border border-[#1e2d4a] text-slate-400 hover:text-[#00d4aa] hover:border-[#2d4470] transition-colors disabled:opacity-50"
        >
          {loading ? "Loading…" : "↻ Refresh"}
        </button>
      </div>

      {/* preset pills */}
      <div className="bg-[#0a0f1e] border-b border-[#1e2d4a] px-6 py-2 flex flex-wrap gap-1.5">
        {presets.map((p) => {
          const active = p.id === preset;
          return (
            <button
              key={p.id}
              onClick={() => setPreset(p.id)}
              className={[
                "text-[11px] px-2.5 py-1 rounded border transition-colors",
                active
                  ? "bg-[#00d4aa]/10 text-[#00d4aa] border-[#00d4aa]/40"
                  : "bg-transparent text-slate-400 border-[#1e2d4a] hover:border-[#2d4470] hover:text-slate-200",
              ].join(" ")}
            >
              {p.label}
            </button>
          );
        })}
      </div>

      {/* body */}
      <div className="p-6">
        {loading && !result ? (
          <p className="text-sm text-slate-400">Loading screen…</p>
        ) : result && result.status ? (
          <div className="max-w-lg mx-auto text-center py-16">
            <p className="text-sm text-slate-300 font-semibold">Screen unavailable</p>
            <p className="text-xs text-slate-400 mt-2 leading-relaxed">
              The screener source could not be reached right now
              <span className="font-mono text-slate-400"> ({result.status})</span>.
              This can happen if the upstream provider is temporarily blocking the
              server. Try Refresh in a moment.
            </p>
          </div>
        ) : rows.length === 0 ? (
          <p className="text-sm text-slate-400">No matches for this screen.</p>
        ) : (
          <div className="max-w-5xl mx-auto border border-[#1e2d4a] rounded-lg overflow-x-auto scrollbar-thin">
            <table className="w-full min-w-[520px] text-xs">
              <thead className="bg-[#0a0f1e] border-b border-[#1e2d4a]">
                <tr>
                  <th scope="col" className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-slate-400 w-8">#</th>
                  <th scope="col" aria-sort={ariaSort("ticker")} className="px-3 py-2 text-left">
                    <button type="button" onClick={() => toggleSort("ticker")} className={SORT_BTN}>
                      Ticker{arrow("ticker")}
                    </button>
                  </th>
                  <th scope="col" className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-slate-400">Company</th>
                  <th scope="col" className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-slate-400 hidden md:table-cell">Sector</th>
                  <th scope="col" aria-sort={ariaSort("price")} className="px-3 py-2 text-right">
                    <button type="button" onClick={() => toggleSort("price")} className={SORT_BTN}>Price{arrow("price")}</button>
                  </th>
                  <th scope="col" aria-sort={ariaSort("change_pct")} className="px-3 py-2 text-right">
                    <button type="button" onClick={() => toggleSort("change_pct")} className={SORT_BTN}>Change{arrow("change_pct")}</button>
                  </th>
                  <th scope="col" aria-sort={ariaSort("volume")} className="px-3 py-2 text-right">
                    <button type="button" onClick={() => toggleSort("volume")} className={SORT_BTN}>Volume{arrow("volume")}</button>
                  </th>
                  <th scope="col" aria-sort={ariaSort("market_cap")} className="px-3 py-2 text-right">
                    <button type="button" onClick={() => toggleSort("market_cap")} className={SORT_BTN}>Mkt Cap{arrow("market_cap")}</button>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const chg = r.change_pct;
                  const chgColor =
                    chg == null ? "text-slate-400" : chg > 0 ? "text-emerald-400" : chg < 0 ? "text-red-400" : "text-slate-400";
                  return (
                    <tr
                      key={r.ticker}
                      className="border-b border-[#1e2d4a]/60 last:border-0 hover:bg-[#0f1629] transition-colors"
                    >
                      <td className="px-3 py-2 text-slate-400 font-mono">{i + 1}</td>
                      <td className="px-3 py-2">
                        <span className="inline-flex items-center gap-1.5">
                          <a
                            href={`https://finance.yahoo.com/quote/${r.ticker}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="font-mono font-bold text-sky-400 hover:text-[#00d4aa] transition-colors"
                          >
                            {r.ticker}
                          </a>
                          <ChartIconButton ticker={r.ticker} />
                        </span>
                      </td>
                      <td className="px-3 py-2 text-slate-300 truncate max-w-[180px]">{r.company}</td>
                      <td className="px-3 py-2 text-slate-400 hidden md:table-cell truncate max-w-[160px]">{r.sector}</td>
                      <td className="px-3 py-2 text-right font-mono text-slate-200">{fmtPrice(r.price)}</td>
                      <td className={`px-3 py-2 text-right font-mono font-semibold ${chgColor}`}>{fmtPct(chg)}</td>
                      <td className="px-3 py-2 text-right font-mono text-slate-400">{fmtVolume(r.volume)}</td>
                      <td className="px-3 py-2 text-right font-mono text-slate-300">{fmtMarketCap(r.market_cap)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
