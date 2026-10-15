"""Real HTTP server + real worker agent + real task subprocesses."""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from daneion.config import ControllerConfig
from daneion.controller.api import create_app
from daneion.controller.service import Controller
from daneion.worker.agent import WorkerAgent
from daneion.workloads import get_workload
from tests.conftest import SMALL


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_job_runs_through_http_and_child_processes(tmp_path: Path) -> None:
    port = free_port()
    cfg = ControllerConfig(data_dir=tmp_path / "ctl", port=port)
    ctl = Controller(cfg)
    ctl.start_background()
    server = uvicorn.Server(uvicorn.Config(create_app(ctl), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    _, token = ctl.register_worker("e2e", is_local=True, slots=3)
    agent = WorkerAgent(base, token, slots=3, work_dir=tmp_path / "wrk")
    threading.Thread(target=agent.run, daemon=True).start()
    try:
        with httpx.Client(base_url=base, timeout=10) as http:
            for _ in range(100):
                try:
                    http.get("/api/v1/health")
                    break
                except httpx.ConnectError:
                    time.sleep(0.05)
            assert http.post("/api/v1/worker/poll", json={"free_slots": 1}).status_code == 401
            job = http.post("/api/v1/jobs", json={"handler": "fractal_tiles_v1",
                                                  "params": SMALL, "mode": "local"}).json()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                job = http.get(f"/api/v1/jobs/{job['id']}").json()
                if job["state"] in ("SUCCEEDED", "FAILED"):
                    break
                time.sleep(0.1)
            assert job["state"] == "SUCCEEDED", job
            png = http.get(f"/api/v1/jobs/{job['id']}/result").content
            wl = get_workload("fractal_tiles_v1")
            p = wl.validate(SMALL)
            expected, _ = wl.merge(p, [wl.run(t) for t in wl.split(p)])
            assert hashlib.sha256(png).hexdigest() == hashlib.sha256(expected).hexdigest()
            kinds = {e["kind"] for e in http.get("/api/v1/events",
                                                  params={"job_id": job["id"]}).json()}
            assert {"job.submitted", "job.fitcheck", "task.leased", "task.running",
                    "task.succeeded", "job.succeeded"} <= kinds
    finally:
        agent.stop()
        server.should_exit = True
        ctl.stop_background()
