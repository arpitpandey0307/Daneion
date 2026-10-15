import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Download, FileText, Gauge, Loader2, Play, Square, Trophy, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type Benchmark, type BenchModeStats } from "../api";
import { fmtPct, fmtSeconds, handlerLabel, timeAgo } from "../format";
import { Badge, Button, Card, Empty, Field, Segmented, cx } from "./ui";

const MODE_COLOR = { local: "#fbbf24", distributed: "#22d3ee" } as const;
const MODE_LABEL = { local: "This laptop", distributed: "Whole cluster" } as const;

function RangeChart({ modes }: { modes: Partial<Record<"local" | "distributed", BenchModeStats>> }) {
  const entries = (["local", "distributed"] as const).filter((m) => modes[m]);
  const max = Math.max(...entries.map((m) => Math.max(modes[m]!.max_s, modes[m]!.predicted_s ?? 0))) * 1.1;
  return (
    <div className="space-y-4">
      {entries.map((m) => {
        const s = modes[m]!;
        const pct = (v: number) => `${(v / max) * 100}%`;
        return (
          <div key={m}>
            <div className="mb-1.5 flex items-baseline justify-between text-xs">
              <span className="font-medium text-ink-200">{MODE_LABEL[m]}</span>
              <span className="text-ink-400">
                median <span className="text-sm font-semibold tabular text-ink-100">{fmtSeconds(s.median_s, 2)}</span>
                <span className="ml-1.5 tabular">({fmtSeconds(s.min_s)}–{fmtSeconds(s.max_s)}, n={s.runs})</span>
              </span>
            </div>
            <div className="relative h-7 rounded-lg bg-ink-800">
              <div className="absolute inset-y-0 left-0 rounded-lg" style={{ width: pct(s.median_s), background: `${MODE_COLOR[m]}cc` }} />
              <div className="absolute top-1/2 h-0.5 -translate-y-1/2 bg-ink-100/70"
                   style={{ left: pct(s.min_s), width: `calc(${pct(s.max_s)} - ${pct(s.min_s)})` }} />
              <div className="absolute top-1.5 bottom-1.5 w-0.5 bg-ink-100/80" style={{ left: pct(s.min_s) }} />
              <div className="absolute top-1.5 bottom-1.5 w-0.5 bg-ink-100/80" style={{ left: pct(s.max_s) }} />
              {s.predicted_s != null && (
                <div className="absolute -top-1 -bottom-1 border-l-2 border-dashed border-ink-300"
                     style={{ left: pct(s.predicted_s) }} title={`FitCheck predicted ${fmtSeconds(s.predicted_s)}`} />
              )}
            </div>
            {s.predicted_s != null && (
              <div className="mt-1 text-[11px] text-ink-500">
                FitCheck predicted {fmtSeconds(s.predicted_s)} ({s.prediction_error != null && s.prediction_error >= 0 ? "+" : ""}{fmtPct(s.prediction_error)})
              </div>
            )}
          </div>
        );
      })}
      <div className="flex items-center gap-4 text-[11px] text-ink-500">
        <span className="inline-flex items-center gap-1.5"><span className="h-2 w-4 rounded bg-ink-400" /> median</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-4 bg-ink-200" /> min–max</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-3 border-l-2 border-dashed border-ink-300" /> FitCheck prediction</span>
      </div>
    </div>
  );
}

function RunStrip({ b }: { b: Benchmark }) {
  const slots = Array.from({ length: b.total_runs }, (_, i) => b.results[i]);
  return (
    <div className="flex flex-wrap gap-1.5">
      {slots.map((r, i) => (
        <div key={i}
             title={r ? `${MODE_LABEL[r.mode]}${r.warmup ? " (warm-up)" : ""}: ${fmtSeconds(r.wall_s, 2)}` : "pending"}
             className={cx("flex h-8 min-w-14 items-center justify-center rounded-lg px-2 text-[11px] font-medium tabular ring-1 ring-inset",
               r ? "text-ink-950" : "bg-ink-800 text-ink-500 ring-ink-700",
               r?.warmup && "opacity-50")}
             style={r ? { background: MODE_COLOR[r.mode], boxShadow: "none" } : undefined}>
          {r ? fmtSeconds(r.wall_s) : i === b.results.length && b.state === "RUNNING"
            ? <Loader2 className="h-3.5 w-3.5 animate-spin text-ink-300" /> : "·"}
        </div>
      ))}
    </div>
  );
}

