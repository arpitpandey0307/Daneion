import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle, Ban, CheckCircle2, Clock, Gauge, History, KeyRound, Laptop, ListTree,
  RotateCcw, Scale, Send, Unplug, XCircle,
} from "lucide-react";
import type { ReactNode } from "react";
import { api, TERMINAL, type EventItem } from "../api";
import { fmtSeconds, handlerLabel, timeAgo } from "../format";
import { useLive } from "../live";
import { Badge, Card, Empty, cx } from "./ui";

function eventLook(kind: string): { icon: ReactNode; color: string } {
  const i = "h-3.5 w-3.5";
  if (kind === "job.succeeded") return { icon: <CheckCircle2 className={i} />, color: "text-emerald-300" };
  if (kind === "job.failed" || kind === "task.failed" || kind === "job.rejected")
    return { icon: <XCircle className={i} />, color: "text-rose-300" };
  if (kind === "job.cancelled") return { icon: <Ban className={i} />, color: "text-ink-300" };
  if (kind === "job.fitcheck") return { icon: <Scale className={i} />, color: "text-cyan-300" };
  if (kind === "task.retry") return { icon: <RotateCcw className={i} />, color: "text-amber-300" };
  if (kind === "worker.lost" || kind === "worker.revoked") return { icon: <Unplug className={i} />, color: "text-rose-300" };
  if (kind === "worker.suspect") return { icon: <AlertTriangle className={i} />, color: "text-amber-300" };
  if (kind.startsWith("worker.")) return { icon: <Laptop className={i} />, color: "text-emerald-300" };
  if (kind.startsWith("pairing.")) return { icon: <KeyRound className={i} />, color: kind.endsWith("failed") ? "text-rose-300" : "text-ink-300" };
  if (kind.startsWith("bench.")) return { icon: <Gauge className={i} />, color: "text-cyan-300" };
  if (kind.startsWith("job.")) return { icon: <Send className={i} />, color: "text-ink-300" };
  return { icon: <Clock className={i} />, color: "text-ink-400" };
}

export function Timeline({ onSelectJob }: { onSelectJob: (id: string) => void }) {
  const { timeline } = useLive();
  return (
    <Card title="Live timeline" icon={<ListTree className="h-4 w-4" />} className="flex flex-col">
      {timeline.length === 0 ? (
        <Empty title="Quiet so far">Events appear here as they happen.</Empty>
      ) : (
        <ol className="-mx-2 max-h-[560px] space-y-0.5 overflow-y-auto pr-1">
          {timeline.map((e: EventItem) => {
            const look = eventLook(e.kind);
            return (
              <li key={e.id}>
                <button type="button" disabled={!e.job_id}
                        onClick={() => e.job_id && onSelectJob(e.job_id)}
                        className="flex w-full items-start gap-2.5 rounded-lg px-2 py-1.5 text-left enabled:hover:bg-ink-800/70">
                  <span className={cx("mt-0.5 shrink-0", look.color)}>{look.icon}</span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-[13px] leading-snug text-ink-200">{e.message}</span>
                    <span className="text-[11px] text-ink-500">{timeAgo(e.ts)} · {e.kind}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}

const STATE_TONE: Record<string, "good" | "bad" | "brand" | "neutral" | "warn"> = {
  SUCCEEDED: "good", FAILED: "bad", REJECTED: "bad", CANCELLED: "neutral", RUNNING: "brand",
};

export function JobsList({ selected, onSelect }: { selected: string | null; onSelect: (id: string) => void }) {
  const jobs = useQuery({
    queryKey: ["jobs"], queryFn: () => api.jobs(25),
    // While something runs, keep its elapsed time ticking; otherwise idle polling.
    refetchInterval: (q) => (q.state.data?.some((j) => !TERMINAL.includes(j.state)) ? 1000 : 5000),
  });
  return (
    <Card title="Recent jobs" icon={<History className="h-4 w-4" />}>
      {!jobs.data?.length ? (
        <Empty title="No jobs yet" />
      ) : (
        <ul className="-mx-2 max-h-[420px] space-y-0.5 overflow-y-auto">
          {jobs.data.map((j) => (
            <li key={j.id}>
              <button type="button" onClick={() => onSelect(j.id)}
                      className={cx("flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left",
                                    j.id === selected ? "bg-ink-800 ring-1 ring-inset ring-ink-600" : "hover:bg-ink-800/60")}>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium text-ink-100">{handlerLabel(j.handler)}</span>
                  <span className="text-[11px] text-ink-400">
                    {j.placement === "distributed" ? "cluster" : j.placement === "local" ? "local" : "—"}
                    {j.mode === "auto" ? " · FitCheck" : " · manual"} · {timeAgo(j.created_at)}
                  </span>
                </span>
                <span className="text-right">
                  <span className="block text-[13px] tabular text-ink-200">{fmtSeconds(j.wall_s)}</span>
                  <Badge tone={STATE_TONE[j.state] ?? "warn"} className="mt-0.5">{j.state.toLowerCase()}</Badge>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
