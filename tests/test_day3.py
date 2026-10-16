"""Day 3: task guard, photo workload with input transfer, speed learning, benchmarks,
migrations and biggest-first leasing."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from daneion.config import ControllerConfig
from daneion.controller.db import Database
from daneion.controller.service import Controller, benchmark_report
from daneion.worker.sandbox import TaskGuard
from daneion.workloads import get_workload
from daneion.workloads.images import make_samples
from tests.conftest import SMALL, live_cluster, wait_job

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows")


def _run_guarded(code: str, memory_mb: int = 2048) -> subprocess.CompletedProcess[bytes]:
    guard = TaskGuard(memory_mb=memory_mb)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **guard.popen_kwargs())
    guard.attach(proc)
    try:
        out, err = proc.communicate(timeout=60)
    finally:
        guard.close()
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


@windows_only
def test_memory_cap_stops_a_greedy_task() -> None:
    r = _run_guarded("x = bytearray(600 * 1024 * 1024); print('allocated')", memory_mb=200)
    assert r.returncode != 0 and b"allocated" not in r.stdout


@windows_only
def test_task_cannot_start_other_programs() -> None:
    r = _run_guarded("import subprocess; subprocess.run(['cmd', '/c', 'echo', 'pwned'])")
    assert r.returncode != 0 and b"pwned" not in r.stdout


@windows_only
def test_guard_kill_stops_task_and_closing_reaps_it() -> None:
    guard = TaskGuard()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            **guard.popen_kwargs())
    guard.attach(proc)
    t0 = time.monotonic()
    guard.kill()
    proc.wait(timeout=10)
    guard.close()
    assert time.monotonic() - t0 < 5


def test_normal_task_runs_under_guard() -> None:
    r = _run_guarded("print('ok')")
    assert r.returncode == 0 and r.stdout.strip() == b"ok"


# ------------------------------------------------------------------ photos end to end
@pytest.fixture(scope="module")
def photos(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("photos")
    make_samples(folder, 10, size=(400, 300))
    (folder / "notes.txt").write_text("not an image")
    (folder / "broken.jpg").write_bytes(b"not really a jpeg")
    return folder


def test_photo_job_moves_inputs_and_is_deterministic(tmp_path: Path, photos: Path) -> None:
    params = {"source_dir": str(photos), "batch": 3, "thumb": 128}
    with live_cluster(tmp_path / "c") as (ctl, _):
        first = wait_job(ctl, ctl.submit_job("image_batch_v1", params, "local")["id"])
        assert first["state"] == "SUCCEEDED", first
        assert first["task_count"] == 4  # 10 good photos in batches of 3; broken files skipped
        second = wait_job(ctl, ctl.submit_job("image_batch_v1", params, "local")["id"])
        assert second["result_sha256"] == first["result_sha256"]
        png = ctl.job_result(first["id"]).read_bytes()
        assert png.startswith(b"\x89PNG")
        # Inputs were downloaded once and then served from the worker's verified cache.
        cache = list((tmp_path / "c" / "worker" / "cache").iterdir())
        assert len(cache) == 10
        assert all(hashlib.sha256(p.read_bytes()).hexdigest() == p.name for p in cache)


def test_inputs_are_only_served_to_the_current_attempt(tmp_path: Path, photos: Path) -> None:
    ctl = Controller(ControllerConfig(data_dir=tmp_path))
    _, token = ctl.register_worker("w", slots=1)
    w = ctl.authenticate(token)
    ctl.submit_job("image_batch_v1", {"source_dir": str(photos), "batch": 3}, "distributed")
    [lease] = ctl.poll(w, 1, wait_s=0)
    mine = lease["params"]["files"][0]["sha256"]
    assert ctl.input_path(w, lease["attempt_id"], lease["fence"], mine).exists()
    other = get_workload("image_batch_v1").validate({"source_dir": str(photos)})
    with pytest.raises(Exception, match="not part of this task"):
        not_mine = ctl.inputs.put_file(photos / "photo_0009.jpg")[0]
        ctl.input_path(w, lease["attempt_id"], lease["fence"], not_mine)
    with pytest.raises(Exception, match="not current"):
        ctl.input_path(w, lease["attempt_id"], lease["fence"] + 1, mine)
    assert other  # validate does not touch the store


def test_photo_job_with_missing_folder_is_rejected(tmp_path: Path) -> None:
    ctl = Controller(ControllerConfig(data_dir=tmp_path))
    job = ctl.submit_job("image_batch_v1", {"source_dir": str(tmp_path / "nope")})
    assert job["state"] == "REJECTED" and "not found" in job["reason"]


# ------------------------------------------------------------------ learning and leasing
def test_finished_job_teaches_fitcheck_the_real_speed(tmp_path: Path) -> None:
    with live_cluster(tmp_path / "c", slots=2) as (ctl, _):
        job = wait_job(ctl, ctl.submit_job("fractal_tiles_v1", SMALL, "local")["id"])
        assert job["state"] == "SUCCEEDED"
        [w] = ctl.list_workers()
        assert w["perf"]["fractal_tiles_v1"] > 0
        assert w["capabilities"]["calibration"]["fractal_tiles_v1"] > 0
        nxt = ctl.submit_job("fractal_tiles_v1", SMALL, "auto")
        assert nxt["fitcheck"]["nodes"][0]["source"] == "observed"


def test_one_slow_job_cannot_wreck_the_learned_speed(tmp_path: Path) -> None:
    from daneion.controller.db import Worker

    with live_cluster(tmp_path / "c", slots=2) as (ctl, _):
        [w] = ctl.list_workers()
        inflated = 1e6  # far faster than reality, so the next job looks "slow" by ~1000x
        with ctl.db.tx() as s:
            s.get(Worker, w["id"]).perf = {"fractal_tiles_v1": inflated}
        job = wait_job(ctl, ctl.submit_job("fractal_tiles_v1", SMALL, "local")["id"])
        assert job["state"] == "SUCCEEDED"
        [w] = ctl.list_workers()
        assert w["perf"]["fractal_tiles_v1"] == pytest.approx(0.75 * inflated)


def test_biggest_tiles_are_leased_first(tmp_path: Path) -> None:
    ctl = Controller(ControllerConfig(data_dir=tmp_path))
    _, token = ctl.register_worker("w", is_local=True, slots=1)
    w = ctl.authenticate(token)
    job = ctl.submit_job("fractal_tiles_v1", get_workload("fractal_tiles_v1").presets["demo"],
                         "local")
    leases = ctl.poll(w, 5, wait_s=0)
    est = {t["id"]: t for t in ctl.job_tasks(job["id"])}
    assert len(leases) == 5 and est  # tasks exist
    # The first leased tile is the most expensive one in the whole job.
    from daneion.controller.db import Task

    with ctl.db.read() as s:
        units = {t.id: t.est_units for t in s.query(Task).filter(Task.job_id == job["id"])}
    assert units[leases[0]["task_id"]] == max(units.values())


# ------------------------------------------------------------------ benchmarks
def test_benchmark_order_alternates_modes() -> None:
    order = Controller.bench_order(runs=3, warmup=1)
    assert order[:2] == [("local", True), ("distributed", True)]
    measured = [m for m, warm in order if not warm]
    assert measured == ["local", "distributed", "distributed", "local", "local", "distributed"]


def test_benchmark_report_math() -> None:
    rows = [{"mode": "local", "warmup": True, "wall_s": 99, "result_sha256": "a"},
            {"mode": "local", "warmup": False, "wall_s": 40, "result_sha256": "a",
             "predicted_s": 44},
            {"mode": "local", "warmup": False, "wall_s": 42, "result_sha256": "a"},
            {"mode": "local", "warmup": False, "wall_s": 38, "result_sha256": "a"},
            {"mode": "distributed", "warmup": False, "wall_s": 20, "result_sha256": "a"},
            {"mode": "distributed", "warmup": False, "wall_s": 22, "result_sha256": "a"}]
    rep = benchmark_report(rows)
    assert rep["modes"]["local"]["median_s"] == 40 and rep["modes"]["local"]["runs"] == 3
    assert rep["modes"]["distributed"]["median_s"] == 21
    assert rep["speedup"] == round(40 / 21, 3)
    assert rep["modes"]["local"]["prediction_error"] == 0.1
    assert rep["identical_outputs"] is True


def test_benchmark_runs_end_to_end(tmp_path: Path) -> None:
    with live_cluster(tmp_path / "c", slots=2) as (ctl, base):
        b = httpx.post(base + "/api/v1/benchmarks", json={
            "handler": "fractal_tiles_v1", "params": SMALL, "runs": 1, "warmup": 0}).json()
        assert httpx.post(base + "/api/v1/benchmarks", json={
            "handler": "fractal_tiles_v1", "params": SMALL}).status_code == 409
        end = time.monotonic() + 60
        while b["state"] == "RUNNING" and time.monotonic() < end:
            time.sleep(0.2)
            b = httpx.get(f"{base}/api/v1/benchmarks/{b['id']}").json()
        assert b["state"] == "DONE", b
        assert [r["mode"] for r in b["results"]] == ["local", "distributed"]
        assert b["report"]["identical_outputs"] is True and b["report"]["speedup"] > 0


# ------------------------------------------------------------------ migration
def test_old_database_gains_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE workers (id VARCHAR PRIMARY KEY, name VARCHAR, token_hash VARCHAR UNIQUE,
            is_local BOOLEAN, state VARCHAR, os VARCHAR, capabilities JSON, slots INTEGER,
            paired_at FLOAT, last_seen_at FLOAT, version INTEGER);
        CREATE TABLE meta (key VARCHAR PRIMARY KEY, value VARCHAR);
        INSERT INTO meta VALUES ('schema_version', '1');
        INSERT INTO workers VALUES ('w1','old','h',0,'ONLINE','',  '{}',4,0,0,1);""")
    con.commit()
    con.close()
    Database(path)
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(workers)")}
    assert "perf" in cols
    assert con.execute("SELECT value FROM meta").fetchone()[0] == "2"
    assert con.execute("SELECT name FROM workers").fetchone()[0] == "old"
