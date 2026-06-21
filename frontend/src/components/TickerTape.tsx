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
  const [paused, setPaused] = useState(false);

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
    <section
      aria-label="Live ticker prices"
      className="bg-[#080e1a] border-b border-[#1e2d4a] overflow-hidden shrink-0 h-8 flex items-center relative"
    >
      {/* WCAG 2.2.2 — let users stop the auto-scrolling motion. */}
      <button
        type="button"
        onClick={() => setPaused((v) => !v)}
        aria-label={paused ? "Resume scrolling ticker" : "Pause scrolling ticker"}
        className="absolute left-1.5 z-20 text-[11px] leading-none text-slate-400 hover:text-[#00d4aa] bg-[#080e1a] px-1 py-1 rounded"
      >
        <span aria-hidden="true">{paused ? "▶" : "❚❚"}</span>
      </button>
      <div
        className="flex whitespace-nowrap pl-7"
        style={{
          animation: `ticker-scroll ${durationSec}s linear infinite`,
          animationPlayState: paused ? "paused" : "running",
          width: "max-content",
        }}
      >
        {chips.map((sym, i) => {
          const p = prices[sym];
          const up = (p?.change ?? 0) >= 0;
          const color = up ? "text-emerald-400" : "text-red-400";
          const arrow = up ? "▲" : "▼";
          const pct = p.change_pct ?? 0;
          return (
            <span
              key={`${sym}-${i}`}
              aria-hidden={i >= ready.length ? "true" : undefined}
              className="inline-flex items-center gap-1.5 px-4 text-[11px] font-mono"
            >
              <span className="text-slate-300 font-semibold tracking-wide">{sym}</span>
              <span className="text-slate-100">${p.price!.toFixed(2)}</span>
              <span className={`${color} font-medium`}>
                <span aria-hidden="true">{arrow}</span> {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
              </span>
              <span aria-hidden="true" className="text-[#1e2d4a] select-none ml-2">|</span>
            </span>
          );
        })}
      </div>

      {/* Fade edges so the scroll looks clean */}
      <div className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-[#080e1a] to-transparent" />
      <div className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-[#080e1a] to-transparent" />

      {lastUpdated && (
        <span className="absolute right-3 text-[10px] text-slate-400 font-mono shrink-0 z-10">
          <span className="sr-only">Prices updated </span>
          {lastUpdated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </span>
      )}
    </section>
  );
}
