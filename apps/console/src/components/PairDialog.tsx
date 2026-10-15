import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, KeyRound, RefreshCw, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, type Pairing } from "../api";
import { Button, cx } from "./ui";

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1500);
        });
      }}
      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-ink-200 ring-1 ring-inset ring-ink-600 hover:bg-ink-700"
    >
      {done ? <Check className="h-3.5 w-3.5 text-emerald-300" /> : <Copy className="h-3.5 w-3.5" />}
      {done ? "Copied" : "Copy"}
    </button>
  );
}

function Countdown({ pairing }: { pairing: Pairing }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), 250);
    return () => window.clearInterval(t);
  }, []);
  const left = Math.max(0, pairing.expires_at - now);
  const frac = left / pairing.ttl_s;
  const r = 20;
  const c = 2 * Math.PI * r;
  return (
    <div className="relative h-14 w-14 shrink-0" title="Code expires">
      <svg viewBox="0 0 48 48" className="h-14 w-14 -rotate-90">
        <circle cx="24" cy="24" r={r} fill="none" stroke="#1d2642" strokeWidth="4" />
        <circle cx="24" cy="24" r={r} fill="none" stroke={left < 20 ? "#fb7185" : "#22d3ee"}
                strokeWidth="4" strokeLinecap="round" strokeDasharray={c}
                strokeDashoffset={c * (1 - frac)} className="transition-[stroke-dashoffset] duration-300" />
      </svg>
      <div className="absolute inset-0 grid place-items-center text-xs font-semibold tabular text-ink-100">
        {Math.ceil(left)}s
      </div>
    </div>
  );
}

export function PairDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const pair = useMutation({ mutationFn: api.pair });
  const workers = useQuery({ queryKey: ["workers"], queryFn: api.workers, enabled: open });
  const [startCount, setStartCount] = useState<number | null>(null);

  const { mutate } = pair;
  useEffect(() => {
    if (open) {
      mutate();
      setStartCount(null);
    }
  }, [open, mutate]);

  const active = useMemo(() => (workers.data ?? []).filter((w) => w.state !== "REVOKED"),
                         [workers.data]);
  useEffect(() => {
    if (open && startCount == null && workers.data) setStartCount(active.length);
  }, [open, startCount, workers.data, active.length]);
  const joined = startCount != null && active.length > startCount ? active[active.length - 1] : null;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const p = pair.data;
  const command = p ? `uv run ${p.command}` : "";

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/70 p-4 backdrop-blur-sm"
         onMouseDown={(e) => e.target === e.currentTarget && onClose()} role="dialog" aria-modal>
      <div className="w-full max-w-xl rounded-2xl border border-ink-700 bg-ink-900 shadow-2xl">
        <div className="flex items-start justify-between border-b border-ink-700/70 px-6 py-4">
          <div>
            <h3 className="text-base font-semibold text-ink-100">Add a laptop</h3>
            <p className="mt-0.5 text-sm text-ink-400">
              Run one command on the other laptop. The code works once and expires.
            </p>
          </div>
          <button onClick={onClose} className="rounded-lg p-1.5 text-ink-400 hover:bg-ink-700 hover:text-ink-100"
                  aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-5 px-6 py-5">
          {joined ? (
            <div className="flex items-center gap-3 rounded-xl bg-emerald-400/10 p-4 ring-1 ring-inset ring-emerald-400/30">
              <Check className="h-5 w-5 text-emerald-300" />
              <div className="text-sm">
                <span className="font-semibold text-emerald-200">{joined.name}</span>
                <span className="text-emerald-300/80"> joined with {joined.slots} slots.</span>
              </div>
            </div>
          ) : pair.isError ? (
            <div className="rounded-xl bg-rose-400/10 p-4 text-sm text-rose-200 ring-1 ring-inset ring-rose-400/30">
              {(pair.error as Error).message}
            </div>
          ) : !p ? (
            <div className="h-28 animate-pulse rounded-xl bg-ink-800" />
          ) : (
            <>
              <div className="flex items-center gap-5">
                <Countdown pairing={p} />
                <div>
                  <div className="text-[11px] font-medium uppercase tracking-wider text-ink-400">
                    Pairing code
                  </div>
                  <div className="font-mono text-4xl font-semibold tracking-[0.2em] text-ink-100">
                    {p.code}
                  </div>
                </div>
              </div>

              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="text-xs font-medium text-ink-300">On the other laptop, in the Daneion folder:</span>
                  <CopyButton text={command} />
                </div>
                <pre className="overflow-x-auto rounded-xl bg-ink-950 p-3.5 font-mono text-[13px] text-cyan-200 ring-1 ring-inset ring-ink-700">
                  {command}
                </pre>
                {p.addresses.length > 1 && (
                  <p className="mt-1.5 text-xs text-ink-400">
                    Other addresses of this laptop: {p.addresses.slice(1).join(", ")}
                  </p>
                )}
                {p.addresses.length === 0 && (
                  <p className="mt-1.5 text-xs text-amber-300">
                    No Wi-Fi/LAN address found. Connect this laptop to the hotspot first.
                  </p>
                )}
              </div>

              <div className="flex items-start gap-2.5 rounded-xl bg-ink-850 p-3.5 text-xs text-ink-300 ring-1 ring-inset ring-ink-700">
                <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-cyan-300" />
                <div>
                  The other laptop checks this controller's certificate fingerprint{" "}
                  <span className="font-mono text-ink-100">{p.fingerprint}</span> before sending anything,
                  then gets its own private token. Can't connect? Same Wi-Fi, and run{" "}
                  <span className="font-mono text-ink-100">daneion firewall</span> here.
                </div>
              </div>
            </>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-ink-700/70 px-6 py-3.5">
          <span className={cx("inline-flex items-center gap-1.5 text-xs",
                              joined ? "text-emerald-300" : "text-ink-400")}>
            <KeyRound className="h-3.5 w-3.5" />
            {joined ? "Paired" : "Waiting for the laptop to join…"}
          </span>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={() => { pair.mutate(); setStartCount(active.length); }}>
              <RefreshCw className="h-4 w-4" /> New code
            </Button>
            <Button variant="primary" onClick={() => { qc.invalidateQueries({ queryKey: ["workers"] }); onClose(); }}>
              Done
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
