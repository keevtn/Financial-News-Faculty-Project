import { SourceType } from "@/types/news";

const SOURCE_CONFIG: Record<SourceType, { label: string; cls: string }> = {
  rss:    { label: "RSS",    cls: "bg-blue-500/10 text-blue-400 border-blue-900" },
  sec:    { label: "SEC",    cls: "bg-emerald-500/10 text-emerald-400 border-emerald-900" },
  fda:    { label: "FDA",    cls: "bg-rose-500/10 text-rose-400 border-rose-900" },
  social: { label: "SOCIAL", cls: "bg-violet-500/10 text-violet-400 border-violet-900" },
};

export default function SourceBadge({ type }: { type: SourceType }) {
  const { label, cls } = SOURCE_CONFIG[type];
  return (
    <span
      className={`inline-block text-[10px] font-bold px-1.5 py-0.5 rounded border ${cls}`}
    >
      {label}
    </span>
  );
}
