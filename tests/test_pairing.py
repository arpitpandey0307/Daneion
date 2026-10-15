from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from daneion.controller import errors
from daneion.controller.api import create_app
from daneion.controller.service import Controller
from tests.conftest import FakeClock


def test_code_is_single_use(ctl: Controller) -> None:
    code = ctl.open_pairing()["code"]
    ctl.pair(code, "laptop-a", client_ip="10.0.0.2")
    with pytest.raises(errors.PairingFailed):
        ctl.pair(code, "laptop-b", client_ip="10.0.0.3")


def test_code_expires(ctl: Controller, clock: FakeClock) -> None:
    code = ctl.open_pairing()["code"]
    clock.advance(ctl.cfg.pairing_ttl_s + 1)
    with pytest.raises(errors.PairingFailed, match="expired"):
        ctl.pair(code, "late", client_ip="10.0.0.2")


def test_code_is_case_and_dash_insensitive(ctl: Controller) -> None:
    code = ctl.open_pairing()["code"]
    worker_id, token = ctl.pair(code.lower().replace("-", " "), "x", client_ip="10.0.0.2")
    assert ctl.authenticate(token).id == worker_id


def test_wrong_codes_are_rate_limited_per_ip(ctl: Controller) -> None:
    code = ctl.open_pairing()["code"]
    for _ in range(ctl.cfg.pair_ip_failures_per_min):
        with pytest.raises(errors.PairingFailed):
            ctl.pair("AAAA-AAAA", "x", client_ip="10.0.0.9")
    with pytest.raises(errors.RateLimited):
        ctl.pair(code, "x", client_ip="10.0.0.9")
    ctl.pair(code, "y", client_ip="10.0.0.10")  # other laptops are unaffected


def test_code_is_burned_after_too_many_failures(ctl: Controller) -> None:
    code = ctl.open_pairing()["code"]
    for i in range(ctl.cfg.pairing_max_failures):
        with pytest.raises(errors.PairingFailed):
            ctl.pair("AAAA-AAAA", "x", client_ip=f"10.0.1.{i}")
    with pytest.raises(errors.PairingFailed):
        ctl.pair(code, "x", client_ip="10.0.2.1")


def test_re_pairing_same_name_replaces_old_identity(ctl: Controller) -> None:
    _, old = ctl.pair(ctl.open_pairing()["code"], "victus", client_ip="10.0.0.2")
    _, new = ctl.pair(ctl.open_pairing()["code"], "victus", client_ip="10.0.0.2")
    with pytest.raises(errors.Unauthorized):
        ctl.authenticate(old)
    ctl.authenticate(new)
    live = [w for w in ctl.list_workers() if w["state"] != "REVOKED"]
    assert len(live) == 1


def test_lan_listener_exposes_only_worker_routes(ctl: Controller) -> None:
    lan = TestClient(create_app(ctl, role="lan"), base_url="https://192.168.1.5")
    assert lan.get("/api/v1/health").status_code == 200
    assert lan.get("/api/v1/jobs").status_code == 404
    assert lan.post("/api/v1/pairing").status_code == 404
    assert lan.get("/api/docs").status_code == 404
    assert lan.post("/api/v1/worker/pair", json={"code": "AAAA-AAAA"}).status_code == 401


def test_console_rejects_foreign_host_and_origin(ctl: Controller) -> None:
    app = create_app(ctl)
    ok = TestClient(app, base_url="http://127.0.0.1:8750")
    assert ok.get("/api/v1/jobs").status_code == 200
    rebound = TestClient(app, base_url="http://evil.example:8750")
    assert rebound.get("/api/v1/jobs").status_code == 403
    cross = ok.post("/api/v1/jobs", json={"handler": "fractal_tiles_v1"},
                    headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403


def test_pairing_endpoint_returns_join_command(ctl: Controller) -> None:
    app = create_app(ctl, lan_info={"fingerprint": "ab" * 8, "port": 8751,
                                    "addresses": ["192.168.43.10"]})
    info = TestClient(app, base_url="http://127.0.0.1:8750").post("/api/v1/pairing").json()
    assert info["command"] == f"daneion join 192.168.43.10 {info['code']} --fp {'ab' * 8}"
