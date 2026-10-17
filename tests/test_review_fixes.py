"""Regression tests for issues found in the Day 1-2 review."""

from __future__ import annotations

import socket
import ssl
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from daneion import cli
from daneion.config import ControllerConfig
from daneion.controller.api import create_app
from daneion.controller.service import Controller
from daneion.tls import ensure_controller_cert, pinned_context
from tests.conftest import SMALL
from tests.test_end_to_end import free_port


def _serve(app, port: int, **kw) -> uvicorn.Server:  # type: ignore[no-untyped-def]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", **kw))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise AssertionError("server did not start")


def _first_event_id(url: str, headers: dict[str, str] | None = None) -> int:
    with httpx.stream("GET", url, headers=headers or {}, timeout=10) as r:
        for line in r.iter_lines():
            if line.startswith("id: "):
                return int(line[4:])
    raise AssertionError("stream ended")


def test_event_stream_resumes_from_last_event_id_and_defaults_to_live_tail(
        tmp_path: Path) -> None:
    ctl = Controller(ControllerConfig(data_dir=tmp_path))
    port = free_port()
    server = _serve(create_app(ctl), port)
    base = f"http://127.0.0.1:{port}/api/v1"
    try:
        for _ in range(3):
            ctl.submit_job("fractal_tiles_v1", SMALL, "local")
        last = ctl.last_event_id()
        assert last > 5
        # Browser reconnect: continue right after the last event it saw.
        assert _first_event_id(base + "/events/stream?after=0",
                               {"Last-Event-ID": "4"}) == 5
        # Explicit cursor.
        assert _first_event_id(base + "/events/stream?after=2") == 3
        # No cursor: only new events, not the whole history.
        threading.Timer(0.5, lambda: ctl.submit_job("fractal_tiles_v1", SMALL, "local")).start()
        assert _first_event_id(base + "/events/stream") == last + 1
    finally:
        server.should_exit = True


def test_pinned_tls_works_under_strict_x509(tmp_path: Path) -> None:
    cert, key, _ = ensure_controller_cert(tmp_path)
    port = free_port()
    ctl = Controller(ControllerConfig(data_dir=tmp_path / "d"))
    server = _serve(create_app(ctl, role="lan"), port, ssl_certfile=str(cert),
                    ssl_keyfile=str(key))
    try:
        strict = ssl.create_default_context(cafile=str(cert))
        strict.check_hostname = False
        strict.verify_flags |= ssl.VERIFY_X509_STRICT  # what Python 3.13 enables by default
        assert httpx.get(f"https://127.0.0.1:{port}/api/v1/health",
                         verify=strict).status_code == 200
        assert httpx.get(f"https://127.0.0.1:{port}/api/v1/health",
                         verify=pinned_context(cert)).status_code == 200
        other, _, _ = ensure_controller_cert(tmp_path / "other")
        try:
            httpx.get(f"https://127.0.0.1:{port}/api/v1/health", verify=pinned_context(other))
            raise AssertionError("a different certificate must not be trusted")
        except httpx.ConnectError:
            pass
    finally:
        server.should_exit = True


def test_controller_refuses_to_start_when_worker_port_is_taken(tmp_path: Path) -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    try:
        code = cli.main(["controller", "--data-dir", str(tmp_path), "--port", str(free_port()),
                         "--lan-host", "127.0.0.1", "--lan-port", str(taken),
                         "--no-local-worker"])
        assert code == 2
    finally:
        blocker.close()


def test_revoked_worker_agent_stops_promptly(tmp_path: Path) -> None:
    from daneion.worker.agent import WorkerAgent

    ctl = Controller(ControllerConfig(data_dir=tmp_path, heartbeat_interval_s=0.5))
    ctl.start_background()
    port = free_port()
    server = _serve(create_app(ctl), port)
    worker_id, token = ctl.register_worker("doomed", slots=1)
    agent = WorkerAgent(f"http://127.0.0.1:{port}", token, slots=1, work_dir=tmp_path / "w")
    t = threading.Thread(target=agent.run, daemon=True)
    t.start()
    try:
        time.sleep(1.0)  # connected and waiting in a long-poll
        ctl.set_worker_state(worker_id, "REVOKED")  # type: ignore[arg-type]
        t.join(timeout=5)
        assert not t.is_alive(), "revoked worker kept running"
    finally:
        agent.stop()
        server.should_exit = True
        ctl.stop_background()


def test_cli_explains_when_the_controller_is_not_running(capsys) -> None:  # type: ignore[no-untyped-def]
    url = f"http://127.0.0.1:{free_port()}"
    assert cli.main(["workers", "--controller", url]) == 2
    assert "Cannot reach the Daneion controller" in capsys.readouterr().err


def test_chunked_result_upload_is_capped(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    ctl = Controller(ControllerConfig(data_dir=tmp_path, max_result_bytes=1000))
    _, token = ctl.register_worker("w", slots=1)
    client = TestClient(create_app(ctl, role="lan"))

    def body():  # type: ignore[no-untyped-def]  # a generator body is sent without content-length
        for _ in range(5):
            yield b"x" * 500

    r = client.put("/api/v1/worker/attempts/att_x/result", content=body(),
                   headers={"Authorization": f"Bearer {token}", "X-Daneion-Fence": "1",
                            "X-Daneion-Sha256": "0" * 64})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESULT_TOO_LARGE"
