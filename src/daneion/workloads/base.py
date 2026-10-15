"""Contract every built-in workload implements (ADR-0004: only allowlisted handlers run)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


class WorkloadError(ValueError):
    """Bad parameters or output. Never retryable."""


@dataclass
class Estimate:
    """What FitCheck needs to know about a job before running it."""

    work_units: float  # comparable with calibrate(): units / (units per second) = seconds
    task_count: int
    bytes_in: float  # bytes every task needs from the controller, summed over all tasks
    bytes_out: float  # result bytes sent back, summed
    quality: float  # 0..1, how much to trust work_units
    note: str = ""
    task_units: list[float] | None = None  # per task, same order as split()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class InputStore(Protocol):
    """Content-addressed store on the controller (ART-01/ART-10)."""

    def put_file(self, path: Path) -> tuple[str, int]:
        """Copy a file in; return (sha256, size)."""
        ...


class Workload(ABC):
    name: str
    version: str = "1"
    title: str = ""
    presets: dict[str, dict[str, Any]] = {}

    @abstractmethod
    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return normalised job parameters or raise WorkloadError."""

    def prepare(self, params: dict[str, Any], store: InputStore) -> dict[str, Any]:
        """Controller-side ingestion of input files (default: none)."""
        return params

    @abstractmethod
    def split(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Split validated job parameters into independent task parameters."""

    def input_refs(self, task: dict[str, Any]) -> list[str]:
        """SHA-256 of every stored input this task reads (the worker downloads these)."""
        return []

    @abstractmethod
    def run(self, task: dict[str, Any], inputs: dict[str, Path] | None = None) -> bytes:
        """Execute one task. Must be deterministic: same input, same bytes.
        `inputs` maps each sha256 from input_refs() to a verified local file."""

    @abstractmethod
    def verify(self, task: dict[str, Any], output: bytes) -> None:
        """Schema check of a task output; raise WorkloadError when malformed."""

    @abstractmethod
    def merge(self, params: dict[str, Any], outputs: list[bytes]) -> tuple[bytes, str]:
        """Combine task outputs (in task order) into (result bytes, file name)."""

    @abstractmethod
    def estimate(self, params: dict[str, Any]) -> Estimate:
        """Predict the job's size for FitCheck (called on the controller after prepare)."""

    @abstractmethod
    def calibrate(self) -> float:
        """Single-core speed of this machine for this workload, in work units per second.
        Must finish in well under a second."""
