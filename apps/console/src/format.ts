import type { Worker } from "./api";

export function fmtSeconds(s: number | null | undefined, digits = 1): string {
  if (s == null || !isFinite(s)) return "—";
  if (s < 60) return `${s.toFixed(digits)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

export function fmtBytes(b: number | null | undefined): string {
  if (b == null) return "—";
  if (b < 1024) return `${b.toFixed(0)} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtPct(x: number | null | undefined, digits = 0): string {
  return x == null ? "—" : `${(x * 100).toFixed(digits)}%`;
}

export function timeAgo(ts: number, now = Date.now() / 1000): string {
  const d = Math.max(0, now - ts);
  if (d < 5) return "just now";
  if (d < 60) return `${Math.floor(d)}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  return `${Math.floor(d / 3600)}h ago`;
}

export const HANDLER_LABEL: Record<string, string> = {
  fractal_tiles_v1: "Fractal render",
  image_batch_v1: "Photo thumbnails",
};

export const handlerLabel = (h: string) => HANDLER_LABEL[h] ?? h;

// One colour per laptop (max 5 in a cluster). The controller's own worker is always cyan.
const PALETTE = ["#22d3ee", "#fbbf24", "#a78bfa", "#34d399", "#fb7185", "#60a5fa"];

export function workerColors(workers: Worker[] | undefined): Map<string, string> {
  const map = new Map<string, string>();
  if (!workers) return map;
  const ordered = [...workers].sort((a, b) => Number(b.is_local) - Number(a.is_local));
  let i = 0;
  for (const w of ordered) {
    map.set(w.id, PALETTE[i % PALETTE.length]);
    map.set(w.name, PALETTE[i % PALETTE.length]);
    i += 1;
  }
  return map;
}

export function shortName(name: string): string {
  return name.replace(/ \(controller\)$/, "");
}

export function cpuShort(cpu?: string): string {
  if (!cpu) return "Unknown CPU";
  return cpu
    .replace(/\(R\)|\(TM\)|CPU|Processor|with Radeon Graphics|\d+-Core/gi, "")
    .replace(/\s+@.*$/, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

export function gpuShort(name?: string): string {
  return (name ?? "").replace(/NVIDIA |GeForce |Laptop GPU/g, "").trim();
}
