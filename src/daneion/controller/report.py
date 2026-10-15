"""Self-contained Proof-of-Speed report (FIT-11): one HTML file with the numbers, the chart, the
cluster's hardware and the rendered image. Works offline; it is the recorded fallback when a
live demo is not possible, and it says plainly that it is a recording."""

from __future__ import annotations

import base64
import datetime as dt
from html import escape
from pathlib import Path
from typing import Any

MODE_LABEL = {"local": "This laptop only", "distributed": "Whole cluster"}
MODE_COLOR = {"local": "#fbbf24", "distributed": "#22d3ee"}


def _fmt_s(s: float | None) -> str:
    return "—" if s is None else f"{s:.2f} s"


def _chart(modes: dict[str, Any]) -> str:
    rows = [m for m in ("local", "distributed") if m in modes]
    if not rows:
        return ""
    top = max(max(modes[m]["max_s"], modes[m].get("predicted_s") or 0) for m in rows) * 1.12
    w, label_w, bar_h, gap = 720, 170, 30, 26
    scale = (w - label_w - 90) / top
    h = len(rows) * (bar_h + gap) + 30
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" '
             f'aria-label="Median run time per mode">']
    for i, m in enumerate(rows):
        s = modes[m]
        y = 10 + i * (bar_h + gap)
        x0 = label_w
        parts.append(f'<text x="0" y="{y + bar_h * 0.68}" class="lbl">{MODE_LABEL[m]}</text>')
        parts.append(f'<rect x="{x0}" y="{y}" width="{s["median_s"] * scale:.1f}" '
                     f'height="{bar_h}" rx="6" fill="{MODE_COLOR[m]}"/>')
        cy = y + bar_h / 2
        parts.append(f'<line x1="{x0 + s["min_s"] * scale:.1f}" x2="{x0 + s["max_s"] * scale:.1f}" '
                     f'y1="{cy}" y2="{cy}" stroke="#e6eaf7" stroke-width="2"/>')
        for v in (s["min_s"], s["max_s"]):
            parts.append(f'<line x1="{x0 + v * scale:.1f}" x2="{x0 + v * scale:.1f}" '
                         f'y1="{y + 7}" y2="{y + bar_h - 7}" stroke="#e6eaf7" stroke-width="2"/>')
        if s.get("predicted_s"):
            px = x0 + s["predicted_s"] * scale
            parts.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{y - 4}" y2="{y + bar_h + 4}" '
                         f'stroke="#98a3c7" stroke-width="2" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{x0 + s["max_s"] * scale + 10:.1f}" y="{y + bar_h * 0.68}" '
                     f'class="val">{s["median_s"]:.2f} s</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_report(bench: dict[str, Any], workers: list[dict[str, Any]],
                  image: Path | None) -> str:
    rep = bench.get("report") or {}
    modes = rep.get("modes", {})
    when = dt.datetime.fromtimestamp(bench["created_at"]).strftime("%d %b %Y, %H:%M")
    speedup = rep.get("speedup")
    img_html = ""
    if image and image.exists() and image.stat().st_size < 12 * 1024 * 1024:
        b64 = base64.b64encode(image.read_bytes()).decode()
        img_html = (f'<figure><img src="data:image/png;base64,{b64}" alt="Rendered result">'
                    f'<figcaption>The result every run produced (SHA-256 '
                    f'{escape((rep.get("result_sha256") or "")[:16])}…)</figcaption></figure>')

    run_rows = []
    for i, r in enumerate(bench.get("results", []), 1):
        split = ", ".join(f"{escape(w['name'])}: {w['tasks']}" for w in r.get("workers", []))
        run_rows.append(
            f"<tr class='{'warm' if r['warmup'] else ''}'><td>{i}</td>"
            f"<td><span class='dot' style='background:{MODE_COLOR[r['mode']]}'></span>"
            f"{MODE_LABEL[r['mode']]}{' (warm-up)' if r['warmup'] else ''}</td>"
            f"<td class='num'>{r['wall_s']:.2f} s</td>"
            f"<td class='num'>{_fmt_s(r.get('predicted_s'))}</td><td>{split}</td></tr>")

    mode_rows = []
    for m in ("local", "distributed"):
        if m not in modes:
            continue
        s = modes[m]
        err = s.get("prediction_error")
        mode_rows.append(
            f"<tr><td>{MODE_LABEL[m]}</td><td class='num'>{s['median_s']:.2f} s</td>"
            f"<td class='num'>{s['min_s']:.2f}–{s['max_s']:.2f} s</td>"
            f"<td class='num'>{s['runs']}</td>"
            f"<td class='num'>{_fmt_s(s.get('predicted_s'))}"
            f"{f' ({err:+.0%})' if err is not None else ''}</td></tr>")

    laptop_rows = []
    for w in workers:
        if w["state"] == "REVOKED":
            continue
        c = w.get("capabilities") or {}
        gpu = ", ".join(g["name"] for g in c.get("gpus", [])) or "—"
        net = c.get("net") or {}
        link = (f"{net['down_mbps']:.0f} Mbit/s, {net['rtt_ms']:.0f} ms" if net
                else ("controller" if w["is_local"] else "—"))
        laptop_rows.append(
            f"<tr><td>{escape(w['name'])}</td><td>{escape(c.get('cpu', '?'))}</td>"
            f"<td class='num'>{c.get('cores_physical', '?')} / {w['slots']}</td>"
            f"<td class='num'>{c.get('ram_gb', '?')} GB</td><td>{escape(gpu)}</td>"
            f"<td>{escape(link)}</td></tr>")

    identical = rep.get("identical_outputs")
    hero = (f"<div class='big'>{speedup:.2f}<span>×</span></div>"
            f"<div class='sub'>faster with the cluster · "
            f"{rep.get('time_saved_s', 0):.1f} s saved per job</div>"
            if speedup else "<div class='sub'>No speedup measured (incomplete benchmark)</div>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daneion Proof-of-Speed</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; background:#070a12; color:#e6eaf7;
  font:15px/1.55 "Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:40px 24px 60px; }}
.tag {{ display:inline-block; font-size:12px; letter-spacing:.08em; text-transform:uppercase;
  color:#fbbf24; border:1px solid #fbbf2466; border-radius:99px; padding:3px 10px; }}
h1 {{ font-size:30px; margin:14px 0 4px; letter-spacing:-.01em; }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.09em; color:#98a3c7;
  margin:36px 0 12px; }}
.muted {{ color:#98a3c7; }}
.card {{ background:#0b1020; border:1px solid #1d2642; border-radius:18px; padding:22px; }}
.hero {{ display:grid; grid-template-columns:260px 1fr; gap:22px; margin-top:26px; }}
.big {{ font-size:64px; font-weight:650; line-height:1; letter-spacing:-.02em; }}
.big span {{ color:#22d3ee; font-size:40px; }}
.sub {{ color:#c3cbe6; margin-top:8px; }}
.ok {{ color:#34d399; margin-top:16px; font-size:13px; }}
.bad {{ color:#fb7185; margin-top:16px; font-size:13px; }}
.lbl {{ fill:#c3cbe6; font-size:14px; }} .val {{ fill:#e6eaf7; font-size:14px; font-weight:600; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ text-align:left; padding:9px 10px; border-bottom:1px solid #141b31; vertical-align:top; }}
th {{ color:#98a3c7; font-weight:500; font-size:12px; text-transform:uppercase;
  letter-spacing:.06em; }}
td.num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
tr.warm td {{ color:#6b77a3; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:8px; }}
figure {{ margin:0; }} figure img {{ width:100%; border-radius:14px; display:block; }}
figcaption {{ color:#98a3c7; font-size:13px; margin-top:8px; }}
ul {{ padding-left:20px; color:#c3cbe6; }}
@media (max-width:720px) {{ .hero {{ grid-template-columns:1fr; }} }}
</style></head>
<body><main>
<span class="tag">Recorded result · {escape(when)}</span>
<h1>Daneion Proof-of-Speed</h1>
<div class="muted">Fractal render, {bench['runs']} measured runs per mode after
{bench['warmup']} warm-up run(s) each, on {len(laptop_rows)} laptop(s).</div>

<div class="hero">
  <div class="card">{hero}
    <div class="{'ok' if identical else 'bad'}">{'✓ Identical output in every run' if identical
        else '✗ Outputs differed between runs'}</div></div>
  <div class="card">{_chart(modes)}
    <div class="muted" style="font-size:12px;margin-top:6px">Bar: median ·
    line: min–max · dashed: FitCheck's prediction before the run</div></div>
</div>

<h2>Summary</h2>
<div class="card"><table><thead><tr><th>Mode</th><th>Median</th><th>Range</th><th>Runs</th>
<th>FitCheck predicted</th></tr></thead><tbody>{''.join(mode_rows)}</tbody></table></div>

<h2>Every run</h2>
<div class="card"><table><thead><tr><th>#</th><th>Mode</th><th>Time</th><th>Predicted</th>
<th>Tiles per laptop</th></tr></thead><tbody>{''.join(run_rows)}</tbody></table></div>

<h2>The cluster</h2>
<div class="card"><table><thead><tr><th>Laptop</th><th>CPU</th><th>Cores / slots</th>
<th>RAM</th><th>GPU</th><th>Link</th></tr></thead><tbody>{''.join(laptop_rows)}</tbody></table></div>

{f'<h2>Output</h2>{img_html}' if img_html else ''}

<h2>How it was measured</h2>
<ul>
<li>Same job, same code, same input in both modes; only the number of laptops changes.</li>
<li>Warm-up runs are excluded. Modes alternate (L D D L …) so heat and turbo affect both.</li>
<li>Time is wall-clock from submission to verified, merged result.</li>
<li>Every tile is checked (SHA-256 and format) before it counts; the final image's SHA-256
is compared across all runs.</li>
<li>FitCheck's prediction is made before each run and shown next to the measurement.</li>
</ul>
</main></body></html>"""
