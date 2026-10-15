"""Build identity. Two laptops may only work together when they run the same workload code,
because a different handler could produce different bytes for the same tile."""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from daneion import __version__


@cache
def workload_fingerprint() -> str:
    """SHA-256 over the source of every built-in workload (short form)."""
    h = hashlib.sha256()
    root = Path(__file__).parent / "workloads"
    for path in sorted(root.glob("*.py")):
        h.update(path.name.encode())
        # Normalise line endings so a CRLF checkout matches an LF one.
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def build_info() -> dict[str, str]:
    return {"version": __version__, "workloads": workload_fingerprint()}
