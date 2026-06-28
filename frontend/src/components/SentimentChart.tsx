"use client";

import { useEffect, useRef, useState } from "react";
import {
  BaselineSeries,
  ColorType,
  HistogramSeries,
  LineStyle,
  createChart,
  type UTCTimestamp,
} from "lightweight-charts";
import { ChartRange, SentimentHistory, fetchTickerSentimentHistory } from "@/lib/api";

// Map the shared price-chart range to a sentiment lookback (days). Sentiment is
// daily and only spans the ingestion window, so short ranges show few points.
const RANGE_DAYS: Record<ChartRange, number> = { "1D": 1, "5D": 5, "1M": 30, "3M": 90, "1Y": 365 };

/** Daily news+social sentiment over time: a baseline series (green above neutral,
 *  red below) with a mention-count histogram — shares the price chart's timeframe. */
export default function SentimentChart({ symbol, range }: { symbol: string; range: ChartRange }) {
  const days = RANGE_DAYS[range];
  const [hist, setHist] = useState<SentimentHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchTickerSentimentHistory(symbol, days).then((h) => {
      if (!cancelled) {
        setHist(h);
        setLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [symbol, days]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || !hist || hist.points.length === 0) return;

    const chart = createChart(el, {
      width: el.clientWidth,
      height: 320,
      layout: {
        background: { type: ColorType.Solid, color: "#0a0f1e" },
        textColor: "#94a3b8",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#1e2d4a" }, horzLines: { color: "#1e2d4a" } },
      timeScale: { borderColor: "#1e2d4a", rightOffset: 4 },
      rightPriceScale: { borderColor: "#1e2d4a" },
      crosshair: { mode: 0 },
    });

    const sentiment = chart.addSeries(BaselineSeries, {
      baseValue: { type: "price", price: 0 },
      topLineColor: "#10b981",
      topFillColor1: "rgba(16,185,129,0.28)",
      topFillColor2: "rgba(16,185,129,0.04)",
      bottomLineColor: "#ef4444",
      bottomFillColor1: "rgba(239,68,68,0.04)",
      bottomFillColor2: "rgba(239,68,68,0.28)",
      lineWidth: 2,
    });
    sentiment.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.28 } });
    sentiment.setData(
      hist.points.map((p) => ({ time: p.time as UTCTimestamp, value: p.mean_sentiment }))
    );
    sentiment.createPriceLine({
      price: 0, color: "#64748b", lineWidth: 1, lineStyle: LineStyle.Dashed,
      axisLabelVisible: true, title: "neutral",
    });

    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volume.setData(
      hist.points.map((p) => ({ time: p.time as UTCTimestamp, value: p.count, color: "#64748b55" }))
    );

    chart.timeScale().fitContent();
    const onResize = () => chart.applyOptions({ width: el.clientWidth });
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.remove();
    };
  }, [hist]);

  const noData = !loading && hist != null && hist.points.length === 0;

  return (
    <div>
      <div className="relative">
        <div
          ref={containerRef}
          role="img"
          aria-label={`News and social sentiment for ${symbol} over ${days} days`}
          className="w-full"
          style={{ height: 320 }}
        />
        {loading && (
          <p className="absolute inset-0 flex items-center justify-center text-xs text-slate-400">
            Loading sentiment…
          </p>
        )}
        {noData && (
          <p className="absolute inset-0 flex items-center justify-center text-xs text-slate-400 text-center px-4">
            No sentiment history for {symbol} yet — needs ingested mentions over the window.
          </p>
        )}
      </div>
    </div>
  );
}
