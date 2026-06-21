import { NewsItem } from "@/types/news";

interface StatsBarProps {
  items: NewsItem[];
}

export default function StatsBar({ items }: StatsBarProps) {
  const counts = { bullish: 0, bearish: 0, neutral: 0 };
  items.forEach((item) => {
    if (item.sentiment) counts[item.sentiment.label]++;
  });

  const total = items.length || 1;
  const bullPct = Math.round((counts.bullish / total) * 100);
  const bearPct = Math.round((counts.bearish / total) * 100);
  const neutPct = 100 - bullPct - bearPct;

  return (
    <div className="bg-[#0f1629] border-b border-[#1e2d4a] px-6 py-2 flex items-center gap-5 text-xs shrink-0">
      <span className="flex items-center gap-1.5 text-emerald-400">
        <span>▲</span>
        <span className="font-semibold uppercase tracking-wider">Bullish</span>
        <span className="text-slate-400">{counts.bullish}</span>
      </span>
      <span className="flex items-center gap-1.5 text-red-400">
        <span>▼</span>
        <span className="font-semibold uppercase tracking-wider">Bearish</span>
        <span className="text-slate-400">{counts.bearish}</span>
      </span>
      <span className="flex items-center gap-1.5 text-slate-400">
        <span>◆</span>
        <span className="font-semibold uppercase tracking-wider">Neutral</span>
        <span className="text-slate-400">{counts.neutral}</span>
      </span>

      {/* sentiment bar */}
      <div className="flex-1 max-w-xs h-1.5 bg-[#1e2d4a] rounded-full overflow-hidden ml-2">
        <div className="h-full flex">
          <div
            className="bg-emerald-500 transition-all duration-500"
            style={{ width: `${bullPct}%` }}
          />
          <div
            className="bg-red-500 transition-all duration-500"
            style={{ width: `${bearPct}%` }}
          />
          <div
            className="bg-slate-600 transition-all duration-500"
            style={{ width: `${neutPct}%` }}
          />
        </div>
      </div>
      <span className="text-slate-400 ml-auto">{bullPct}% bullish</span>
    </div>
  );
}
