"use client";

import { useChart } from "./ChartProvider";

/**
 * Small bar-chart icon button that opens the candlestick chart modal for a
 * ticker. Sits next to a ticker label so the label itself can stay a link
 * (e.g. out to Yahoo) while the icon opens the in-app chart.
 */
export default function ChartIconButton({
  ticker,
  className = "",
}: {
  ticker: string;
  className?: string;
}) {
  const { openChart } = useChart();
  return (
    <button
      type="button"
      onClick={() => openChart(ticker)}
      aria-label={`View ${ticker} candlestick chart`}
      title="View chart"
      className={`inline-flex items-center text-slate-400 hover:text-[#00d4aa] transition-colors shrink-0 ${className}`}
    >
      <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
        <rect x="1.5" y="9" width="2.5" height="5.5" rx="0.5" />
        <rect x="6.75" y="5.5" width="2.5" height="9" rx="0.5" />
        <rect x="12" y="2.5" width="2.5" height="12" rx="0.5" />
      </svg>
    </button>
  );
}
