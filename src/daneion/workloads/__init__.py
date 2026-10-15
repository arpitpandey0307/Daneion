"""Allowlisted built-in workloads. Nothing outside this registry can run (ADR-0004)."""

from __future__ import annotations

from daneion.workloads.base import Estimate, Workload, WorkloadError
from daneion.workloads.fractal import FractalTiles
from daneion.workloads.images import ImageBatch

REGISTRY: dict[str, Workload] = {w.name: w for w in (FractalTiles(), ImageBatch())}


def get_workload(name: str) -> Workload:
    try:
        return REGISTRY[name]
    except KeyError:
        raise WorkloadError(f"unknown workload: {name!r}") from None


__all__ = ["REGISTRY", "Estimate", "Workload", "WorkloadError", "get_workload"]
