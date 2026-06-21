"use client";

import { useChart } from "./ChartProvider";

interface TickerBadgeProps {
  ticker: string;
  /** "badge" = pill (news cards); "plain" = compact $TICKER (social rows). */
  variant?: "badge" | "plain";
}

export default function TickerBadge({ ticker, variant = "badge" }: TickerBadgeProps) {
  const { openChart } = useChart();

  const cls =
    variant === "plain"
      ? "text-[10px] font-mono text-sky-400 hover:text-[#00d4aa] transition-colors"
      : "inline-flex items-center text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded border bg-sky-500/10 text-sky-400 border-sky-800 hover:bg-sky-500/20 transition-colors shrink-0";

  return (
    <button
      type="button"
      onClick={() => openChart(ticker)}
      aria-label={`View ${ticker} price chart`}
      title="View price chart"
      className={cls}
    >
      {variant === "plain" ? `$${ticker}` : ticker}
    </button>
  );
}
