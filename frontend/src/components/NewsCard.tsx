import { NewsItem } from "@/types/news";
import SentimentBadge from "./SentimentBadge";
import TickerBadge from "./TickerBadge";
import TopicBadge from "./TopicBadge";
import SourceBadge from "./SourceBadge";
import { formatDistanceToNow } from "@/lib/time";

interface NewsCardProps {
  item: NewsItem;
  scoringPending?: boolean;
}

export default function NewsCard({ item, scoringPending }: NewsCardProps) {
  const topics = item.topic
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  const isLink = item.url && item.url !== "#";

  return (
    <article className="bg-[#0f1629] border border-[#1e2d4a] rounded-lg p-4 flex flex-col gap-3 hover:border-[#2d4470] transition-colors">
      {/* badges row */}
      <div className="flex items-start justify-between gap-2 flex-wrap">
        <div className="flex flex-wrap gap-1.5">
          <SourceBadge type={item.source_type} />
          {topics.slice(0, 2).map((t) => (
            <TopicBadge key={t} topic={t} />
          ))}
        </div>
        {item.sentiment ? (
          <SentimentBadge result={item.sentiment} />
        ) : scoringPending ? (
          // Skeleton placeholder while FinBERT is scoring
          <span className="h-5 w-16 rounded bg-slate-700/50 animate-pulse inline-block" />
        ) : null}
      </div>

      {/* title */}
      {isLink ? (
        <a
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          className="group"
        >
          <h2 className="text-sm font-semibold text-slate-100 leading-snug group-hover:text-[#00d4aa] transition-colors line-clamp-3">
            {item.title}
          </h2>
        </a>
      ) : (
        <h2 className="text-sm font-semibold text-slate-100 leading-snug line-clamp-3">
          {item.title}
        </h2>
      )}

      {/* tickers */}
      {item.tickers && item.tickers.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {item.tickers.slice(0, 6).map((ticker) => (
            <TickerBadge key={ticker} ticker={ticker} />
          ))}
        </div>
      )}

      {/* description */}
      <p className="text-xs text-slate-500 leading-relaxed line-clamp-2">
        {item.description}
      </p>

      {/* footer */}
      <footer className="flex items-center justify-between mt-auto pt-2 border-t border-[#1e2d4a]">
        <span className="text-[10px] text-slate-600 truncate max-w-[60%]">
          {item.source}
        </span>
        <span className="text-[10px] text-slate-600 shrink-0">
          {formatDistanceToNow(item.published_at)}
        </span>
      </footer>
    </article>
  );
}
