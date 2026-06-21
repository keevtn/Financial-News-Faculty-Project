"use client";

import ChartIconButton from "./ChartIconButton";

interface TickerBadgeProps {
  ticker: string;
  /** "badge" = pill (news cards); "plain" = compact $TICKER (social rows). */
  variant?: "badge" | "plain";
}

export default function TickerBadge({ ticker, variant = "badge" }: TickerBadgeProps) {
  const linkCls =
    variant === "plain"
      ? "text-[10px] font-mono text-sky-400 hover:text-[#00d4aa] transition-colors"
      : "inline-flex items-center text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded border bg-sky-500/10 text-sky-400 border-sky-800 hover:bg-sky-500/20 transition-colors";

  return (
    <span className="inline-flex items-center gap-1 shrink-0">
      <a
        href={`https://finance.yahoo.com/quote/${ticker}`}
        target="_blank"
        rel="noopener noreferrer"
        className={linkCls}
      >
        {variant === "plain" ? `$${ticker}` : ticker}
      </a>
      <ChartIconButton ticker={ticker} />
    </span>
  );
}
