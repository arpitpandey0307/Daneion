import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity, CheckCircle2, CircleSlash, Download, Fingerprint, Layers, Loader2, Scale, XCircle,
} from "lucide-react";
import { useEffect, useState } from "react";
import { api, TERMINAL, type Job, type TaskRow, type Worker } from "../api";
import { fmtBytes, fmtPct, fmtSeconds, handlerLabel, shortName, workerColors } from "../format";
import { Badge, Bar, Button, Card, Empty, Segmented, Stat, cx } from "./ui";

// ------------------------------------------------------------------ helpers
function useNow(active: boolean) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(Date.now() / 1000), 200);
    return () => window.clearInterval(t);
  }, [active]);
  return now;
}

function stateTone(state: string): "good" | "bad" | "warn" | "brand" | "neutral" {
  if (state === "SUCCEEDED") return "good";
  if (state === "FAILED" || state === "REJECTED") return "bad";
  if (state === "CANCELLED") return "neutral";
  if (state === "RUNNING") return "brand";
  return "warn";
}

// ------------------------------------------------------------------ tile map
function TileMap({ job, tasks, colors, workers, view }: {
  job: Job;
  tasks: TaskRow[];
  colors: Map<string, string>;
  workers: Worker[];
  view: "image" | "tiles";
}) {
  const p = job.params as Record<string, number>;
  const spatial = tasks.length > 0 && tasks[0].x0 != null;
  const W = spatial ? p.width : Math.ceil(Math.sqrt(tasks.length * 16 / 9));
  const H = spatial ? p.height : Math.ceil(tasks.length / Math.max(1, W));
  const unit = 1;
  const withImage = spatial && job.has_result && job.handler === "fractal_tiles_v1";
  const names = new Map(workers.map((w) => [w.id, shortName(w.name)]));

  const rect = (t: TaskRow) => spatial
    ? { x: t.x0!, y: t.y0!, w: t.tw!, h: t.th! }
    : { x: (t.idx % W) * unit, y: Math.floor(t.idx / W) * unit, w: unit, h: unit };

  return (
    <div className="relative overflow-hidden rounded-xl bg-ink-950 ring-1 ring-inset ring-ink-700">
      <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" preserveAspectRatio="xMidYMid meet"
           role="img" aria-label="Task map coloured by laptop">
        {withImage && (
          <image href={api.resultUrl(job.id)} x={0} y={0} width={W} height={H} preserveAspectRatio="none" />
        )}
        {view === "tiles" && tasks.map((t) => {
          const r = rect(t);
          const color = (t.worker_id && colors.get(t.worker_id)) || "#45507a";
          const pad = spatial ? Math.max(1, Math.min(r.w, r.h) * 0.04) : 0.06;
          const done = t.state === "SUCCEEDED";
          const live = ["LEASED", "ACCEPTED", "RUNNING", "VERIFYING"].includes(t.state);
          const failed = t.state === "FAILED";
          const waiting = t.state === "RETRY_WAIT";
          if (withImage) {
            // Finished render: tint each tile with the colour of the laptop that computed it.
            return (
              <rect key={t.id} x={r.x} y={r.y} width={r.w} height={r.h} fill={color} fillOpacity={0.42}
                    stroke="#070a12" strokeOpacity={0.55} strokeWidth={pad}>
                <title>{`Tile ${t.idx} · ${names.get(t.worker_id ?? "") ?? "?"}`}</title>
              </rect>
            );
          }
          return (
            <rect key={t.id} x={r.x + pad / 2} y={r.y + pad / 2} width={r.w - pad} height={r.h - pad}
                  rx={spatial ? Math.min(r.w, r.h) * 0.08 : 0.08}
                  fill={failed ? "#fb7185" : done ? color : live ? color : waiting ? "#fbbf24" : "#141b31"}
                  fillOpacity={done ? 0.85 : live ? 0.45 : waiting ? 0.5 : 1}
                  stroke={live ? color : "none"} strokeWidth={live ? pad * 1.2 : 0}
                  className={live ? "animate-pulse-soft" : undefined}>
              <title>{`Tile ${t.idx} · ${t.state.toLowerCase()}${t.worker_id ? ` · ${names.get(t.worker_id) ?? ""}` : ""}${t.attempts > 1 ? ` · attempt ${t.attempts}` : ""}`}</title>
            </rect>
          );
        })}
      </svg>
    </div>
  );
}

