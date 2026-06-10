const TOPIC_STYLES: Record<string, string> = {
  Crypto:      "bg-purple-500/10 text-purple-400 border-purple-900",
  Energy:      "bg-orange-500/10 text-orange-400 border-orange-900",
  Equities:    "bg-blue-500/10 text-blue-400 border-blue-900",
  Macro:       "bg-green-500/10 text-green-400 border-green-900",
  Regulatory:  "bg-red-500/10 text-red-400 border-red-900",
  Bonds:       "bg-yellow-500/10 text-yellow-400 border-yellow-900",
  Commodities: "bg-amber-500/10 text-amber-400 border-amber-900",
  Technology:  "bg-cyan-500/10 text-cyan-400 border-cyan-900",
  General:     "bg-slate-500/10 text-slate-400 border-slate-700",
};

export default function TopicBadge({ topic }: { topic: string }) {
  const style = TOPIC_STYLES[topic] ?? TOPIC_STYLES.General;
  return (
    <span
      className={`inline-block text-[10px] font-medium px-1.5 py-0.5 rounded border ${style}`}
    >
      {topic}
    </span>
  );
}
