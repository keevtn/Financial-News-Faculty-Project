import { NewsItem } from "@/types/news";
import NewsCard from "./NewsCard";

interface NewsFeedProps {
  items: NewsItem[];
  scoringPending?: boolean;
}

export default function NewsFeed({ items, scoringPending }: NewsFeedProps) {
  if (items.length === 0) {
    return (
      <main className="flex-1 flex items-center justify-center text-slate-600 text-sm">
        No articles match your current filters.
      </main>
    );
  }

  return (
    <main className="flex-1 overflow-y-auto scrollbar-thin p-4">
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 max-w-screen-2xl mx-auto">
        {items.map((item) => (
          <NewsCard key={item.id} item={item} scoringPending={scoringPending} />
        ))}
      </div>
    </main>
  );
}
