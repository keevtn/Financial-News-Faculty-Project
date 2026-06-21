export type TabId = "structured" | "unstructured" | "catalysts" | "screener";

interface TabNavProps {
  active: TabId;
  onChange: (tab: TabId) => void;
}

const TABS: { id: TabId; label: string; sub: string }[] = [
  { id: "structured",   label: "Structured",   sub: "RSS · SEC · FDA" },
  { id: "unstructured", label: "Unstructured", sub: "Social · Transcripts · Crawls" },
  { id: "catalysts",    label: "Catalysts",    sub: "Pre-market AI ranking" },
  { id: "screener",     label: "Screener",     sub: "Market Cap · Movers · Volume" },
];

export default function TabNav({ active, onChange }: TabNavProps) {
  return (
    <nav aria-label="Dashboard views" className="bg-[#0a0f1e] border-b border-[#1e2d4a] px-6 flex items-end gap-0 shrink-0">
      {TABS.map((tab) => {
        const isActive = active === tab.id;
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            aria-current={isActive ? "page" : undefined}
            className={[
              "relative flex items-baseline gap-2 px-5 py-2.5 text-xs font-semibold tracking-wide transition-colors border-b-2 -mb-px",
              isActive
                ? "text-[#00d4aa] border-[#00d4aa]"
                : "text-slate-400 border-transparent hover:text-slate-300",
            ].join(" ")}
          >
            {tab.label}
            <span className="text-[10px] font-normal text-slate-400">
              {tab.sub}
            </span>
          </button>
        );
      })}
    </nav>
  );
}
