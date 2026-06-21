"use client";

import { ReactNode, createContext, useCallback, useContext, useState } from "react";
import TickerChartModal from "./TickerChartModal";

interface ChartContextValue {
  /** Open the candlestick chart modal for a ticker symbol. */
  openChart: (symbol: string) => void;
}

const ChartContext = createContext<ChartContextValue>({ openChart: () => {} });

/** Hook any component can use to pop up a ticker chart. */
export function useChart(): ChartContextValue {
  return useContext(ChartContext);
}

/**
 * Wraps the app so any descendant can call `useChart().openChart(symbol)` to
 * open a single shared chart modal (one instance, reused everywhere).
 */
export function ChartProvider({ children }: { children: ReactNode }) {
  const [symbol, setSymbol] = useState<string | null>(null);
  const openChart = useCallback((s: string) => setSymbol(s.toUpperCase()), []);

  return (
    <ChartContext.Provider value={{ openChart }}>
      {children}
      {symbol && <TickerChartModal symbol={symbol} onClose={() => setSymbol(null)} />}
    </ChartContext.Provider>
  );
}
