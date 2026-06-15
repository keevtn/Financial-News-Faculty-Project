"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchTickerPrices, TickerPrice } from "@/lib/api";

interface TickerTapeProps {
  symbols: string[];
  pollIntervalMs?: number;
}

export default function TickerTape({ symbols, pollIntervalMs = 60_000 }: TickerTapeProps) {
  const [prices, setPrices] = useState<Record<string, TickerPrice>>({});
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  // Stable dep key — re-fetches immediately when the symbol set changes
  const symbolsKey = useMemo(() => [...symbols].sort().join(","), [symbols]);

  useEffect(() => {
    if (!symbolsKey) return;
    let cancelled = false;

    async function load() {
      const data = await fetchTickerPrices(symbols);
      if (cancelled) return;
      setPrices(data);
      setLastUpdated(new Date());
    }

    load();
    const id = setInterval(load, pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbolsKey, pollIntervalMs]);

  // Only render symbols that have price data and are in the current set
  const ready = symbols.filter((s) => prices[s]?.price != null);

  if (!ready.length) return null;

  // 4 seconds per chip, minimum 20s so a single ticker doesn't flash past
  const durationSec = Math.max(20, ready.length * 4);

  // Duplicate for seamless loop — the animation moves -50% so the second copy
  // fills in exactly where the first left off.
  const chips = [...ready, ...ready];

  return (
    <div className="bg-[#080e1a] border-b border-[#1e2d4a] overflow-hidden shrink-0 h-8 flex items-center relative">
      <div
        className="flex whitespace-nowrap"
        style={{
          animation: `ticker-scroll ${durationSec}s linear infinite`,
          width: "max-content",
        }}
      >
        {chips.map((sym, i) => {
          const p = prices[sym];
          const up = (p?.change ?? 0) >= 0;
          const color = up ? "text-emerald-400" : "text-red-400";
          const arrow = up ? "▲" : "▼";
          return (
            <span
              key={`${sym}-${i}`}
              className="inline-flex items-center gap-1.5 px-4 text-[11px] font-mono"
            >
              <span className="text-slate-300 font-semibold tracking-wide">{sym}</span>
              <span className="text-slate-100">${p.price!.toFixed(2)}</span>
              <span className={`${color} font-medium`}>
                {arrow} {Math.abs(p.change_pct ?? 0).toFixed(2)}%
              </span>
              <span className="text-[#1e2d4a] select-none ml-2">|</span>
            </span>
          );
        })}
      </div>

      {/* Fade edges so the scroll looks clean */}
      <div className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-[#080e1a] to-transparent" />
      <div className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-[#080e1a] to-transparent" />

      {lastUpdated && (
        <span className="absolute right-3 text-[9px] text-slate-600 font-mono shrink-0 z-10">
          {lastUpdated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </span>
      )}
    </div>
  );
}
