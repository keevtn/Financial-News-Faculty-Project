import { SentimentResult } from "@/types/news";

interface SentimentBadgeProps {
  result: SentimentResult;
}

const BADGE_CONFIG = {
  bullish: {
    icon: "▲",
    cls: "bg-emerald-500/10 text-emerald-400 border-emerald-800",
  },
  bearish: {
    icon: "▼",
    cls: "bg-red-500/10 text-red-400 border-red-900",
  },
  neutral: {
    icon: "◆",
    cls: "bg-slate-700/30 text-slate-400 border-slate-700",
  },
} as const;

export default function SentimentBadge({ result }: SentimentBadgeProps) {
  const { icon, cls } = BADGE_CONFIG[result.label];
  const sign = result.score > 0 ? "+" : "";
  const scoreStr = `${sign}${result.score.toFixed(2)}`;
  const confStr = `${(result.confidence * 100).toFixed(0)}%`;
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded border shrink-0 ${cls}`}
      title={`Confidence: ${confStr}`}
    >
      {icon} {result.label}
      <span className="opacity-70 font-mono">{scoreStr}</span>
    </span>
  );
}
