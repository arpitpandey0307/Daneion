import type { ButtonHTMLAttributes, ReactNode } from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function Card({ title, icon, actions, children, className }: {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cx(
        "rounded-2xl border border-ink-700/70 bg-ink-900/80 shadow-[0_1px_0_0_rgba(255,255,255,0.03)_inset,0_20px_40px_-24px_rgba(0,0,0,0.6)] backdrop-blur",
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-ink-700/60 px-5 py-3.5">
          <h2 className="flex items-center gap-2 text-[13px] font-semibold uppercase tracking-[0.08em] text-ink-300">
            {icon}
            {title}
          </h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

type Tone = "neutral" | "brand" | "good" | "warn" | "bad" | "amber";

const TONES: Record<Tone, string> = {
  neutral: "bg-ink-700/60 text-ink-200 ring-ink-600/60",
  brand: "bg-cyan-400/10 text-cyan-300 ring-cyan-400/30",
  good: "bg-emerald-400/10 text-emerald-300 ring-emerald-400/30",
  warn: "bg-amber-400/10 text-amber-300 ring-amber-400/30",
  amber: "bg-amber-400/10 text-amber-300 ring-amber-400/30",
  bad: "bg-rose-400/10 text-rose-300 ring-rose-400/30",
};

export function Badge({ tone = "neutral", children, className }: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

type Variant = "primary" | "ghost" | "danger" | "subtle";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-cyan-400 text-ink-950 hover:bg-cyan-300 shadow-[0_8px_24px_-8px_rgba(34,211,238,0.6)] disabled:bg-ink-600 disabled:text-ink-300 disabled:shadow-none",
  subtle: "bg-ink-700/70 text-ink-100 hover:bg-ink-600 ring-1 ring-inset ring-ink-600",
  ghost: "text-ink-300 hover:text-ink-100 hover:bg-ink-700/60",
  danger: "text-rose-300 hover:bg-rose-400/10 ring-1 ring-inset ring-rose-400/30",
};

export function Button({ variant = "subtle", className, children, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...rest}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-xl px-3.5 py-2 text-sm font-medium transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-400 disabled:cursor-not-allowed",
        VARIANTS[variant],
        className,
      )}
    >
      {children}
    </button>
  );
}

export function Segmented<T extends string>({ value, onChange, options, className }: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode; hint?: string }[];
  className?: string;
}) {
  return (
    <div className={cx("inline-flex rounded-xl bg-ink-850 p-1 ring-1 ring-inset ring-ink-700", className)}
         role="radiogroup">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          title={o.hint}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded-lg px-3 py-1.5 text-sm font-medium transition-all",
            value === o.value
              ? "bg-ink-600 text-ink-100 shadow-sm"
              : "text-ink-400 hover:text-ink-200",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Stat({ label, value, sub, className }: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <div className="text-[11px] font-medium uppercase tracking-wider text-ink-400">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular text-ink-100">{value}</div>
      {sub && <div className="text-xs text-ink-400">{sub}</div>}
    </div>
  );
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      {icon && <div className="text-ink-500">{icon}</div>}
      <div className="text-sm font-medium text-ink-200">{title}</div>
      {children && <div className="max-w-sm text-sm text-ink-400">{children}</div>}
    </div>
  );
}

export function Bar({ value, max, color, className, height = 8 }: {
  value: number;
  max: number;
  color: string;
  className?: string;
  height?: number;
}) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className={cx("w-full overflow-hidden rounded-full bg-ink-800", className)} style={{ height }}>
      <div className="h-full rounded-full transition-[width] duration-500"
           style={{ width: `${pct}%`, background: color }} />
    </div>
  );
}

export function Dot({ color, pulse }: { color: string; pulse?: boolean }) {
  return (
    <span className="relative inline-flex h-2.5 w-2.5">
      {pulse && <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60"
                      style={{ background: color }} />}
      <span className="relative inline-flex h-2.5 w-2.5 rounded-full" style={{ background: color }} />
    </span>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-medium text-ink-300">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-400">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "w-full rounded-xl bg-ink-850 px-3 py-2 text-sm text-ink-100 ring-1 ring-inset ring-ink-700 placeholder:text-ink-500 focus:outline-none focus:ring-2 focus:ring-cyan-400/70";