export function BenchmarkPanel() {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["benchmarks"], queryFn: api.benchmarks,
    refetchInterval: (q) => (q.state.data?.some((b) => b.state === "RUNNING") ? 1500 : 10000),
  });
  const workloads = useQuery({ queryKey: ["workloads"], queryFn: api.workloads, staleTime: Infinity });
  const [selected, setSelected] = useState<string | null>(null);
  const [runs, setRuns] = useState("3");
  const [preset, setPreset] = useState("demo");

  const current = list.data?.find((b) => b.id === selected) ?? list.data?.[0];
  const running = list.data?.find((b) => b.state === "RUNNING");
  useEffect(() => {
    if (running) setSelected(running.id);
  }, [running?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = useMutation({
    mutationFn: () => {
      const params = workloads.data?.find((w) => w.name === "fractal_tiles_v1")?.presets?.[preset] ?? {};
      return api.startBenchmark("fractal_tiles_v1", params, Number(runs), 1);
    },
    onSuccess: (b) => {
      setSelected(b.id);
      qc.invalidateQueries({ queryKey: ["benchmarks"] });
    },
  });
  const cancel = useMutation({ mutationFn: (id: string) => api.cancelBenchmark(id) });

  const rep = current?.report;

  return (
    <Card title="Proof-of-Speed" icon={<Gauge className="h-4 w-4" />}
          actions={running
            ? <Button variant="danger" className="px-2.5 py-1 text-xs" onClick={() => cancel.mutate(running.id)}>
                <Square className="h-3 w-3" /> Stop
              </Button>
            : null}>
      <div className="flex flex-wrap items-end gap-4">
        <Field label="Fractal size">
          <Segmented value={preset} onChange={setPreset}
                     options={[{ value: "quick", label: "Quick" }, { value: "demo", label: "Demo" }]} />
        </Field>
        <Field label="Measured runs per mode">
          <Segmented value={runs} onChange={setRuns}
                     options={["2", "3", "5"].map((v) => ({ value: v, label: v }))} />
        </Field>
        <Button variant="primary" className="ml-auto" onClick={() => start.mutate()}
                disabled={!!running || start.isPending}>
          {start.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
          Run benchmark
        </Button>
      </div>
      <p className="mt-2 text-xs text-ink-400">
        One warm-up per mode, then local and cluster runs alternate (L D D L…) so heat and turbo affect both equally.
        The same job, the same code, the same output — only the number of laptops changes.
      </p>
      {start.isError && <p className="mt-2 text-sm text-rose-300">{(start.error as Error).message}</p>}

      <div className="mt-5 border-t border-ink-700/60 pt-5">
        {!current ? (
          <Empty icon={<Trophy className="h-8 w-8" />} title="No benchmark yet">
            Run one with every laptop connected. The result is the measured speedup, not a prediction.
          </Empty>
        ) : (
          <div className="space-y-5">
            <div className="flex flex-wrap items-center gap-2 text-xs text-ink-400">
              <span className="font-medium text-ink-200">{handlerLabel(current.handler)}</span>
              <span>· {current.runs} runs per mode · {timeAgo(current.created_at)}</span>
              <Badge tone={current.state === "DONE" ? "good" : current.state === "RUNNING" ? "brand" : "bad"}>
                {current.state === "RUNNING" && <Loader2 className="h-3 w-3 animate-spin" />}
                {current.state.toLowerCase()}
              </Badge>
              {(list.data?.length ?? 0) > 1 && (
                <select className="ml-auto rounded-lg bg-ink-850 px-2 py-1 text-xs text-ink-200 ring-1 ring-inset ring-ink-700"
                        value={current.id} onChange={(e) => setSelected(e.target.value)}>
                  {list.data!.map((b) => (
                    <option key={b.id} value={b.id}>
                      {timeAgo(b.created_at)} · {b.report?.speedup ? `${b.report.speedup.toFixed(2)}×` : b.state.toLowerCase()}
                    </option>
                  ))}
                </select>
              )}
            </div>

            <RunStrip b={current} />

            {rep?.speedup != null && (
              <div className="grid gap-5 md:grid-cols-[220px_1fr]">
                <div className="rounded-2xl bg-gradient-to-br from-cyan-400/15 to-ink-850 p-5 ring-1 ring-inset ring-cyan-400/25">
                  <div className="text-[11px] font-medium uppercase tracking-wider text-cyan-200/80">Measured speedup</div>
                  <div className="mt-1 text-5xl font-semibold tracking-tight tabular text-ink-100">
                    {rep.speedup.toFixed(2)}<span className="text-3xl text-cyan-300">×</span>
                  </div>
                  <div className="mt-1 text-sm text-ink-300">
                    {rep.time_saved_s != null && rep.time_saved_s >= 0
                      ? <>{fmtSeconds(rep.time_saved_s)} saved per job</>
                      : <>cluster was slower here</>}
                  </div>
                  <div className={cx("mt-4 flex items-center gap-1.5 text-xs",
                                     rep.identical_outputs ? "text-emerald-300" : "text-rose-300")}>
                    {rep.identical_outputs ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                    {rep.identical_outputs ? "Identical output in every run" : "Outputs differed!"}
                  </div>
                  {rep.result_sha256 && (
                    <div className="mt-1 truncate font-mono text-[10px] text-ink-500" title={rep.result_sha256}>
                      sha256 {rep.result_sha256.slice(0, 16)}…
                    </div>
                  )}
                  <div className="mt-4 flex gap-2">
                    <a href={`/api/v1/benchmarks/${current.id}/report.html`} target="_blank" rel="noreferrer"
                       className="inline-flex items-center gap-1.5 rounded-lg bg-ink-700/70 px-2.5 py-1.5 text-xs font-medium text-ink-100 ring-1 ring-inset ring-ink-600 hover:bg-ink-600"
                       title="Self-contained page for slides or a backup if the live demo fails">
                      <FileText className="h-3.5 w-3.5" /> Report
                    </a>
                    <a href={`/api/v1/benchmarks/${current.id}/report.html?download=true`}
                       className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-ink-300 ring-1 ring-inset ring-ink-600 hover:bg-ink-700"
                       title="Save the report as one HTML file">
                      <Download className="h-3.5 w-3.5" /> Save
                    </a>
                  </div>
                </div>
                <RangeChart modes={rep.modes} />
              </div>
            )}
            {current.state === "FAILED" && (
              <p className="text-sm text-rose-300">{current.error}</p>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
