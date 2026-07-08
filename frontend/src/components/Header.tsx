interface HeaderProps {
  itemCount: number;
  scoringPending?: boolean;
}

export default function Header({ itemCount, scoringPending }: HeaderProps) {
  return (
    <header className="bg-[#0f1629] border-b border-[#1e2d4a] px-3 sm:px-6 py-3 flex items-center justify-between gap-3 shrink-0">
      <div className="flex items-center gap-2 sm:gap-3 min-w-0">
        <span aria-hidden="true" className="w-2 h-2 rounded-full bg-[#00d4aa] animate-pulse shrink-0" />
        <span className="text-xs font-semibold text-[#00d4aa] tracking-widest uppercase shrink-0">
          Live
        </span>
        <h1 className="text-sm sm:text-base font-bold text-slate-100 sm:ml-1 truncate">
          Financial News Dashboard
        </h1>
        {scoringPending && (
          <span className="hidden sm:flex items-center gap-1.5 text-[10px] text-slate-400 ml-2 shrink-0">
            <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
            Scoring sentiment…
          </span>
        )}
      </div>
      <div className="flex items-center gap-3 text-xs text-slate-400 shrink-0">
        <span className="text-slate-300 whitespace-nowrap">{itemCount} articles</span>
        <span aria-hidden="true" className="hidden sm:inline text-[#1e2d4a]">|</span>
        <span className="hidden sm:inline">RSS · SEC · FDA</span>
      </div>
    </header>
  );
}
