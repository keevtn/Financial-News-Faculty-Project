interface TickerBadgeProps {
  ticker: string;
}

export default function TickerBadge({ ticker }: TickerBadgeProps) {
  const href = `https://finance.yahoo.com/quote/${ticker}`;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded border bg-sky-500/10 text-sky-400 border-sky-800 hover:bg-sky-500/20 transition-colors shrink-0"
    >
      {ticker}
    </a>
  );
}
