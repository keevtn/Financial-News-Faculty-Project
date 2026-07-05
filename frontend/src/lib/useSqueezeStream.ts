"use client";

/**
 * useSqueezeStream — live squeeze feed over SSE (Phase 4).
 *
 * Subscribes to GET /api/squeeze/stream (a relay of the Redis
 * `squeeze:updates` channel). Two event shapes arrive as JSON lines:
 *   squeeze_run — a new ranking was persisted (refetch the full doc)
 *   doc         — a ticker-tagged item just ingested (message density)
 *
 * Degrades honestly: if the endpoint 503s (no Redis) or errors, EventSource
 * auto-reconnect is left to do its thing while `connected` stays false — the
 * caller keeps its polling fallback. Live per-ticker mention counts reset on
 * every new run event so density reads "since last ranking".
 */

import { useEffect, useRef, useState } from "react";
import { SQUEEZE_STREAM_URL, SqueezeRunEvent, SqueezeStreamEvent } from "./api";

export interface SqueezeStreamState {
  connected: boolean;
  /** Most recent squeeze_run event (the trigger to refetch /latest). */
  lastRun: SqueezeRunEvent | null;
  /** Live per-ticker mention counts since the last run event. */
  docCounts: Record<string, number>;
  /** Total doc events seen since connect (activity pulse). */
  docTotal: number;
}

export function useSqueezeStream(enabled: boolean = true): SqueezeStreamState {
  const [connected, setConnected] = useState(false);
  const [lastRun, setLastRun] = useState<SqueezeRunEvent | null>(null);
  const [docCounts, setDocCounts] = useState<Record<string, number>>({});
  const [docTotal, setDocTotal] = useState(0);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!enabled || typeof window === "undefined" || !("EventSource" in window)) {
      return;
    }
    const es = new EventSource(SQUEEZE_STREAM_URL);
    sourceRef.current = es;

    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false); // EventSource retries on its own

    es.onmessage = (msg) => {
      let event: SqueezeStreamEvent;
      try {
        event = JSON.parse(msg.data) as SqueezeStreamEvent;
      } catch {
        return; // keepalives arrive as comments and never hit onmessage anyway
      }
      if (event.type === "squeeze_run") {
        setLastRun(event);
        setDocCounts({}); // density restarts "since last ranking"
      } else if (event.type === "doc") {
        setDocTotal((n) => n + 1);
        setDocCounts((prev) => {
          const next = { ...prev };
          for (const t of event.tickers) next[t] = (next[t] ?? 0) + 1;
          return next;
        });
      }
    };

    return () => {
      es.close();
      sourceRef.current = null;
      setConnected(false);
    };
  }, [enabled]);

  return { connected, lastRun, docCounts, docTotal };
}
