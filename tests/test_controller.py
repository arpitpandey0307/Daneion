from __future__ import annotations

import hashlib
import time

import pytest

from daneion.controller import errors
from daneion.controller.service import Controller
from daneion.controller.states import IllegalTransition, TaskState, check_task
from daneion.workloads import get_workload
from tests.conftest import SMALL, FakeClock

wl = get_workload("fractal_tiles_v1")
ONE_TILE = {**SMALL, "width": 64, "height": 64, "tile": 64}


def worker(ctl: Controller, name: str = "w", local: bool = False):  # type: ignore[no-untyped-def]
    _, token = ctl.register_worker(name, is_local=local, slots=4)
    return ctl.authenticate(token)


def run_lease(lease: dict) -> tuple[bytes, str]:
    data = wl.run(lease["params"])
    return data, hashlib.sha256(data).hexdigest()


def wait_state(ctl: Controller, job_id: str, state: str, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = ctl.get_job(job_id)
        if job["state"] == state:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job stayed {ctl.get_job(job_id)['state']}, wanted {state}")


def test_happy_path_runs_every_task_and_merges(ctl: Controller) -> None:
    w = worker(ctl, local=True)
    job = ctl.submit_job("fractal_tiles_v1", SMALL, "local")
    assert job["state"] == "QUEUED" and job["task_count"] == 6
    leases = ctl.poll(w, 10, wait_s=0)
    assert len(leases) == 6 and all(lease["fence"] == 1 for lease in leases)
    for lease in leases:
        ctl.accept(w, lease["attempt_id"], lease["fence"])
        ctl.start(w, lease["attempt_id"], lease["fence"])
        data, sha = run_lease(lease)
        assert ctl.complete(w, lease["attempt_id"], lease["fence"], data, sha)["verdict"] == \
            "accepted"
    done = wait_state(ctl, job["id"], "SUCCEEDED")
    assert done["result_sha256"] and ctl.job_result(job["id"]).exists()
    assert done["per_worker"][0]["tasks"] == 6


def test_local_placement_is_never_leased_to_remote_worker(ctl: Controller) -> None:
    remote = worker(ctl, "remote")
    ctl.submit_job("fractal_tiles_v1", SMALL, "local")
    assert ctl.poll(remote, 4, wait_s=0) == []


def test_stale_attempt_cannot_publish_after_lease_expiry(ctl: Controller,
                                                         clock: FakeClock) -> None:
    a, b = worker(ctl, "a"), worker(ctl, "b")
    ctl.submit_job("fractal_tiles_v1", ONE_TILE, "distributed")
    [old] = ctl.poll(a, 1, wait_s=0)
    ctl.start(a, old["attempt_id"], old["fence"])
    clock.advance(ctl.cfg.lease_ttl_s + 1)
    ctl.sweep()
    [new] = ctl.poll(b, 1, wait_s=0)
    assert new["task_id"] == old["task_id"] and new["fence"] == old["fence"] + 1
    data, sha = run_lease(old)
    with pytest.raises(errors.StaleAttempt):
        ctl.complete(a, old["attempt_id"], old["fence"], data, sha)
    assert ctl.complete(b, new["attempt_id"], new["fence"], data, sha)["verdict"] == "accepted"


def test_heartbeat_keeps_lease_alive(ctl: Controller, clock: FakeClock) -> None:
    a = worker(ctl)
    ctl.submit_job("fractal_tiles_v1", ONE_TILE, "distributed")
    [lease] = ctl.poll(a, 1, wait_s=0)
    for _ in range(5):
        clock.advance(ctl.cfg.lease_ttl_s * 0.6)
        reply = ctl.heartbeat(a, [{"attempt_id": lease["attempt_id"], "fence": lease["fence"]}])
        assert reply["stop"] == []
        ctl.sweep()
    data, sha = run_lease(lease)
    assert ctl.complete(a, lease["attempt_id"], lease["fence"], data, sha)["verdict"] == "accepted"


def test_corrupted_upload_is_retried(ctl: Controller) -> None:
    a = worker(ctl)
    ctl.submit_job("fractal_tiles_v1", ONE_TILE, "distributed")
    [lease] = ctl.poll(a, 1, wait_s=0)
    data, sha = run_lease(lease)
    bad = b"\x00" + data[1:]
    assert ctl.complete(a, lease["attempt_id"], lease["fence"], bad, sha)["verdict"] == "retry"
    [again] = ctl.poll(a, 1, wait_s=0)
    assert again["fence"] == 2


def test_malformed_output_fails_job_without_retry(ctl: Controller) -> None:
    a = worker(ctl)
    job = ctl.submit_job("fractal_tiles_v1", SMALL, "distributed")
    lease = ctl.poll(a, 1, wait_s=0)[0]
    junk = b"abc"
    r = ctl.complete(a, lease["attempt_id"], lease["fence"], junk,
                     hashlib.sha256(junk).hexdigest())
    assert r["verdict"] == "rejected"
    j = ctl.get_job(job["id"])
    assert j["state"] == "FAILED" and j["task_states"].get("CANCELLED") == 5


def test_retries_are_bounded(ctl: Controller) -> None:
    a = worker(ctl)
    job = ctl.submit_job("fractal_tiles_v1", ONE_TILE, "distributed")
    for _ in range(ctl.cfg.max_attempts):
        [lease] = ctl.poll(a, 1, wait_s=0)
        ctl.fail(a, lease["attempt_id"], lease["fence"], "timeout", "slow")
    assert ctl.get_job(job["id"])["state"] == "FAILED"


def test_unknown_error_class_is_not_retried(ctl: Controller) -> None:
    a = worker(ctl)
    job = ctl.submit_job("fractal_tiles_v1", ONE_TILE, "distributed")
    [lease] = ctl.poll(a, 1, wait_s=0)
    ctl.fail(a, lease["attempt_id"], lease["fence"], "mystery", "??")
    assert ctl.get_job(job["id"])["state"] == "FAILED"


def test_cancel_stops_running_attempts(ctl: Controller) -> None:
    a = worker(ctl)
    job = ctl.submit_job("fractal_tiles_v1", SMALL, "distributed")
    leases = ctl.poll(a, 2, wait_s=0)
    ctl.cancel_job(job["id"])
    reply = ctl.heartbeat(a, [{"attempt_id": x["attempt_id"], "fence": x["fence"]}
                              for x in leases])
    assert sorted(reply["stop"]) == sorted(x["attempt_id"] for x in leases)
    assert ctl.poll(a, 4, wait_s=0) == []
    data, sha = run_lease(leases[0])
    with pytest.raises(errors.StaleAttempt):
        ctl.complete(a, leases[0]["attempt_id"], leases[0]["fence"], data, sha)


def test_revoked_worker_loses_access_and_work_is_released(ctl: Controller) -> None:
    _, token = ctl.register_worker("r", slots=1)
    w = ctl.authenticate(token)
    ctl.submit_job("fractal_tiles_v1", ONE_TILE, "distributed")
    [lease] = ctl.poll(w, 1, wait_s=0)
    ctl.set_worker_state(w.id, "REVOKED")  # type: ignore[arg-type]
    with pytest.raises(errors.Unauthorized):
        ctl.authenticate(token)
    other = worker(ctl, "other")
    [again] = ctl.poll(other, 1, wait_s=0)
    assert again["task_id"] == lease["task_id"] and again["fence"] == 2


def test_cluster_is_capped_at_five(ctl: Controller) -> None:
    for i in range(5):
        worker(ctl, f"n{i}")
    with pytest.raises(errors.ClusterFull):
        worker(ctl, "sixth")


def test_bad_params_are_rejected_with_reason(ctl: Controller) -> None:
    job = ctl.submit_job("fractal_tiles_v1", {"width": 1}, "auto")
    assert job["state"] == "REJECTED" and "width" in job["reason"]


def test_illegal_transition_raises() -> None:
    with pytest.raises(IllegalTransition):
        check_task(TaskState.SUCCEEDED, TaskState.RUNNING)


def test_worker_goes_suspect_then_lost(ctl: Controller, clock: FakeClock) -> None:
    worker(ctl)
    clock.advance(ctl.cfg.heartbeat_interval_s * ctl.cfg.suspect_after_missed + 1)
    ctl.sweep()
    assert ctl.list_workers()[0]["state"] == "SUSPECT"
    clock.advance(ctl.cfg.lease_ttl_s)
    ctl.sweep()
    assert ctl.list_workers()[0]["state"] == "LOST"
