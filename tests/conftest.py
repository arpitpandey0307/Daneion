from __future__ import annotations

from pathlib import Path

import pytest

from daneion.config import ControllerConfig
from daneion.controller.db import Database
from daneion.controller.service import Controller

SMALL = {"width": 96, "height": 64, "tile": 32, "max_iter": 64}


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cfg(tmp_path: Path) -> ControllerConfig:
    return ControllerConfig(data_dir=tmp_path / "data", retry_base_s=0.0, retry_cap_s=0.0)


@pytest.fixture
def ctl(cfg: ControllerConfig, clock: FakeClock) -> Controller:
    return Controller(cfg, db=Database(":memory:"), clock=clock)
