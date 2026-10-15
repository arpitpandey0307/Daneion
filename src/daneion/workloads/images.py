"""image_batch_v1: make web thumbnails from a folder of photos (resize, auto-contrast, sharpen).

A real everyday job, but the opposite of the fractal: megabytes of input per image and only
milliseconds of CPU each, so over Wi-Fi the transfer costs more than the compute saves.
FitCheck should keep it local; that is the honest "do not distribute" case (ADR-0006, ADR-0013).
"""

from __future__ import annotations

import io
import math
import random
import struct
import time
from pathlib import Path
from typing import Any

from daneion.workloads.base import Estimate, InputStore, Workload, WorkloadError

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PNG_SIG = b"\x89PNG\r\n\x1a\n"
DEFAULTS: dict[str, Any] = {"source_dir": "", "max_images": 200, "thumb": 320, "batch": 8}
LIMITS = {"max_images": (1, 2000), "thumb": (32, 1024), "batch": (1, 64)}
MAX_INPUT_BYTES = 50 * 1024 * 1024  # per image


def _process(data: bytes, thumb: int) -> bytes:
    from PIL import Image, ImageFilter, ImageOps

    with Image.open(io.BytesIO(data)) as im:
        img = im.convert("RGB")
    w, h = img.size
    f = min(thumb / w, thumb / h, 1.0)
    img = img.resize((max(1, round(w * f)), max(1, round(h * f))), Image.Resampling.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=80, threshold=2))
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def _unpack(output: bytes) -> list[bytes]:
    items, pos = [], 0
    while pos < len(output):
        if pos + 4 > len(output):
            raise WorkloadError("truncated thumbnail container")
        (n,) = struct.unpack_from(">I", output, pos)
        pos += 4
        if n == 0 or pos + n > len(output):
            raise WorkloadError("bad thumbnail length")
        items.append(output[pos:pos + n])
        pos += n
    return items


