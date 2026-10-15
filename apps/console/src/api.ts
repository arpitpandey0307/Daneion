// Typed client for the controller's console API (same origin; Vite proxies it in dev).

export type WorkerState = "ONLINE" | "SUSPECT" | "LOST" | "PAUSED" | "REVOKED";
export type JobState =
  | "SUBMITTED" | "VALIDATING" | "ADMITTED" | "FITCHECK" | "QUEUED" | "RUNNING"
  | "SUCCEEDED" | "FAILED" | "CANCELLED" | "REJECTED";
export type Mode = "auto" | "local" | "distributed";

export interface Capabilities {
  cpu?: string;
  cores_physical?: number;
  cores_logical?: number;
  cpu_mhz?: number | null;
  ram_gb?: number;
  gpus?: { name: string; memory_mb: number; driver: string }[];
  arch?: string;
  calibration?: Record<string, number>;
  net?: { rtt_ms: number; down_mbps: number; up_mbps: number };
  task_memory_mb?: number;
}

export interface Worker {
  id: string;
  name: string;
  is_local: boolean;
  state: WorkerState;
  os: string;
  capabilities: Capabilities;
  slots: number;
  perf: Record<string, number>;
  running: number;
  completed: number;
  last_seen_s: number;
}

export interface FitNode {
  name: string;
  local: boolean;
  slots: number;
  rate: number;
  source: "observed" | "calibrated" | "default";
  share: number;
}

export interface FitCheck {
  placement: "local" | "distributed";
  reason: string;
  reasons: string[];
  predicted_local_s: number | null;
  predicted_distributed_s: number | null;
  predicted_gain: number | null;
  confidence: number;
  nodes: FitNode[];
  estimate: { work_units: number; task_count: number; bytes_in: number; bytes_out: number;
              quality: number; note: string; merge_s: number };
}

export interface Job {
  id: string;
  handler: string;
  params: Record<string, unknown>;
  mode: Mode;
  placement: "local" | "distributed" | null;
  fitcheck: FitCheck | null;
  state: JobState;
  reason: string | null;
  task_count: number;
  task_states: Partial<Record<string, number>>;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  wall_s: number | null;
  result_sha256: string | null;
  has_result: boolean;
  per_worker?: { worker_id: string; name: string; tasks: number; busy_s: number }[];
}

export interface TaskRow {
  id: string;
  idx: number;
  state: string;
  attempts: number;
  fence: number;
  worker_id: string | null;
  last_error: string | null;
  x0: number | null;
  y0: number | null;
  tw: number | null;
  th: number | null;
}

export interface Workload {
  name: string;
  title: string;
  presets: Record<string, Record<string, unknown>>;
}

export interface EventItem {
  id: number;
  ts: number;
  kind: string;
  job_id: string | null;
  task_id: string | null;
  worker_id: string | null;
  message: string;
  data: Record<string, unknown>;
}

export interface BenchRun {
  mode: "local" | "distributed";
  warmup: boolean;
  job_id: string;
  wall_s: number;
  result_sha256: string;
  workers: { name: string; tasks: number }[];
  predicted_s: number | null;
}

export interface BenchModeStats {
  runs: number;
  median_s: number;
  min_s: number;
  max_s: number;
  predicted_s: number | null;
  prediction_error: number | null;
}

export interface Benchmark {
  id: string;
  handler: string;
  params: Record<string, unknown>;
  runs: number;
  warmup: number;
  state: "RUNNING" | "DONE" | "FAILED" | "CANCELLED";
  created_at: number;
  finished_at: number | null;
  results: BenchRun[];
  report: {
    modes: Partial<Record<"local" | "distributed", BenchModeStats>>;
    speedup?: number;
    time_saved_s?: number;
    identical_outputs: boolean;
    result_sha256: string | null;
  } | null;
  error: string | null;
  total_runs: number;
}

export interface SystemInfo {
  hostname: string;
  version: string;
  data_dir: string;
  max_nodes: number;
  lan: { fingerprint: string; port: number; addresses: string[] } | null;
  samples: { path: string; count: number; generating: boolean };
}

export interface Pairing {
  code: string;
  expires_at: number;
  ttl_s: number;
  fingerprint: string;
  lan_port: number | null;
  addresses: string[];
  command: string;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let code = "HTTP_" + res.status;
    let message = res.statusText;
    try {
      const body = await res.json();
      code = body?.error?.code ?? code;
      message = body?.error?.message ?? body?.detail?.[0]?.msg ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, message);
  }
  return res.json() as Promise<T>;
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  system: () => request<SystemInfo>("/system"),
  workers: () => request<Worker[]>("/workers"),
  workerAction: (id: string, action: "pause" | "resume" | "revoke") =>
    post<{ ok: boolean }>(`/workers/${encodeURIComponent(id)}/${action}`),
  workloads: () => request<Workload[]>("/workloads"),
  jobs: (limit = 30) => request<Job[]>(`/jobs?limit=${limit}`),
  job: (id: string) => request<Job>(`/jobs/${id}`),
  tasks: (id: string) => request<TaskRow[]>(`/jobs/${id}/tasks`),
  submit: (handler: string, params: Record<string, unknown>, mode: Mode) =>
    post<Job>("/jobs", { handler, params, mode }),
  cancel: (id: string) => post<Job>(`/jobs/${id}/cancel`),
  resultUrl: (id: string) => `/api/v1/jobs/${id}/result`,
  pair: () => post<Pairing>("/pairing"),
  timeline: (tail = 80) => request<EventItem[]>(`/events?tail=${tail}&important=true`),
  benchmarks: () => request<Benchmark[]>("/benchmarks"),
  startBenchmark: (handler: string, params: Record<string, unknown>, runs: number,
                   warmup: number) => post<Benchmark>("/benchmarks", { handler, params, runs, warmup }),
  cancelBenchmark: (id: string) => post<Benchmark>(`/benchmarks/${id}/cancel`),
  makeSamples: (count = 160) => post<SystemInfo["samples"]>(`/samples/photos?count=${count}`),
};

export const TERMINAL: JobState[] = ["SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"];
