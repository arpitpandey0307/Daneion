"""Two-listener controller over real TLS; a separate worker pairs with `daneion join` logic and
runs a distributed job with the controller's own worker switched off."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from daneion import cli
from daneion.config import ControllerConfig
from daneion.controller.api import create_app
from daneion.controller.service import Controller
from daneion.tls import FINGERPRINT_CHARS, ensure_controller_cert
from tests.conftest import SMALL
from tests.test_end_to_end import free_port


def _wait_up(url: str, **kw) -> None:  # type: ignore[no-untyped-def]
    for _ in range(200):
        try:
            httpx.get(url, timeout=1, **kw)
            return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise AssertionError(f"{url} never came up")


def test_worker_pairs_over_pinned_tls_and_runs_job(tmp_path: Path) -> None:
    cport, lport = free_port(), free_port()
    cfg = ControllerConfig(data_dir=tmp_path / "ctl", port=cport, lan_host="127.0.0.1",
                           lan_port=lport, local_worker=False)
    ctl = Controller(cfg)
    ctl.start_background()
    cert, key, fp = ensure_controller_cert(cfg.tls_dir)
    lan_info = {"fingerprint": fp[:FINGERPRINT_CHARS], "port": lport, "addresses": ["127.0.0.1"]}
    lan = uvicorn.Server(uvicorn.Config(create_app(ctl, role="lan"), host="127.0.0.1",
                                        port=lport, ssl_certfile=str(cert),
                                        ssl_keyfile=str(key), log_level="warning"))
    con = uvicorn.Server(uvicorn.Config(create_app(ctl, lan_info=lan_info), host="127.0.0.1",
                                        port=cport, log_level="warning"))
    threading.Thread(target=lan.run, daemon=True).start()
    threading.Thread(target=con.run, daemon=True).start()
    console = f"http://127.0.0.1:{cport}"
    _wait_up(console + "/api/v1/health")
    _wait_up(f"https://127.0.0.1:{lport}/api/v1/health", verify=False)
    wdir = tmp_path / "w"
    try:
        # Plain HTTP to the TLS port must not work.
        try:
            r = httpx.get(f"http://127.0.0.1:{lport}/api/v1/health", timeout=2)
            assert r.status_code >= 400
        except httpx.HTTPError:
            pass

        info = httpx.post(console + "/api/v1/pairing").json()
        # A wrong fingerprint is refused before any secret is sent.
        bad = cli.main(["join", "127.0.0.1", info["code"], "--fp", "00" * 8, "--port",
                        str(lport), "--data-dir", str(wdir), "--no-run"])
        assert bad == 3
        ok = cli.main(["join", "127.0.0.1", info["code"], "--fp", info["fingerprint"],
                       "--port", str(lport), "--data-dir", str(wdir), "--name", "victus",
                       "--slots", "2", "--no-run"])
        assert ok == 0
        creds = json.loads((wdir / "worker" / "credentials.json").read_text())
        assert creds["url"] == f"https://127.0.0.1:{lport}"

        t = threading.Thread(target=cli.main, args=(["worker", "--data-dir", str(wdir)],),
                             daemon=True)
        t.start()
        job = httpx.post(console + "/api/v1/jobs", json={
            "handler": "fractal_tiles_v1", "params": SMALL, "mode": "distributed"}).json()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            job = httpx.get(f"{console}/api/v1/jobs/{job['id']}").json()
            if job["state"] in ("SUCCEEDED", "FAILED"):
                break
            time.sleep(0.1)
        assert job["state"] == "SUCCEEDED", job
        assert [w["name"] for w in job["per_worker"]] == ["victus"]
        workers = httpx.get(console + "/api/v1/workers").json()
        assert workers[0]["capabilities"]["cores_logical"] >= 1
    finally:
        for w in ctl.list_workers():
            ctl.set_worker_state(w["id"], "REVOKED")  # type: ignore[arg-type]
        lan.should_exit = con.should_exit = True
        ctl.stop_background()
