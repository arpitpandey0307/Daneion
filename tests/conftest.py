from __future__ import annotations

import socket
from contextlib import contextmanager
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


@contextmanager
def live_cluster(data_dir: Path, slots: int = 2, **cfg_kw: object):  # type: ignore[no-untyped-def]
    """Real HTTP controller + one local worker agent running real task processes."""
    import threading
    import time

    import uvicorn

    from daneion.controller.api import create_app
    from daneion.worker.agent import WorkerAgent

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    ctl = Controller(ControllerConfig(data_dir=data_dir, port=port, **cfg_kw))  # type: ignore[arg-type]
    ctl.start_background()
    server = uvicorn.Server(uvicorn.Config(create_app(ctl), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    _, token = ctl.register_worker("local", is_local=True, slots=slots)
    agent = WorkerAgent(f"http://127.0.0.1:{port}", token, slots=slots,
                        work_dir=data_dir / "worker")
    threading.Thread(target=agent.run, daemon=True).start()
    try:
        yield ctl, f"http://127.0.0.1:{port}"
    finally:
        agent.stop()
        server.should_exit = True
        ctl.stop_background()


def wait_job(ctl: Controller, job_id: str, timeout: float = 60.0) -> dict:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = ctl.get_job(job_id)
        if job["state"] in ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still {ctl.get_job(job_id)['state']}")
