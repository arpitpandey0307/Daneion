"""Allowlisted built-in workloads. Nothing outside this registry can run (ADR-0004)."""

from __future__ import annotations

from daneion.workloads.base import Workload, WorkloadError
from daneion.workloads.fractal import FractalTiles

REGISTRY: dict[str, Workload] = {w.name: w for w in (FractalTiles(),)}


def get_workload(name: str) -> Workload:
    try:
        return REGISTRY[name]
    except KeyError:
        raise WorkloadError(f"unknown workload: {name!r}") from None


__all__ = ["REGISTRY", "Workload", "WorkloadError", "get_workload"]
