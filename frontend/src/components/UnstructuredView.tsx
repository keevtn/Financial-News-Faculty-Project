import { NewsItem } from "@/types/news";
import SocialFeed from "@/components/SocialFeed";
import StatsBar from "@/components/StatsBar";

interface UnstructuredViewProps {
  items: NewsItem[];
  scoringPending?: boolean;
}

// Shown when no social data has arrived yet (backend not running social sources)
const PLANNED_SOURCES = [
  {
    label: "Reddit",
    sub: "r/wallstreetbets · r/investing · r/stocks · r/SecurityAnalysis · and 7 more",
    color: "text-orange-400",
    border: "border-orange-400/20",
    bg: "bg-orange-400/5",
    status: "Live (RSS)",
  },
  {
    label: "StockTwits",
    sub: "22-ticker watchlist — SPY, QQQ, NVDA, BTC.X, ETH.X and more",
    color: "text-sky-400",
    border: "border-sky-400/20",
    bg: "bg-sky-400/5",
    status: "Live",
  },
  {
    label: "Bluesky",
    sub: "27 financial hashtags — #stocks, #crypto, #earnings, #inflation and more",
    color: "text-violet-400",
    border: "border-violet-400/20",
    bg: "bg-violet-400/5",
    status: "Live",
  },
];

function EmptyState() {
  return (
    <div className="flex-1 overflow-y-auto bg-[#080d1a] px-8 py-10">
      <div className="max-w-3xl mx-auto">
        <div className="mb-8">
          <h2 className="text-sm font-bold text-slate-100 tracking-wide uppercase mb-1">
            Unstructured News
          </h2>
          <p className="text-xs text-slate-400 leading-relaxed">
            Social media sources — Reddit threads, StockTwits messages, and Bluesky posts.
            Start the ingestion pipeline with{" "}
            <code className="text-[#00d4aa] bg-[#0f1629] px-1 py-0.5 rounded text-[10px]">
              --stocktwits --bluesky
            </code>{" "}
            to see live social data here.
          </p>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-8">
          {PLANNED_SOURCES.map((src) => (
            <div
              key={src.label}
              className={`rounded-lg border ${src.border} ${src.bg} px-4 py-3 flex flex-col gap-1`}
            >
              <div className="flex items-center gap-2">
                <span className={`text-xs font-semibold ${src.color} tracking-wide`}>
                  {src.label}
                </span>
                <span
                  className={`ml-auto text-[10px] font-medium px-1.5 py-0.5 rounded ${
                    src.status.startsWith("Live")
                      ? "text-[#00d4aa] bg-[#00d4aa]/10"
                      : "text-slate-600 bg-[#1e2d4a]"
                  }`}
                >
                  {src.status}
                </span>
              </div>
              <p className="text-[11px] text-slate-500 leading-snug">{src.sub}</p>
            </div>
          ))}
        </div>

        <div className="rounded-lg border border-[#1e2d4a] bg-[#0f1629] px-5 py-4">
          <p className="text-xs text-slate-500 leading-relaxed">
            <span className="text-slate-300 font-semibold">Run command</span> —{" "}
            from <code className="text-[10px] text-slate-400">backend/</code>:
          </p>
          <pre className="mt-2 text-[11px] text-[#00d4aa] bg-[#080d1a] rounded px-3 py-2 overflow-x-auto">
            python run_ingest.py --rss --stocktwits --bluesky --mongo
          </pre>
        </div>
      </div>
    </div>
  );
}

export default function UnstructuredView({ items, scoringPending }: UnstructuredViewProps) {
  if (items.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      {/* Reuse StatsBar — calculates sentiment counts from social items */}
      <StatsBar items={items} />
      <div className="flex flex-1 overflow-hidden">
        {/* Social source summary sidebar */}
        <aside className="w-52 shrink-0 bg-[#0a0e1a] border-r border-[#1e2d4a] overflow-y-auto py-4 px-3">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-600 mb-3">
            Sources
          </p>
          {/* Count items per source name */}
          {Array.from(
            items.reduce((acc, item) => {
              acc.set(item.source, (acc.get(item.source) ?? 0) + 1);
              return acc;
            }, new Map<string, number>())
          )
            .sort((a, b) => b[1] - a[1])
            .map(([source, count]) => (
              <div key={source} className="flex items-center justify-between mb-1.5">
                <span className="text-[10px] text-slate-400 truncate">{source}</span>
                <span className="text-[10px] text-slate-600 ml-1 shrink-0">{count}</span>
              </div>
            ))}
        </aside>

        <SocialFeed items={items} scoringPending={scoringPending} />
      </div>
    </div>
  );
}
