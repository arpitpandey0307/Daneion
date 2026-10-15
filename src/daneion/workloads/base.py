"""Contract every built-in workload implements (ADR-0004: only allowlisted handlers run)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class WorkloadError(ValueError):
    """Bad parameters or output. Never retryable."""


class Workload(ABC):
    name: str
    version: str = "1"
    title: str = ""
    presets: dict[str, dict[str, Any]] = {}

    @abstractmethod
    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return normalised job parameters or raise WorkloadError."""

    @abstractmethod
    def split(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Split validated job parameters into independent task parameters."""

    @abstractmethod
    def run(self, task: dict[str, Any]) -> bytes:
        """Execute one task. Must be deterministic: same input, same bytes."""

    @abstractmethod
    def verify(self, task: dict[str, Any], output: bytes) -> None:
        """Schema check of a task output; raise WorkloadError when malformed."""

    @abstractmethod
    def merge(self, params: dict[str, Any], outputs: list[bytes]) -> tuple[bytes, str]:
        """Combine task outputs (in task order) into (result bytes, file name)."""

    def estimate(self, params: dict[str, Any]) -> dict[str, float]:
        """Rough per-job sizes used by FitCheck: work units and bytes in/out."""
        return {"work_units": 1.0, "bytes_in": 0.0, "bytes_out": 0.0}