class ImageBatch(Workload):
    name = "image_batch_v1"
    title = "Photo thumbnails (light CPU, heavy transfers)"
    presets = {"quick": {"max_images": 40}, "demo": {"max_images": 160}}

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        unknown = set(params) - set(DEFAULTS)
        if unknown:
            raise WorkloadError(f"unknown parameters: {sorted(unknown)}")
        p = {**DEFAULTS, **params}
        if not isinstance(p["source_dir"], str) or not p["source_dir"].strip():
            raise WorkloadError("source_dir must be a folder of images on the controller")
        for key, (lo, hi) in LIMITS.items():
            if not isinstance(p[key], int) or isinstance(p[key], bool) or not lo <= p[key] <= hi:
                raise WorkloadError(f"{key} must be an integer in [{lo}, {hi}]")
        return p

    def prepare(self, params: dict[str, Any], store: InputStore) -> dict[str, Any]:
        from PIL import Image

        src = Path(params["source_dir"]).expanduser()
        if not src.is_dir():
            raise WorkloadError(f"folder not found: {src}")
        files: list[dict[str, Any]] = []
        for path in sorted(p for p in src.iterdir() if p.suffix.lower() in IMAGE_EXTS):
            if len(files) >= params["max_images"]:
                break
            if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
                continue
            try:
                with Image.open(path) as im:
                    w, h = im.size
            except OSError:
                continue  # not a readable image: skip, do not fail the job
            sha, size = store.put_file(path)
            files.append({"name": path.name, "sha256": sha, "size": size, "w": w, "h": h})
        if not files:
            raise WorkloadError(f"no readable images in {src}")
        return {**params, "source_dir": str(src), "files": files}

    def split(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        files, b = params["files"], params["batch"]
        return [{"thumb": params["thumb"], "files": files[i:i + b]}
                for i in range(0, len(files), b)]

    def input_refs(self, task: dict[str, Any]) -> list[str]:
        return [f["sha256"] for f in task["files"]]

    def run(self, task: dict[str, Any], inputs: dict[str, Path] | None = None) -> bytes:
        if inputs is None:
            raise WorkloadError("image_batch_v1 needs its input files")
        out = bytearray()
        for f in task["files"]:
            png = _process(inputs[f["sha256"]].read_bytes(), task["thumb"])
            out += struct.pack(">I", len(png)) + png
        return bytes(out)

    def verify(self, task: dict[str, Any], output: bytes) -> None:
        items = _unpack(output)
        if len(items) != len(task["files"]):
            raise WorkloadError(f"{len(items)} thumbnails for {len(task['files'])} images")
        for png in items:
            if not png.startswith(PNG_SIG) or len(png) < 24:
                raise WorkloadError("thumbnail is not a PNG")
            w, h = struct.unpack(">II", png[16:24])
            if not (0 < w <= task["thumb"] and 0 < h <= task["thumb"]):
                raise WorkloadError(f"thumbnail {w}x{h} exceeds {task['thumb']}px")

    def merge(self, params: dict[str, Any], outputs: list[bytes]) -> tuple[bytes, str]:
        """Contact sheet of every thumbnail, in input order."""
        from PIL import Image

        thumbs = [png for out in outputs for png in _unpack(out)]
        cell = 160
        cols = max(1, math.ceil(math.sqrt(len(thumbs) * 16 / 9)))
        rows = math.ceil(len(thumbs) / cols)
        sheet = Image.new("RGB", (cols * cell, rows * cell), (14, 17, 24))
        for i, png in enumerate(thumbs):
            with Image.open(io.BytesIO(png)) as im:
                t = im.convert("RGB")
            t.thumbnail((cell - 8, cell - 8), Image.Resampling.LANCZOS)
            x = (i % cols) * cell + (cell - t.width) // 2
            y = (i // cols) * cell + (cell - t.height) // 2
            sheet.paste(t, (x, y))
        buf = io.BytesIO()
        sheet.save(buf, format="PNG")
        return buf.getvalue(), "contact_sheet.png"

    def estimate(self, params: dict[str, Any]) -> Estimate:
        files = params["files"]
        thumb = params["thumb"]
        return Estimate(
            work_units=sum(f["w"] * f["h"] for f in files) / 1e6,
            task_count=math.ceil(len(files) / params["batch"]),
            bytes_in=float(sum(f["size"] for f in files)),
            bytes_out=len(files) * thumb * thumb * 1.2,
            quality=0.9, note=f"{len(files)} images", merge_s=0.004 * len(files),
            task_units=[sum(f["w"] * f["h"] for f in t["files"]) / 1e6
                        for t in self.split(params)])

    def calibrate(self) -> float:
        sample = _synthetic_photo(random.Random(7), 1600, 1200)
        t0 = time.perf_counter()
        _process(sample, 320)
        return 1.92 / (time.perf_counter() - t0)


def _synthetic_photo(rng: random.Random, w: int, h: int) -> bytes:
    """A photo-like JPEG: smooth gradients, shapes and sensor noise (compresses like a photo)."""
    from PIL import Image, ImageDraw, ImageFilter

    c1 = tuple(rng.randrange(256) for _ in range(3))
    c2 = tuple(rng.randrange(256) for _ in range(3))
    grad = Image.linear_gradient("L").resize((w, h)).rotate(rng.randrange(360), expand=False)
    img = Image.composite(Image.new("RGB", (w, h), c1), Image.new("RGB", (w, h), c2), grad)
    draw = ImageDraw.Draw(img)
    for _ in range(12):
        x, y, r = rng.randrange(w), rng.randrange(h), rng.randrange(40, 260)
        draw.ellipse((x - r, y - r, x + r, y + r),
                     fill=tuple(rng.randrange(256) for _ in range(3)))
    img = img.filter(ImageFilter.GaussianBlur(3))
    noise = Image.effect_noise((w, h), 18).convert("RGB")
    img = Image.blend(img, noise, 0.12)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def make_samples(folder: Path, count: int, seed: int = 2026, size: tuple[int, int] = (1920, 1080)
                 ) -> list[Path]:
    """Write `count` photo-like JPEGs for demos and tests."""
    folder.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    paths = []
    for i in range(count):
        p = folder / f"photo_{i:04d}.jpg"
        if not p.exists():
            p.write_bytes(_synthetic_photo(rng, *size))
        else:
            _synthetic_photo(rng, 8, 8)  # keep the random stream aligned
        paths.append(p)
    return paths
