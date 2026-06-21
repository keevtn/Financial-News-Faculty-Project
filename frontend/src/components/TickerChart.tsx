"use client";

import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineStyle,
  createChart,
  type CandlestickData,
  type HistogramData,
  type UTCTimestamp,
} from "lightweight-charts";
import { ChartRange, TickerHistory, fetchTickerHistory } from "@/lib/api";

const RANGES: ChartRange[] = ["1D", "5D", "1M", "3M", "1Y"];

const UP = "#10b981";
const DOWN = "#ef4444";

export default function TickerChart({ symbol }: { symbol: string }) {
  const [range, setRange] = useState<ChartRange>("1M");
  const [history, setHistory] = useState<TickerHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  // Fetch bars whenever symbol or range changes.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchTickerHistory(symbol, range).then((h) => {
      if (!cancelled) {
        setHistory(h);
        setLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [symbol, range]);

  // (Re)build the chart whenever the bars change.
  useEffect(() => {
    const el = containerRef.current;
    if (!el || !history || history.bars.length === 0) return;

    const intraday = range === "1D" || range === "5D";
    const chart = createChart(el, {
      width: el.clientWidth,
      height: 320,
      layout: {
        background: { type: ColorType.Solid, color: "#0a0f1e" },
        textColor: "#94a3b8",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#1e2d4a" },
        horzLines: { color: "#1e2d4a" },
      },
      timeScale: { borderColor: "#1e2d4a", timeVisible: intraday, rightOffset: 4 },
      rightPriceScale: { borderColor: "#1e2d4a" },
      crosshair: { mode: 0 },
    });

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      priceLineVisible: true,
      priceLineStyle: LineStyle.Dashed,
      priceLineColor: DOWN,
    });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.28 } });
    candles.setData(
      history.bars.map<CandlestickData>((b) => ({
        time: b.time as UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      }))
    );

    // Previous-session close as a labeled reference line (the gap reference).
    if (history.prev_close != null) {
      candles.createPriceLine({
        price: history.prev_close,
        color: "#64748b",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "prev close",
      });
    }

    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volume.setData(
      history.bars.map<HistogramData>((b) => ({
        time: b.time as UTCTimestamp,
        value: b.volume,
        color: b.close >= b.open ? `${UP}55` : `${DOWN}55`,
      }))
    );

    chart.timeScale().fitContent();

    const onResize = () => chart.applyOptions({ width: el.clientWidth });
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.remove();
    };
  }, [history, range]);

  const noData = !loading && history != null && history.bars.length === 0;

  return (
    <div>
      <div role="group" aria-label="Chart time range" className="flex gap-1 mb-2">
        {RANGES.map((r) => (
          <button
            key={r}
            onClick={() => setRange(r)}
            aria-pressed={r === range}
            className={[
              "text-[11px] px-2 py-0.5 rounded border transition-colors",
              r === range
                ? "bg-[#00d4aa]/10 text-[#00d4aa] border-[#00d4aa]/40"
                : "text-slate-400 border-[#1e2d4a] hover:border-[#2d4470] hover:text-slate-200",
            ].join(" ")}
          >
            {r}
          </button>
        ))}
      </div>
      <div className="relative">
        <div
          ref={containerRef}
          role="img"
          aria-label={`Candlestick price chart for ${symbol} over the ${range} range`}
          className="w-full"
          style={{ height: 320 }}
        />
        {loading && (
          <p className="absolute inset-0 flex items-center justify-center text-xs text-slate-400">
            Loading chart…
          </p>
        )}
        {noData && (
          <p className="absolute inset-0 flex items-center justify-center text-xs text-slate-400">
            No chart data for {symbol} ({range}).
          </p>
        )}
      </div>
    </div>
  );
}
