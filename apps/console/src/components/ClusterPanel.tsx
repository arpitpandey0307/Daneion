import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu, Gauge, Laptop, MemoryStick, MonitorSmartphone, Pause, Play, Trash2, Zap } from "lucide-react";
import { useState } from "react";
import { api, type Worker } from "../api";
import { cpuShort, gpuShort, shortName, workerColors } from "../format";
import { Badge, Bar, Button, Card, Dot, Empty } from "./ui";

const STATE_STYLE: Record<string, { color: string; label: string; tone: "good" | "warn" | "bad" | "neutral" }> = {
  ONLINE: { color: "#34d399", label: "Online", tone: "good" },
  SUSPECT: { color: "#fbbf24", label: "Missed heartbeats", tone: "warn" },
  LOST: { color: "#fb7185", label: "Lost", tone: "bad" },
  PAUSED: { color: "#98a3c7", label: "Paused", tone: "neutral" },
  REVOKED: { color: "#45507a", label: "Removed", tone: "neutral" },
};

function WorkerCard({ w, color }: { w: Worker; color: string }) {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const act = useMutation({
    mutationFn: (a: "pause" | "resume" | "revoke") => api.workerAction(w.id, a),
    onSettled: () => qc.invalidateQueries({ queryKey: ["workers"] }),
  });
  const st = STATE_STYLE[w.state] ?? STATE_STYLE.ONLINE;
  const caps = w.capabilities ?? {};
  const gpu = caps.gpus?.[0];
  const speed = w.perf?.fractal_tiles_v1 ?? (caps.calibration?.fractal_tiles_v1 ?? 0) * 0.65;
  const measured = Boolean(w.perf?.fractal_tiles_v1);

  return (
    <div className="group relative overflow-hidden rounded-2xl border border-ink-700/70 bg-ink-850/80 p-4 transition-colors hover:border-ink-600">
      <div className="absolute inset-x-0 top-0 h-0.5" style={{ background: color, opacity: w.state === "ONLINE" ? 1 : 0.3 }} />
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl ring-1 ring-inset"
               style={{ background: `${color}14`, color, borderColor: color, boxShadow: `inset 0 0 0 1px ${color}40` }}>
            {w.is_local ? <MonitorSmartphone className="h-4.5 w-4.5" /> : <Laptop className="h-4.5 w-4.5" />}
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-ink-100" title={w.name}>{shortName(w.name)}</div>
            <div className="flex items-center gap-1.5 text-xs text-ink-400">
              <Dot color={st.color} pulse={w.state === "ONLINE" && w.running > 0} />
              {st.label}
              {w.is_local && <span className="text-ink-500">· controller</span>}
            </div>
          </div>
        </div>
        <Badge tone={w.running > 0 ? "brand" : "neutral"} className="tabular">
          {w.running}/{w.slots} busy
        </Badge>
      </div>

      <Bar value={w.running} max={w.slots} color={color} className="mt-3.5" height={6} />

      <dl className="mt-3.5 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
        <div className="flex items-center gap-1.5 text-ink-300" title={caps.cpu}>
          <Cpu className="h-3.5 w-3.5 shrink-0 text-ink-500" />
          <span className="truncate">{cpuShort(caps.cpu)}</span>
        </div>
        <div className="flex items-center gap-1.5 text-ink-300">
          <MemoryStick className="h-3.5 w-3.5 shrink-0 text-ink-500" />
          {caps.ram_gb ?? "?"} GB · {caps.cores_physical ?? "?"} cores
        </div>
        <div className="flex items-center gap-1.5 text-ink-300" title={gpu ? `${gpu.name}, ${gpu.memory_mb} MB` : "No NVIDIA GPU detected"}>
          <Zap className="h-3.5 w-3.5 shrink-0 text-ink-500" />
          <span className="truncate">{gpu ? `${gpuShort(gpu.name)} · ${(gpu.memory_mb / 1024).toFixed(0)} GB` : "No GPU"}</span>
        </div>
        <div className="flex items-center gap-1.5 text-ink-300" title={measured ? "Measured on real jobs" : "From the startup calibration"}>
          <Gauge className="h-3.5 w-3.5 shrink-0 text-ink-500" />
          <span className="tabular">{speed ? `${(speed * w.slots).toFixed(0)} u/s` : "—"}</span>
          <span className="text-ink-500">{measured ? "measured" : "calibrated"}</span>
        </div>
      </dl>

      <div className="mt-3 flex items-center justify-between border-t border-ink-700/60 pt-3 text-xs text-ink-400">
        <span>
          {w.is_local ? "This laptop" : caps.net
            ? <>Link <span className="tabular text-ink-200">{caps.net.down_mbps.toFixed(0)}</span> Mbit/s · <span className="tabular text-ink-200">{caps.net.rtt_ms.toFixed(0)}</span> ms</>
            : "Link not measured"}
          <span className="ml-2 text-ink-500">· {w.completed} tiles done</span>
        </span>
        {!w.is_local && w.state !== "REVOKED" && (
          <div className="flex items-center gap-1 opacity-70 transition-opacity group-hover:opacity-100">
            {w.state === "PAUSED" ? (
              <button className="rounded-lg p-1.5 hover:bg-ink-700 hover:text-ink-100" title="Resume: accept new work"
                      onClick={() => act.mutate("resume")}><Play className="h-3.5 w-3.5" /></button>
            ) : (
              <button className="rounded-lg p-1.5 hover:bg-ink-700 hover:text-ink-100" title="Pause: finish current tiles, take no new ones"
                      onClick={() => act.mutate("pause")}><Pause className="h-3.5 w-3.5" /></button>
            )}
            {confirm ? (
              <Button variant="danger" className="px-2 py-1 text-xs" onClick={() => act.mutate("revoke")}
                      onMouseLeave={() => setConfirm(false)}>Remove?</Button>
            ) : (
              <button className="rounded-lg p-1.5 hover:bg-rose-400/10 hover:text-rose-300" title="Remove this laptop (revokes its token)"
                      onClick={() => setConfirm(true)}><Trash2 className="h-3.5 w-3.5" /></button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export function ClusterPanel({ onAddLaptop }: { onAddLaptop: () => void }) {
  const workers = useQuery({ queryKey: ["workers"], queryFn: api.workers, refetchInterval: 5000 });
  const colors = workerColors(workers.data);
  const list = (workers.data ?? [])
    .filter((w) => w.state !== "REVOKED")
    .sort((a, b) => Number(b.is_local) - Number(a.is_local));

  return (
    <Card title="Cluster" icon={<Laptop className="h-4 w-4" />}
          actions={<span className="text-xs text-ink-400">{list.length} of 5 laptops</span>}>
      {workers.isLoading ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => <div key={i} className="h-44 animate-pulse rounded-2xl bg-ink-850" />)}
        </div>
      ) : list.length === 0 ? (
        <Empty icon={<Laptop className="h-8 w-8" />} title="No laptops yet">
          Start the controller with its local worker, or add a laptop.
        </Empty>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {list.map((w) => <WorkerCard key={w.id} w={w} color={colors.get(w.id) ?? "#22d3ee"} />)}
          {list.length < 5 && (
            <button onClick={onAddLaptop}
                    className="flex min-h-44 flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-ink-600 text-sm text-ink-400 transition-colors hover:border-cyan-400/60 hover:bg-cyan-400/5 hover:text-cyan-200">
              <Laptop className="h-6 w-6" />
              Add a laptop
              <span className="text-xs text-ink-500">pairing code + one command</span>
            </button>
          )}
        </div>
      )}
    </Card>
  );
}
