"""fractal_tiles_v1: Mandelbrot render split into tiles.

Tiny input, heavy CPU per byte, bit-for-bit deterministic output (pure Python IEEE-754 doubles),
so it is the workload that can show a real distributed speedup over Wi-Fi (ADR-0013).
Each task returns the raw iteration counts of its tile as little-endian uint16.

Work is measured in "units" = (iterations + PIXEL_COST * pixels) / 1e6, which tracks CPU time
far better than pixel counts because the cost per pixel varies by four orders of magnitude.
"""

from __future__ import annotations

import io
import sys
import time
from array import array
from pathlib import Path
from typing import Any

from daneion.workloads.base import Estimate, Workload, WorkloadError

DEFAULTS: dict[str, Any] = {
    "width": 1280,
    "height": 800,
    "tile": 160,
    "max_iter": 1500,
    "center_x": -0.7436438870,
    "center_y": 0.1318259042,
    "scale": 4.0e-6,  # complex-plane units per pixel
}

# Named parameter sets shown in the console. "demo" takes ~40 s on a 16-core laptop alone.
PRESETS: dict[str, dict[str, Any]] = {
    "quick": {},
    "demo": {"width": 1920, "height": 1200, "tile": 120, "max_iter": 9000,
             "center_x": -0.7453, "center_y": 0.1127, "scale": 5.0e-6},
}

MAX_PIXELS = 4096 * 4096  # the merged RGB image is held in memory on the controller

LIMITS = {
    "width": (64, 8192),
    "height": (64, 8192),
    "tile": (32, 1024),
    "max_iter": (16, 65535),
}

PIXEL_COST = 12  # per-pixel loop overhead, expressed in equivalent iterations
SAMPLE_STEP = 24  # estimate() renders every 24th pixel in each direction (~1/576 of the work)
CALIBRATION = {"width": 640, "height": 400, "x0": 300, "y0": 180, "tw": 40, "th": 40,
               "center_x": -0.7453, "center_y": 0.1127, "scale": 5.0e-6, "max_iter": 2000}


def _palette() -> list[tuple[int, int, int]]:
    # Smooth deep-blue -> cyan -> amber -> white ramp, 256 entries.
    stops = [(0, (8, 10, 40)), (80, (20, 110, 200)), (150, (120, 230, 255)),
             (200, (255, 190, 60)), (255, (255, 250, 235))]
    out: list[tuple[int, int, int]] = []
    for (a, ca), (b, cb) in zip(stops, stops[1:], strict=False):
        for i in range(a, b):
            t = (i - a) / (b - a)
            red, green, blue = (round(ca[k] + (cb[k] - ca[k]) * t) for k in range(3))
            out.append((red, green, blue))
    out.append(stops[-1][1])
    return out


def _render(task: dict[str, Any], step: int = 1) -> array:
    """Iteration counts for every `step`-th pixel of the task's tile, row-major."""
    W, H = task["width"], task["height"]
    cx, cy = task["center_x"], task["center_y"]
    scale, max_iter = task["scale"], task["max_iter"]
    out = array("H")
    half_w, half_h = W / 2, H / 2
    for py in range(task["y0"], task["y0"] + task["th"], step):
        ci = cy + (py - half_h) * scale
        for px in range(task["x0"], task["x0"] + task["tw"], step):
            cr = cx + (px - half_w) * scale
            zr = zi = 0.0
            n = 0
            while n < max_iter:
                zr2 = zr * zr
                zi2 = zi * zi
                if zr2 + zi2 > 4.0:
                    break
                zi = 2.0 * zr * zi + ci
                zr = zr2 - zi2 + cr
                n += 1
            out.append(n)
    return out


def _units(counts: array) -> float:
    return (sum(counts) + PIXEL_COST * len(counts)) / 1e6


class FractalTiles(Workload):
    name = "fractal_tiles_v1"
    title = "Fractal render (CPU-heavy, tiny transfers)"
    presets = PRESETS

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        unknown = set(params) - set(DEFAULTS)
        if unknown:
            raise WorkloadError(f"unknown parameters: {sorted(unknown)}")
        p = {**DEFAULTS, **params}
        for key, (lo, hi) in LIMITS.items():
            if not isinstance(p[key], int) or isinstance(p[key], bool) or not lo <= p[key] <= hi:
                raise WorkloadError(f"{key} must be an integer in [{lo}, {hi}]")
        for key in ("center_x", "center_y", "scale"):
            if not isinstance(p[key], (int, float)) or isinstance(p[key], bool):
                raise WorkloadError(f"{key} must be a number")
            p[key] = float(p[key])
        if not p["scale"] > 0:
            raise WorkloadError("scale must be > 0")
        if p["width"] * p["height"] > MAX_PIXELS:
            raise WorkloadError(f"image too large: at most {MAX_PIXELS:,} pixels")
        return p

    def split(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        w, h, t = params["width"], params["height"], params["tile"]
        tasks = []
        for y0 in range(0, h, t):
            for x0 in range(0, w, t):
                tasks.append({
                    **params,
                    "x0": x0, "y0": y0,
                    "tw": min(t, w - x0), "th": min(t, h - y0),
                })
        return tasks

    def run(self, task: dict[str, Any], inputs: dict[str, Path] | None = None) -> bytes:
        out = _render(task)
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()

    def verify(self, task: dict[str, Any], output: bytes) -> None:
        expected = task["tw"] * task["th"] * 2
        if len(output) != expected:
            raise WorkloadError(f"tile output is {len(output)} bytes, expected {expected}")
        counts = array("H", output)
        if sys.byteorder != "little":
            counts.byteswap()
        if max(counts) > task["max_iter"]:
            raise WorkloadError("iteration count above max_iter")

    def merge(self, params: dict[str, Any], outputs: list[bytes]) -> tuple[bytes, str]:
        from PIL import Image

        W, H, max_iter = params["width"], params["height"], params["max_iter"]
        pal = _palette()
        lut = [bytes(c) for c in pal]
        black = b"\x00\x00\x00"
        img = Image.new("RGB", (W, H))
        for task, data in zip(self.split(params), outputs, strict=True):
            counts = array("H", data)
            if sys.byteorder != "little":
                counts.byteswap()
            rgb = b"".join(black if n >= max_iter else lut[(n * 3) % 256] for n in counts)
            tile = Image.frombytes("RGB", (task["tw"], task["th"]), rgb)
            img.paste(tile, (task["x0"], task["y0"]))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue(), "fractal.png"

    def estimate(self, params: dict[str, Any]) -> Estimate:
        """Render a sparse grid of every tile (~0.2% of the work) and scale it up."""
        tasks = self.split(params)
        per_task, sampled = [], 0
        for t in tasks:
            sample = _render(t, SAMPLE_STEP)
            sampled += len(sample)
            per_task.append(_units(sample) * t["tw"] * t["th"] / max(1, len(sample)))
        pixels = params["width"] * params["height"]
        units = sum(per_task)
        return Estimate(
            work_units=units, task_count=len(tasks), task_units=per_task,
            bytes_in=600.0 * len(tasks),  # the lease JSON per tile
            bytes_out=pixels * 2.0,
            # Sparse sampling misses thin filaments; well within FitCheck's 15 % margin.
            quality=0.85, merge_s=0.15 * pixels / 1e6,
            note=f"sampled {sampled:,} of {pixels:,} pixels")

    def calibrate(self) -> float:
        t0 = time.perf_counter()
        counts = _render(CALIBRATION)
        return _units(counts) / (time.perf_counter() - t0)
