import { useQuery } from "@tanstack/react-query";
import { Plus, Wifi, WifiOff } from "lucide-react";
import { api } from "../api";
import { useLive } from "../live";
import { Button, cx } from "./ui";

export function Logo() {
  return (
    <svg viewBox="0 0 32 32" className="h-8 w-8" aria-hidden>
      <defs>
        <linearGradient id="lg" x1="0" x2="1" y1="0" y2="1">
          <stop offset="0" stopColor="#22d3ee" />
          <stop offset="1" stopColor="#0e7490" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="#0f1528" stroke="#1d2642" />
      <path d="M10 8.5h6a7.5 7.5 0 010 15h-6z" fill="none" stroke="url(#lg)" strokeWidth="2.6"
            strokeLinejoin="round" />
      <circle cx="16" cy="16" r="2.4" fill="#fbbf24" />
      <circle cx="10" cy="8.5" r="1.6" fill="#22d3ee" />
      <circle cx="10" cy="23.5" r="1.6" fill="#22d3ee" />
    </svg>
  );
}

export function Header({ onAddLaptop }: { onAddLaptop: () => void }) {
  const { connected } = useLive();
  const workers = useQuery({ queryKey: ["workers"], queryFn: api.workers, refetchInterval: 5000 });
  const system = useQuery({ queryKey: ["system"], queryFn: api.system, refetchInterval: 15000 });
  const active = (workers.data ?? []).filter((w) => w.state !== "REVOKED");
  const online = active.filter((w) => w.state === "ONLINE").length;
  const slots = active.filter((w) => w.state === "ONLINE").reduce((s, w) => s + w.slots, 0);
  const max = system.data?.max_nodes ?? 5;

  return (
    <header className="sticky top-0 z-30 border-b border-ink-700/60 bg-ink-950/75 backdrop-blur-xl">
      <div className="mx-auto flex max-w-[1500px] items-center gap-4 px-4 py-3 sm:px-6">
        <div className="flex items-center gap-3">
          <Logo />
          <div className="leading-tight">
            <div className="text-[15px] font-semibold tracking-tight text-ink-100">Daneion</div>
            <div className="text-[11px] text-ink-400">LAN compute sharing</div>
          </div>
        </div>

        <div className="ml-2 hidden items-center gap-2 md:flex">
          <span className="rounded-full bg-ink-800 px-3 py-1 text-xs text-ink-300 ring-1 ring-inset ring-ink-700">
            <span className="font-semibold text-ink-100 tabular">{online}</span> / {max} laptops online
          </span>
          <span className="rounded-full bg-ink-800 px-3 py-1 text-xs text-ink-300 ring-1 ring-inset ring-ink-700">
            <span className="font-semibold text-ink-100 tabular">{slots}</span> CPU slots
          </span>
        </div>

        <div className="ml-auto flex items-center gap-3">
          <span
            className={cx(
              "hidden items-center gap-1.5 text-xs sm:inline-flex",
              connected ? "text-emerald-300" : "text-amber-300",
            )}
            title={connected ? "Live updates connected" : "Reconnecting to the controller…"}
          >
            {connected ? <Wifi className="h-3.5 w-3.5" /> : <WifiOff className="h-3.5 w-3.5" />}
            {connected ? "Live" : "Reconnecting"}
          </span>
          <Button variant="primary" onClick={onAddLaptop} disabled={active.length >= max}
                  title={active.length >= max ? `Cluster is full (${max} laptops)` : undefined}>
            <Plus className="h-4 w-4" /> Add laptop
          </Button>
        </div>
      </div>
    </header>
  );
}
