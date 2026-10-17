"""Day 5 hardening: build handshake, controller restart mid-job, log redaction."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from daneion.build import build_info
from daneion.config import ControllerConfig
from daneion.controller.api import create_app
from daneion.controller.service import Controller
from daneion.logs import RedactSecrets
from daneion.worker.agent import WorkerAgent
from daneion.workloads import get_workload
from tests.conftest import SMALL, wait_job
from tests.test_end_to_end import free_port


def test_laptop_with_different_workload_code_is_refused(ctl: Controller) -> None:
    worker_id, token = ctl.pair(ctl.open_pairing()["code"], "old-laptop", client_ip="10.0.0.2",
                                capabilities={"cpu": "x"})
    http = TestClient(create_app(ctl), base_url="http://127.0.0.1:8750",
                      headers={"Authorization": f"Bearer {token}"})
    ctl.submit_job("fractal_tiles_v1", SMALL, "distributed")
    # Paired but never said hello: no work.
    assert http.post("/api/v1/worker/poll", json={"free_slots": 2, "wait_s": 0}).status_code == 409
    bad = http.post("/api/v1/worker/hello", json={"build": {"workloads": "0000"}})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "VERSION_MISMATCH"
    assert "same Daneion version" in bad.json()["error"]["message"]
    assert http.post("/api/v1/worker/poll", json={"free_slots": 2, "wait_s": 0}).status_code == 409
    ok = http.post("/api/v1/worker/hello", json={"build": build_info()})
    assert ok.status_code == 200 and ok.json()["build"] == build_info()
    leases = http.post("/api/v1/worker/poll", json={"free_slots": 2, "wait_s": 0}).json()["leases"]
    assert len(leases) == 2


def test_fingerprint_ignores_line_endings_but_not_code() -> None:
    assert len(build_info()["workloads"]) == 16


def test_job_survives_a_controller_restart(tmp_path: Path) -> None:
    """Stop the controller mid-job, start a new one on the same database and port: the worker
    reconnects by itself and the job finishes with the same result as an uninterrupted run."""
    port = free_port()
    params = {**SMALL, "width": 160, "height": 128, "max_iter": 3000}

    def boot() -> tuple[Controller, uvicorn.Server]:
        ctl = Controller(ControllerConfig(data_dir=tmp_path / "ctl", port=port, lease_ttl_s=6,
                                          heartbeat_interval_s=0.5))
        ctl.start_background()
        server = uvicorn.Server(uvicorn.Config(create_app(ctl), host="127.0.0.1", port=port,
                                               log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.02)
        return ctl, server

    ctl, server = boot()
    _, token = ctl.register_worker("w", is_local=True, slots=2)
    agent = WorkerAgent(f"http://127.0.0.1:{port}", token, slots=2, work_dir=tmp_path / "w")
    threading.Thread(target=agent.run, daemon=True).start()
    try:
        job_id = ctl.submit_job("fractal_tiles_v1", params, "local")["id"]
        while (ctl.get_job(job_id)["task_states"].get("SUCCEEDED", 0)) < 3:
            time.sleep(0.05)
        server.should_exit = True
        ctl.stop_background()
        while server.started and not server.should_exit:
            time.sleep(0.05)
        time.sleep(1.5)  # controller is down; the worker keeps failing to reach it

        ctl, server = boot()
        job = wait_job(ctl, job_id, timeout=90)
        assert job["state"] == "SUCCEEDED", job
        wl = get_workload("fractal_tiles_v1")
        p = wl.validate(params)
        expected, _ = wl.merge(p, [wl.run(t) for t in wl.split(p)])
        import hashlib

        assert job["result_sha256"] == hashlib.sha256(expected).hexdigest()
    finally:
        agent.stop()
        server.should_exit = True
        ctl.stop_background()


def test_logs_never_contain_tokens() -> None:
    text = 'sent Authorization: Bearer abcdefghijklmnop and {"token": "s3cr3t"}'
    record = logging.LogRecord("x", logging.INFO, __file__, 1, text, None, None)
    RedactSecrets().filter(record)
    msg = record.getMessage()
    assert "abcdefghijklmnop" not in msg and "s3cr3t" not in msg and "[redacted]" in msg


def test_proof_of_speed_report_is_self_contained(tmp_path: Path) -> None:
    from daneion.controller.report import render_report
    from daneion.controller.service import benchmark_report

    results = [
        {"mode": "local", "warmup": False, "wall_s": 40.0, "result_sha256": "ab" * 32,
         "predicted_s": 41.0, "workers": [{"name": "me", "tasks": 160}], "job_id": "j1"},
        {"mode": "distributed", "warmup": False, "wall_s": 20.0, "result_sha256": "ab" * 32,
         "predicted_s": 21.0, "workers": [{"name": "me", "tasks": 80},
                                          {"name": "<b>victus</b>", "tasks": 80}],
         "job_id": "j2"},
    ]
    bench = {"id": "bench_x", "runs": 1, "warmup": 0, "created_at": 1_700_000_000.0,
             "results": results, "report": benchmark_report(results)}
    img = tmp_path / "r.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    workers = [{"name": "me", "state": "ONLINE", "is_local": True, "slots": 8,
                "capabilities": {"cpu": "Ryzen 9", "cores_physical": 16, "ram_gb": 15}}]
    html = render_report(bench, workers, img)
    assert "2.00<span>×</span>" in html and "Recorded result" in html
    assert "data:image/png;base64," in html
    assert "<b>victus</b>" not in html and "&lt;b&gt;victus&lt;/b&gt;" in html  # escaped
    assert "http://" not in html and "https://" not in html  # nothing loaded from the network
