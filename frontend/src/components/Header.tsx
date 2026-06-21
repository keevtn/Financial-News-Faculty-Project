interface HeaderProps {
  itemCount: number;
  scoringPending?: boolean;
}

export default function Header({ itemCount, scoringPending }: HeaderProps) {
  return (
    <header className="bg-[#0f1629] border-b border-[#1e2d4a] px-6 py-3 flex items-center justify-between shrink-0">
      <div className="flex items-center gap-3">
        <span aria-hidden="true" className="w-2 h-2 rounded-full bg-[#00d4aa] animate-pulse shrink-0" />
        <span className="text-xs font-semibold text-[#00d4aa] tracking-widest uppercase">
          Live
        </span>
        <h1 className="text-base font-bold text-slate-100 ml-1">
          Financial News Dashboard
        </h1>
        {scoringPending && (
          <span className="flex items-center gap-1.5 text-[10px] text-slate-400 ml-2">
            <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
            Scoring sentiment…
          </span>
        )}
      </div>
      <div className="flex items-center gap-3 text-xs text-slate-400">
        <span className="text-slate-300">{itemCount} articles</span>
        <span aria-hidden="true" className="text-[#1e2d4a]">|</span>
        <span>RSS · SEC · FDA</span>
      </div>
    </header>
  );
}