// ------------------------------------------------------------------ fitcheck receipt
function FitCheckReceipt({ job }: { job: Job }) {
  const fc = job.fitcheck;
  if (!fc) return null;
  const local = fc.predicted_local_s;
  const dist = fc.predicted_distributed_s;
  const actual = job.state === "SUCCEEDED" && job.finished_at ? job.finished_at - job.created_at : null;
  const max = Math.max(local ?? 0, dist ?? 0, actual ?? 0) * 1.08 || 1;
  const chosen = fc.placement;
  const override = fc.reason.includes("override");

  const row = (label: string, value: number | null, color: string, isChosen: boolean) => (
    <div className="grid grid-cols-[110px_1fr_64px] items-center gap-3 text-xs">
      <span className={cx("truncate", isChosen ? "font-semibold text-ink-100" : "text-ink-400")}>{label}</span>
      <div className="relative h-3 rounded-full bg-ink-800">
        {value != null && (
          <div className="absolute inset-y-0 left-0 rounded-full transition-[width] duration-700"
               style={{ width: `${(value / max) * 100}%`, background: color, opacity: isChosen ? 1 : 0.45 }} />
        )}
        {isChosen && actual != null && (
          <div className="absolute -top-1 -bottom-1 w-0.5 rounded bg-ink-100"
               style={{ left: `${(actual / max) * 100}%` }} title={`Actual ${fmtSeconds(actual)}`} />
        )}
      </div>
      <span className="text-right tabular text-ink-200">{fmtSeconds(value)}</span>
    </div>
  );

  return (
    <div className="rounded-2xl bg-ink-850/70 p-4 ring-1 ring-inset ring-ink-700">
      <div className="flex flex-wrap items-center gap-2">
        <Scale className="h-4 w-4 text-cyan-300" />
        <span className="text-sm font-semibold text-ink-100">FitCheck</span>
        <Badge tone={chosen === "distributed" ? "brand" : "amber"}>
          {chosen === "distributed" ? "Distributed" : "Kept local"}
        </Badge>
        {override && <Badge tone="neutral">manual override</Badge>}
        <span className="ml-auto text-xs text-ink-400">
          confidence <span className="font-semibold tabular text-ink-200">{fmtPct(fc.confidence)}</span>
        </span>
      </div>
      <p className="mt-2 text-sm text-ink-200">{fc.reason}</p>

      <div className="mt-4 space-y-2.5">
        {row("This laptop", local, "#fbbf24", chosen === "local")}
        {row("Whole cluster", dist, "#22d3ee", chosen === "distributed")}
      </div>
      <div className="mt-2 flex items-center justify-between text-[11px] text-ink-400">
        <span>Bars: predicted · white line: actual</span>
        {actual != null && (
          <span>actual <span className="tabular text-ink-200">{fmtSeconds(actual)}</span>
            {(chosen === "local" ? local : dist) != null && (
              <> · error <span className="tabular text-ink-200">
                {fmtPct(((chosen === "local" ? local! : dist!) / actual) - 1)}
              </span></>
            )}
          </span>
        )}
      </div>

      <ul className="mt-3 space-y-1 border-t border-ink-700/60 pt-3 text-xs text-ink-400">
        {fc.reasons.map((r, i) => <li key={i} className="flex gap-2"><span className="text-ink-600">•</span>{r}</li>)}
      </ul>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-500">
        <span>{fc.estimate.task_count} tasks</span>
        <span>{fc.estimate.work_units.toFixed(0)} work units</span>
        <span>{fmtBytes(fc.estimate.bytes_in + fc.estimate.bytes_out)} to move if shared</span>
        {fc.estimate.note && <span>{fc.estimate.note}</span>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ contribution
function Contribution({ job, colors }: { job: Job; colors: Map<string, string> }) {
  const rows = (job.per_worker ?? []).slice().sort((a, b) => b.tasks - a.tasks);
  const total = rows.reduce((s, r) => s + r.tasks, 0);
  if (!rows.length) return null;
  return (
    <div className="space-y-2.5">
      {rows.map((r) => (
        <div key={r.worker_id} className="text-xs">
          <div className="mb-1 flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-ink-200">
              <span className="h-2 w-2 rounded-full" style={{ background: colors.get(r.worker_id) }} />
              {shortName(r.name)}
            </span>
            <span className="tabular text-ink-400">
              {r.tasks} tasks · {fmtPct(r.tasks / Math.max(1, total))} · busy {fmtSeconds(r.busy_s)}
            </span>
          </div>
          <Bar value={r.tasks} max={total} color={colors.get(r.worker_id) ?? "#22d3ee"} />
        </div>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ main
export function JobView({ jobId }: { jobId: string | null }) {
  const workers = useQuery({ queryKey: ["workers"], queryFn: api.workers, refetchInterval: 5000 });
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => (q.state.data && TERMINAL.includes(q.state.data.state) ? false : 1500),
  });
  const tasks = useQuery({
    queryKey: ["tasks", jobId],
    queryFn: () => api.tasks(jobId!),
    enabled: !!jobId,
    refetchInterval: () => (job.data && TERMINAL.includes(job.data.state) ? false : 1500),
  });
  const cancel = useMutation({ mutationFn: () => api.cancel(jobId!) });
  const [view, setView] = useState<"tiles" | "image">("tiles");
  const j = job.data;
  const running = !!j && !TERMINAL.includes(j.state);
  const now = useNow(running);
  useEffect(() => setView("tiles"), [jobId]);
  useEffect(() => {
    if (j?.state === "SUCCEEDED" && j.handler === "fractal_tiles_v1") setView("image");
  }, [j?.state, j?.handler]);

  const colors = workerColors(workers.data);

  if (!jobId) {
    return (
      <Card title="Current job" icon={<Activity className="h-4 w-4" />}>
        <Empty icon={<Layers className="h-8 w-8" />} title="No job yet">
          Pick a workload above and press Run. The map here fills in tile by tile, coloured by the laptop that did it.
        </Empty>
      </Card>
    );
  }
  if (!j) {
    return <Card title="Current job"><div className="h-72 animate-pulse rounded-xl bg-ink-850" /></Card>;
  }

  const done = j.task_states.SUCCEEDED ?? 0;
  const elapsed = j.started_at ? (j.finished_at ?? now) - j.started_at : 0;
  const workersById = workers.data ?? [];
  const involved = new Set((tasks.data ?? []).map((t) => t.worker_id).filter(Boolean));

  return (
    <Card
      title={<>Job <span className="font-mono normal-case tracking-normal text-ink-400">{j.id.slice(4)}</span></>}
      icon={<Activity className="h-4 w-4" />}
      actions={
        <>
          <Badge tone={stateTone(j.state)}>
            {running && <Loader2 className="h-3 w-3 animate-spin" />}
            {j.state.toLowerCase()}
          </Badge>
          {running && (
            <Button variant="danger" className="px-2.5 py-1 text-xs" onClick={() => cancel.mutate()}>
              <CircleSlash className="h-3.5 w-3.5" /> Cancel
            </Button>
          )}
        </>
      }
    >
      <div className="grid gap-5 lg:grid-cols-[1.35fr_1fr]">
        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <div className="text-base font-semibold text-ink-100">{handlerLabel(j.handler)}</div>
              <div className="text-xs text-ink-400">
                {j.placement === "distributed" ? "Shared across the cluster" : "On this laptop"} ·
                {" "}{j.task_count} tasks
              </div>
            </div>
            {j.handler === "fractal_tiles_v1" && j.has_result && (
              <Segmented value={view} onChange={setView}
                         options={[{ value: "image", label: "Result" }, { value: "tiles", label: "Who did what" }]} />
            )}
          </div>

          {tasks.data && tasks.data.length > 0 ? (
            <TileMap job={j} tasks={tasks.data} colors={colors} workers={workersById}
                     view={j.has_result && j.handler === "fractal_tiles_v1" ? view : "tiles"} />
          ) : (
            <div className="grid aspect-[16/10] place-items-center rounded-xl bg-ink-950 text-sm text-ink-400 ring-1 ring-inset ring-ink-700">
              {j.state === "REJECTED" ? "Rejected before any task was created" : "Preparing tasks…"}
            </div>
          )}

          <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-ink-300">
            {workersById.filter((w) => involved.has(w.id)).map((w) => (
              <span key={w.id} className="inline-flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-sm" style={{ background: colors.get(w.id) }} />
                {shortName(w.name)}
              </span>
            ))}
            {(j.task_states.RETRY_WAIT ?? 0) > 0 && (
              <span className="inline-flex items-center gap-1.5 text-amber-300">
                <span className="h-2.5 w-2.5 rounded-sm bg-amber-400" /> waiting to retry
              </span>
            )}
          </div>

          {j.handler === "image_batch_v1" && j.has_result && (
            <a href={api.resultUrl(j.id)} target="_blank" rel="noreferrer"
               className="block overflow-hidden rounded-xl ring-1 ring-inset ring-ink-700">
              <img src={api.resultUrl(j.id)} alt="Contact sheet of all thumbnails" className="w-full" />
            </a>
          )}
        </div>

        <div className="min-w-0 space-y-4">
          <div className="grid grid-cols-3 gap-3 rounded-2xl bg-ink-850/70 p-4 ring-1 ring-inset ring-ink-700">
            <Stat label="Progress" value={`${done}/${j.task_count}`} />
            <Stat label="Elapsed" value={fmtSeconds(elapsed)} />
            <Stat label="Retries" value={(tasks.data ?? []).reduce((s, t) => s + Math.max(0, t.attempts - 1), 0)} />
            <Bar value={done} max={j.task_count} color={j.state === "FAILED" ? "#fb7185" : "#22d3ee"}
                 className="col-span-3" />
          </div>

          {j.state === "SUCCEEDED" && (
            <div className="flex items-center gap-3 rounded-2xl bg-emerald-400/10 p-3.5 ring-1 ring-inset ring-emerald-400/25">
              <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-300" />
              <div className="min-w-0 flex-1 text-xs">
                <div className="font-medium text-emerald-200">Every task verified (SHA-256 + format) and merged</div>
                <div className="mt-0.5 flex items-center gap-1 truncate font-mono text-emerald-300/80"
                     title="SHA-256 of the result: identical on every run and every laptop mix">
                  <Fingerprint className="h-3 w-3 shrink-0" />{j.result_sha256?.slice(0, 24)}…
                </div>
              </div>
              <a href={api.resultUrl(j.id)} download
                 className="rounded-lg p-2 text-emerald-200 hover:bg-emerald-400/10" title="Download result">
                <Download className="h-4 w-4" />
              </a>
            </div>
          )}
          {(j.state === "FAILED" || j.state === "REJECTED") && (
            <div className="flex gap-3 rounded-2xl bg-rose-400/10 p-3.5 text-xs text-rose-200 ring-1 ring-inset ring-rose-400/25">
              <XCircle className="h-5 w-5 shrink-0 text-rose-300" />
              <span>{j.reason}</span>
            </div>
          )}

          <FitCheckReceipt job={j} />

          {(j.per_worker?.length ?? 0) > 0 && (
            <div>
              <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-ink-400">Who did the work</div>
              <Contribution job={j} colors={colors} />
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
