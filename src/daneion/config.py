"""Runtime defaults (ADR-0017, ADR-0019). Every value can be overridden per run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    return Path.home() / ".daneion"


@dataclass
class ControllerConfig:
    data_dir: Path = field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8750
    max_nodes: int = 5  # ADR-0019: includes the controller's own local worker
    heartbeat_interval_s: float = 3.0
    suspect_after_missed: int = 3
    lease_ttl_s: float = 30.0
    long_poll_s: float = 20.0
    max_attempts: int = 3
    retry_base_s: float = 1.0
    retry_cap_s: float = 15.0
    sweep_interval_s: float = 1.0
    max_result_bytes: int = 64 * 1024 * 1024
    local_worker: bool = True
    local_worker_slots: int | None = None  # None = physical cores

    @property
    def db_path(self) -> Path:
        return self.data_dir / "controller.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"
