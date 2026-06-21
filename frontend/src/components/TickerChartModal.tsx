"use client";

import { useEffect, useRef } from "react";
import dynamic from "next/dynamic";

// Load the chart client-side only (lightweight-charts touches the DOM/canvas).
const TickerChart = dynamic(() => import("./TickerChart"), { ssr: false });

const FOCUSABLE =
  'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';

export default function TickerChartModal({
  symbol,
  onClose,
}: {
  symbol: string;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Move focus into the dialog and remember what to restore on close.
    const previouslyFocused = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      // Focus trap (WCAG 2.4.3) — keep Tab focus inside the dialog.
      if (e.key === "Tab" && panelRef.current) {
        const items = Array.from(
          panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)
        ).filter((el) => el.offsetParent !== null);
        if (items.length === 0) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };

    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      previouslyFocused?.focus?.();
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={`${symbol} price chart`}
        tabIndex={-1}
        className="bg-[#0f1629] border border-[#1e2d4a] rounded-lg w-full max-w-3xl p-4 shadow-xl focus:outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-baseline gap-2.5">
            <h2 className="text-lg font-bold font-mono text-slate-100">{symbol}</h2>
            <a
              href={`https://finance.yahoo.com/quote/${symbol}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[10px] text-slate-400 hover:text-[#00d4aa] transition-colors"
            >
              Yahoo ↗
            </a>
          </div>
          <button
            onClick={onClose}
            aria-label="Close chart"
            className="text-slate-400 hover:text-slate-100 transition-colors text-sm px-2"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>
        <TickerChart symbol={symbol} />
      </div>
    </div>
  );
}
