import { NewsItem } from "@/types/news";
import { formatDistanceToNow } from "@/lib/time";

// Left border color keyed by sentiment label
const BORDER: Record<string, string> = {
  bullish: "border-l-emerald-500",
  bearish: "border-l-red-500",
  neutral: "border-l-slate-600",
};

// Inline sentiment indicator
const SENTIMENT: Record<string, { icon: string; cls: string }> = {
  bullish: { icon: "▲", cls: "text-emerald-400" },
  bearish: { icon: "▼", cls: "text-red-400" },
  neutral: { icon: "◆", cls: "text-slate-400" },
};

const TOPIC_COLOR: Record<string, string> = {
  Crypto:      "text-purple-400",
  Energy:      "text-orange-400",
  Equities:    "text-blue-400",
  Macro:       "text-green-400",
  Regulatory:  "text-red-400",
  Bonds:       "text-yellow-400",
  Commodities: "text-amber-400",
  Technology:  "text-cyan-400",
};

// Colored dot per platform so the source column scans fast
function PlatformDot({ source }: { source: string }) {
  if (source.startsWith("Reddit"))     return <span className="w-1.5 h-1.5 rounded-full bg-orange-400 shrink-0" />;
  if (source.startsWith("StockTwits")) return <span className="w-1.5 h-1.5 rounded-full bg-sky-400 shrink-0" />;
  if (source.startsWith("Bluesky"))    return <span className="w-1.5 h-1.5 rounded-full bg-violet-400 shrink-0" />;
  return <span className="w-1.5 h-1.5 rounded-full bg-slate-600 shrink-0" />;
}

// Compress verbose source labels into something that fits ~110px
function shortSource(source: string): string {
  if (source.startsWith("Reddit - "))     return source.slice("Reddit - ".length);
  if (source.startsWith("StockTwits — ")) return source.slice("StockTwits — ".length);
  return source;
}

function SocialRow({
  item,
  scoringPending,
}: {
  item: NewsItem;
  scoringPending?: boolean;
}) {
  const s = item.sentiment;
  const borderCls = s ? (BORDER[s.label] ?? "border-l-slate-700") : "border-l-slate-700";
  const sentConf = s ? SENTIMENT[s.label] : null;
  const firstTopic = item.topic.split(",")[0].trim();
  const isLink = item.url && item.url !== "#";

  const row = (
    <div
      className={`
        flex items-center gap-3 pl-3 pr-4 py-[7px]
        border-l-2 ${borderCls}
        hover:bg-[#0f1629] transition-colors group
      `}
    >
      {/* Platform dot + source name */}
      <div className="flex items-center gap-1.5 w-28 shrink-0 min-w-0">
        <PlatformDot source={item.source} />
        <span className="text-[10px] text-slate-400 truncate font-mono leading-none">
          {shortSource(item.source)}
        </span>
      </div>

      {/* Message — fills remaining space, single line */}
      <span className="flex-1 text-xs text-slate-300 truncate min-w-0 group-hover:text-[#00d4aa] transition-colors leading-none">
        {item.title}
      </span>

      {/* First topic (skip General — it adds no signal) */}
      {firstTopic && firstTopic !== "General" && (
        <span
          className={`text-[10px] shrink-0 ${TOPIC_COLOR[firstTopic] ?? "text-slate-400"}`}
        >
          {firstTopic}
        </span>
      )}

      {/* Up to 2 tickers */}
      {item.tickers && item.tickers.length > 0 && (
        <div className="flex gap-1.5 shrink-0">
          {item.tickers.slice(0, 2).map((t) => (
            <span key={t} className="text-[10px] font-mono text-sky-400">
              ${t}
            </span>
          ))}
        </div>
      )}

      {/* Sentiment score */}
      <div className="w-14 shrink-0 text-right">
        {sentConf ? (
          <span className={`text-[10px] font-semibold tabular-nums ${sentConf.cls}`}>
            {sentConf.icon}{" "}
            <span className="font-mono opacity-80">
              {s!.score > 0 ? "+" : ""}
              {s!.score.toFixed(2)}
            </span>
          </span>
        ) : scoringPending ? (
          <span className="inline-block h-2.5 w-10 rounded bg-slate-700/50 animate-pulse" />
        ) : null}
      </div>

      {/* Relative timestamp */}
      <span className="text-[10px] text-slate-400 shrink-0 w-10 text-right tabular-nums">
        {formatDistanceToNow(item.published_at)}
      </span>
    </div>
  );

  return isLink ? (
    <a href={item.url} target="_blank" rel="noopener noreferrer" className="block">
      {row}
    </a>
  ) : (
    row
  );
}

interface SocialFeedProps {
  items: NewsItem[];
  scoringPending?: boolean;
}

export default function SocialFeed({ items, scoringPending }: SocialFeedProps) {
  if (items.length === 0) {
    return (
      <main className="flex-1 flex items-center justify-center text-slate-400 text-sm">
        No social items yet.
      </main>
    );
  }

  return (
    <main className="flex-1 overflow-y-auto scrollbar-thin">
      {/* Column header */}
      <div className="flex items-center gap-3 pl-5 pr-4 py-1.5 border-b border-[#1e2d4a] sticky top-0 bg-[#0a0e1a] z-10">
        <span className="w-28 shrink-0 text-[9px] uppercase tracking-widest text-slate-400">Source</span>
        <span className="flex-1 text-[9px] uppercase tracking-widest text-slate-400">Message</span>
        <span className="text-[9px] uppercase tracking-widest text-slate-400 w-14 text-right">Sentiment</span>
        <span className="text-[9px] uppercase tracking-widest text-slate-400 w-10 text-right">Age</span>
      </div>

      {/* Rows */}
      <div className="divide-y divide-[#1e2d4a]/60">
        {items.map((item) => (
          <SocialRow key={item.id} item={item} scoringPending={scoringPending} />
        ))}
      </div>
    </main>
  );
}
