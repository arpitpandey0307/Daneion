import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Images, Loader2, Play, Rocket, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type Mode } from "../api";
import { Button, Card, Field, Segmented, cx, inputClass } from "./ui";

const WORKLOADS = [
  {
    name: "fractal_tiles_v1",
    title: "Fractal render",
    blurb: "Heavy CPU, tiny data. The case where sharing wins.",
    icon: Sparkles,
    accent: "#22d3ee",
  },
  {
    name: "image_batch_v1",
    title: "Photo thumbnails",
    blurb: "Light CPU, megabytes per photo. FitCheck should keep it here.",
    icon: Images,
    accent: "#fbbf24",
  },
];

const MODES: { value: Mode; label: string; hint: string }[] = [
  { value: "auto", label: "Auto · FitCheck", hint: "FitCheck predicts both and picks" },
  { value: "local", label: "This laptop", hint: "Run only on the controller laptop" },
  { value: "distributed", label: "Whole cluster", hint: "Use every online laptop" },
];

export function JobLauncher({ onSubmitted }: { onSubmitted: (jobId: string) => void }) {
  const qc = useQueryClient();
  const workloads = useQuery({ queryKey: ["workloads"], queryFn: api.workloads, staleTime: Infinity });
  const system = useQuery({ queryKey: ["system"], queryFn: api.system, refetchInterval: 15000 });
  const [handler, setHandler] = useState("fractal_tiles_v1");
  const [preset, setPreset] = useState("demo");
  const [mode, setMode] = useState<Mode>("auto");
  const [source, setSource] = useState("");

  const samples = system.data?.samples;
  useEffect(() => {
    if (!source && samples && samples.count > 0) setSource(samples.path);
  }, [samples, source]);

  const makeSamples = useMutation({
    mutationFn: () => api.makeSamples(160),
    onSuccess: (s) => {
      setSource(s.path);
      qc.invalidateQueries({ queryKey: ["system"] });
    },
  });

  const submit = useMutation({
    mutationFn: () => {
      const base = workloads.data?.find((w) => w.name === handler)?.presets?.[preset] ?? {};
      const params = handler === "image_batch_v1" ? { ...base, source_dir: source.trim() } : base;
      return api.submit(handler, params, mode);
    },
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      onSubmitted(job.id);
    },
  });

  const needsSource = handler === "image_batch_v1";
  const generating = samples?.generating || makeSamples.isPending;

  return (
    <Card title="Run a job" icon={<Rocket className="h-4 w-4" />}>
      <div className="grid gap-3 sm:grid-cols-2">
        {WORKLOADS.map((w) => {
          const Icon = w.icon;
          const active = handler === w.name;
          return (
            <button key={w.name} type="button" onClick={() => setHandler(w.name)}
                    className={cx(
                      "flex items-start gap-3 rounded-2xl p-4 text-left ring-1 ring-inset transition-all",
                      active ? "bg-ink-800 ring-2" : "bg-ink-850/60 ring-ink-700 hover:bg-ink-800/80",
                    )}
                    style={active ? { boxShadow: `inset 0 0 0 2px ${w.accent}99` } : undefined}>
              <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl"
                   style={{ background: `${w.accent}1a`, color: w.accent }}>
                <Icon className="h-5 w-5" />
              </div>
              <div>
                <div className="text-sm font-semibold text-ink-100">{w.title}</div>
                <div className="mt-0.5 text-xs leading-relaxed text-ink-400">{w.blurb}</div>
              </div>
            </button>
          );
        })}
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-[auto_1fr]">
        <Field label="Size">
          <Segmented value={preset} onChange={setPreset}
                     options={[{ value: "quick", label: "Quick" }, { value: "demo", label: "Demo" }]} />
        </Field>
        <Field label="Where to run">
          <Segmented value={mode} onChange={setMode} options={MODES} className="flex-wrap" />
        </Field>
      </div>

      {needsSource && (
        <div className="mt-4">
          <Field label="Photo folder on this laptop"
                 hint={samples?.count
                   ? `${samples.count} sample photos available in the data folder.`
                   : "Any folder of JPG/PNG photos, or generate demo photos."}>
            <div className="flex gap-2">
              <input className={inputClass} value={source} onChange={(e) => setSource(e.target.value)}
                     placeholder="C:\Users\you\Pictures\Trip" spellCheck={false} />
              <Button type="button" onClick={() => makeSamples.mutate()} disabled={generating}
                      title="Create 160 photo-like JPEGs (about 30 s)">
                {generating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Images className="h-4 w-4" />}
                {generating ? "Creating…" : "Sample photos"}
              </Button>
            </div>
          </Field>
        </div>
      )}

      <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-ink-400">
          {mode === "auto"
            ? "FitCheck predicts local and distributed time, then picks; it shows its reasons."
            : "Explicit choice: FitCheck still shows what it would have done."}
        </p>
        <Button variant="primary" onClick={() => submit.mutate()}
                disabled={submit.isPending || (needsSource && !source.trim()) || generating}>
          {submit.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
          {submit.isPending && handler === "fractal_tiles_v1" ? "Estimating…" : "Run job"}
        </Button>
      </div>
      {submit.isError && (
        <div className="mt-3 rounded-xl bg-rose-400/10 p-3 text-sm text-rose-200 ring-1 ring-inset ring-rose-400/30">
          {(submit.error as Error).message}
        </div>
      )}
    </Card>
  );
}
