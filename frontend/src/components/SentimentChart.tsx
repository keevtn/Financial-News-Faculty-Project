"use client";

import { useEffect, useRef, useState } from "react";
import {
  BaselineSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  type UTCTimestamp,
} from "lightweight-charts";
import { ChartRange, SentimentHistory, SentimentPoint, fetchTickerSentimentHistory } from "@/lib/api";

// Map the shared price-chart range to a sentiment lookback (days). Sentiment is
// daily and only spans the ingestion window, so short ranges show few points.
const RANGE_DAYS: Record<ChartRange, number> = { "1D": 1, "5D": 5, "2W": 14, "1M": 30, "3M": 90, "1Y": 365 };

type Mode = "all" | "news" | "social" | "both";
const MODES: { id: Mode; label: string }[] = [
  { id: "all", label: "All" },
  { id: "news", label: "News" },
  { id: "social", label: "Social" },
  { id: "both", label: "Both" },
];

const NEWS_COLOR = "#38bdf8";    // sky
const SOCIAL_COLOR = "#a78bfa";  // violet

// count-weighted blend of news + social for one day
function combined(p: SentimentPoint): { value: number; count: number } | null {
  const tot = p.news_count + p.social_count;
  if (tot === 0) return null;
  const ns = (p.news_sentiment ?? 0) * p.news_count;
  const ss = (p.social_sentiment ?? 0) * p.social_count;
  return { value: (ns + ss) / tot, count: tot };
}

type SeriesPoint = { time: number; value: number; count: number };

function singleSeries(points: SentimentPoint[], mode: Mode): SeriesPoint[] {
  const out: SeriesPoint[] = [];
  for (const p of points) {
    if (mode === "news" && p.news_sentiment != null) {
      out.push({ time: p.time, value: p.news_sentiment, count: p.news_count });
    } else if (mode === "social" && p.social_sentiment != null) {
      out.push({ time: p.time, value: p.social_sentiment, count: p.social_count });
    } else if (mode === "all") {
      const c = combined(p);
      if (c) out.push({ time: p.time, value: c.value, count: c.count });
    }
  }
  return out;
}

/** Daily news/social sentiment over time. Toggle All / News / Social, or "Both"
 *  to overlay the two as separate colored lines. Shares the price chart's range. */
export default function SentimentChart({ symbol, range }: { symbol: string; range: ChartRange }) {
  const days = RANGE_DAYS[range];
  const [mode, setMode] = useState<Mode>("all");
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

    const newsLine = hist.points.filter((p) => p.news_sentiment != null);
    const socialLine = hist.points.filter((p) => p.social_sentiment != null);
    const single = mode === "both" ? [] : singleSeries(hist.points, mode);
    if (mode === "both" ? newsLine.length === 0 && socialLine.length === 0 : single.length === 0) return;

    const chart = createChart(el, {
      width: el.clientWidth,
      height: 320,
      layout: { background: { type: ColorType.Solid, color: "#0a0f1e" }, textColor: "#94a3b8", fontSize: 11 },
      grid: { vertLines: { color: "#1e2d4a" }, horzLines: { color: "#1e2d4a" } },
      timeScale: { borderColor: "#1e2d4a", rightOffset: 4 },
      rightPriceScale: { borderColor: "#1e2d4a" },
      crosshair: { mode: 0 },
    });

    if (mode === "both") {
      const news = chart.addSeries(LineSeries, { color: NEWS_COLOR, lineWidth: 2 });
      news.setData(newsLine.map((p) => ({ time: p.time as UTCTimestamp, value: p.news_sentiment as number })));
      const social = chart.addSeries(LineSeries, { color: SOCIAL_COLOR, lineWidth: 2 });
      social.setData(socialLine.map((p) => ({ time: p.time as UTCTimestamp, value: p.social_sentiment as number })));
      news.createPriceLine({ price: 0, color: "#64748b", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "neutral" });
    } else {
      const series = chart.addSeries(BaselineSeries, {
        baseValue: { type: "price", price: 0 },
        topLineColor: "#10b981", topFillColor1: "rgba(16,185,129,0.28)", topFillColor2: "rgba(16,185,129,0.04)",
        bottomLineColor: "#ef4444", bottomFillColor1: "rgba(239,68,68,0.04)", bottomFillColor2: "rgba(239,68,68,0.28)",
        lineWidth: 2,
      });
      series.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.28 } });
      series.setData(single.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
      series.createPriceLine({ price: 0, color: "#64748b", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "neutral" });

      const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "" });
      volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      volume.setData(single.map((p) => ({ time: p.time as UTCTimestamp, value: p.count, color: "#64748b55" })));
    }

    chart.timeScale().fitContent();
    const onResize = () => chart.applyOptions({ width: el.clientWidth });
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.remove();
    };
  }, [hist, mode]);

  const points = hist?.points ?? [];
  const hasModeData =
    mode === "both"
      ? points.some((p) => p.news_sentiment != null || p.social_sentiment != null)
      : mode === "news"
      ? points.some((p) => p.news_sentiment != null)
      : mode === "social"
      ? points.some((p) => p.social_sentiment != null)
      : points.some((p) => p.news_count + p.social_count > 0);
  const noData = !loading && hist != null && !hasModeData;

  return (
    <div>
      <div role="group" aria-label="Sentiment source" className="flex items-center gap-1 mb-2">
        {MODES.map((m) => (
          <button
            key={m.id}
            onClick={() => setMode(m.id)}
            aria-pressed={m.id === mode}
            className={[
              "text-[11px] px-2 py-0.5 rounded border transition-colors",
              m.id === mode
                ? "bg-[#00d4aa]/10 text-[#00d4aa] border-[#00d4aa]/40"
                : "text-slate-400 border-[#1e2d4a] hover:border-[#2d4470] hover:text-slate-200",
            ].join(" ")}
          >
            {m.label}
          </button>
        ))}
        {mode === "both" && (
          <span className="ml-auto flex items-center gap-2 text-[10px] text-slate-400">
            <span className="flex items-center gap-1">
              <span className="inline-block w-3 h-0.5" style={{ background: NEWS_COLOR }} /> News
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-3 h-0.5" style={{ background: SOCIAL_COLOR }} /> Social
            </span>
          </span>
        )}
      </div>
      <div className="relative">
        <div
          ref={containerRef}
          role="img"
          aria-label={`${mode} sentiment for ${symbol} over ${days} days`}
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
            No {mode === "all" ? "" : `${mode} `}sentiment for {symbol} in this window.
          </p>
        )}
      </div>
    </div>
  );
}
