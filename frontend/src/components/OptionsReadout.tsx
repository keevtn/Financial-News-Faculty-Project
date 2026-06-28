"use client";

import { useEffect, useState } from "react";
import { OptionsSignal, fetchOptions } from "@/lib/api";

/** Compact options-flow line (put/call, lean, ATM IV) for one ticker. Renders
 *  nothing when the ticker has no options chain. */
export default function OptionsReadout({ symbol }: { symbol: string }) {
  const [sig, setSig] = useState<OptionsSignal | null | undefined>(undefined); // undefined = loading

  useEffect(() => {
    let cancelled = false;
    setSig(undefined);
    fetchOptions(symbol).then((s) => {
      if (!cancelled) setSig(s);
    });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  if (sig === undefined) {
    return <p className="mt-3 pt-3 border-t border-[#1e2d4a] text-[10px] text-slate-400">Loading options flow…</p>;
  }
  if (!sig) return null;

  const leanColor =
    sig.lean === "bullish" ? "text-emerald-400" : sig.lean === "bearish" ? "text-red-400" : "text-slate-300";

  return (
    <div className="mt-3 pt-3 border-t border-[#1e2d4a] flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px]">
      <span className="uppercase tracking-wider text-slate-400">Options flow</span>
      <span className="text-slate-400">
        P/C <span className={`font-mono ${leanColor}`}>{sig.put_call_ratio ?? "—"}</span>
      </span>
      <span className="text-slate-400">·</span>
      <span className={`font-mono uppercase tracking-wider ${leanColor}`}>{sig.lean}</span>
      <span className="text-slate-400">·</span>
      <span className="text-slate-400">
        ATM IV{" "}
        <span className="font-mono text-slate-300">
          {sig.atm_iv != null ? `${(sig.atm_iv * 100).toFixed(0)}%` : "—"}
        </span>
      </span>
      <span className="text-slate-400">·</span>
      <span className="text-slate-400">
        vol <span className="font-mono text-slate-300">{sig.call_volume.toLocaleString()}c / {sig.put_volume.toLocaleString()}p</span>
      </span>
    </div>
  );
}
