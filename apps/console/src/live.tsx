// Live updates: one Server-Sent Events stream drives cache invalidation and the timeline.
// If the stream drops, the browser reconnects with Last-Event-ID and queries keep polling.

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type EventItem } from "./api";

const TILE_CHATTER = new Set(["task.leased", "task.running", "task.succeeded"]);
const MAX_TIMELINE = 120;

interface LiveState {
  connected: boolean;
  timeline: EventItem[];
}

const LiveContext = createContext<LiveState>({ connected: false, timeline: [] });

export function LiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [timeline, setTimeline] = useState<EventItem[]>([]);
  const pending = useRef(new Set<string>());
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.timeline(60).then((items) => {
      if (!cancelled) setTimeline(items.slice().reverse());
    }).catch(() => undefined);

    // Coalesce bursts (a 160-tile job emits ~500 events) into one refresh per 300 ms.
    const flush = () => {
      timer.current = null;
      for (const key of pending.current) {
        const [kind, id] = key.split("|");
        if (kind === "job") {
          qc.invalidateQueries({ queryKey: ["job", id] });
          qc.invalidateQueries({ queryKey: ["tasks", id] });
        } else {
          qc.invalidateQueries({ queryKey: [kind] });
        }
      }
      pending.current.clear();
    };
    const mark = (key: string) => {
      pending.current.add(key);
      if (timer.current == null) timer.current = window.setTimeout(flush, 300);
    };

    const es = new EventSource("/api/v1/events/stream");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("daneion", (msg) => {
      const ev = JSON.parse((msg as MessageEvent).data) as EventItem;
      if (ev.job_id) mark(`job|${ev.job_id}`);
      if (ev.kind.startsWith("task.") || ev.kind.startsWith("worker.")) mark("workers");
      if (ev.kind.startsWith("job.")) mark("jobs");
      if (ev.kind.startsWith("bench.")) mark("benchmarks");
      if (ev.kind.startsWith("samples.") || ev.kind.startsWith("pairing.")) mark("system");
      if (!TILE_CHATTER.has(ev.kind)) {
        setTimeline((t) => [ev, ...t].slice(0, MAX_TIMELINE));
      }
    });
    return () => {
      cancelled = true;
      es.close();
      if (timer.current != null) window.clearTimeout(timer.current);
    };
  }, [qc]);

  return <LiveContext.Provider value={{ connected, timeline }}>{children}</LiveContext.Provider>;
}

export const useLive = () => useContext(LiveContext);
