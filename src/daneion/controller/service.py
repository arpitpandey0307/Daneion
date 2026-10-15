"""Controller core: admission, leasing with fencing tokens, verification and recovery.

Every state change happens inside `db.tx()` (one writer), goes through the transition tables in
`states.py`, bumps the row version and appends a timeline event in the same transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from daneion.config import ControllerConfig
from daneion.controller import errors
from daneion.controller.db import Attempt, Database, EventRow, Job, Task, Worker
from daneion.controller.states import (
    ATTEMPT_LIVE,
    JOB_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    JobState,
    TaskState,
    WorkerState,
    check_job,
    check_task,
)
from daneion.workloads import WorkloadError, get_workload

log = logging.getLogger("daneion.controller")

# Failure classes a worker may report. Anything else is treated as non-retryable.
# Pairing codes avoid look-alike characters (0/O, 1/I/L) so they can be read across a room.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

RETRYABLE = {"timeout", "killed", "worker_lost", "transfer", "resource", "lease_expired"}
MODES = {"local", "distributed", "auto"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Controller:
    def __init__(
        self,
        cfg: ControllerConfig,
        db: Database | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or Database(cfg.db_path)
        self.clock = clock
        self.work_ready = threading.Condition()
        self.events_ready = threading.Condition()
        self._finalizing: set[str] = set()
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None
        self._pair_lock = threading.Lock()
        self._pairing: dict[str, Any] | None = None  # {hash, expires_at, failures}
        self._pair_failures: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ lifecycle
    def start_background(self) -> None:
        self.recover()
        self._sweeper = threading.Thread(target=self._sweep_loop, name="sweeper", daemon=True)
        self._sweeper.start()

    def stop_background(self) -> None:
        self._stop.set()
        self._notify_work()

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self.cfg.sweep_interval_s):
            try:
                self.sweep()
            except Exception:  # keep sweeping; a bad tick must not kill recovery
                log.exception("sweep failed")

    def recover(self) -> None:
        """After a controller restart: finish jobs whose tasks all succeeded."""
        with self.db.read() as s:
            ids = s.scalars(select(Job.id).where(Job.state == JobState.RUNNING)).all()
        for job_id in ids:
            self._maybe_finalize(job_id)

    # ------------------------------------------------------------------ helpers
    def _event(self, s: Session, kind: str, message: str = "", *, job_id: str | None = None,
               task_id: str | None = None, worker_id: str | None = None,
               **data: Any) -> None:
        s.add(EventRow(ts=self.clock(), kind=kind, job_id=job_id, task_id=task_id,
                       worker_id=worker_id, message=message, data=data))

    def _notify_work(self) -> None:
        with self.work_ready:
            self.work_ready.notify_all()

    def _notify_events(self) -> None:
        with self.events_ready:
            self.events_ready.notify_all()

    @staticmethod
    def _set_job(job: Job, dst: JobState) -> None:
        check_job(job.state, dst)
        job.state = dst
        job.version += 1

    @staticmethod
    def _set_task(task: Task, dst: TaskState) -> None:
        check_task(task.state, dst)
        task.state = dst
        task.version += 1

    def _backoff(self, attempts: int) -> float:
        cap = min(self.cfg.retry_cap_s, self.cfg.retry_base_s * 2 ** max(0, attempts - 1))
        return cap * (0.5 + random.random() * 0.5)

    # ------------------------------------------------------------------ workers
    def register_worker(self, name: str, *, is_local: bool = False, os_name: str = "",
                        capabilities: dict[str, Any] | None = None,
                        slots: int = 1) -> tuple[str, str]:
        """Create a worker identity and return (worker_id, bearer token). Token is stored hashed."""
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.db.tx() as s:
            active = s.scalar(select(func.count()).select_from(Worker)
                              .where(Worker.state != WorkerState.REVOKED)) or 0
            if active >= self.cfg.max_nodes:
                raise errors.ClusterFull(
                    f"cluster already has {active} of {self.cfg.max_nodes} laptops")
            worker = Worker(id=new_id("wrk"), name=name[:64], token_hash=hash_token(token),
                            is_local=is_local, state=WorkerState.ONLINE, os=os_name,
                            capabilities=capabilities or {}, slots=max(1, slots),
                            paired_at=now, last_seen_at=now, version=1)
            s.add(worker)
            self._event(s, "worker.paired", f"{worker.name} joined", worker_id=worker.id,
                        local=is_local, slots=worker.slots)
        self._notify_events()
        return worker.id, token

    # ------------------------------------------------------------------ pairing
    def open_pairing(self) -> dict[str, Any]:
        """Issue a fresh one-time code (replaces any open one). Only the hash is kept."""
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        expires = self.clock() + self.cfg.pairing_ttl_s
        with self._pair_lock:
            self._pairing = {"hash": hash_token(raw), "expires_at": expires, "failures": 0}
        with self.db.tx() as s:
            self._event(s, "pairing.opened", "Pairing code issued",
                        ttl_s=self.cfg.pairing_ttl_s)
        self._notify_events()
        return {"code": f"{raw[:4]}-{raw[4:]}", "expires_at": expires,
                "ttl_s": self.cfg.pairing_ttl_s}

    def _check_room(self, name: str) -> None:
        """Raise ClusterFull unless a new laptop (or a re-pair of `name`) fits under the cap."""
        with self.db.read() as s:
            active = s.scalars(select(Worker).where(Worker.state != WorkerState.REVOKED)).all()
        replaceable = sum(1 for w in active if w.name == name and not w.is_local)
        if len(active) - replaceable >= self.cfg.max_nodes:
            lost = [w.name for w in active if w.state == WorkerState.LOST]
            hint = f" Revoke a lost laptop first: {', '.join(lost)}." if lost else ""
            raise errors.ClusterFull(
                f"cluster already has {len(active)} of {self.cfg.max_nodes} laptops.{hint}")

    def pairing_status(self) -> dict[str, Any]:
        with self._pair_lock:
            p = self._pairing
            if p is None or p["expires_at"] <= self.clock():
                return {"open": False}
            return {"open": True, "expires_at": p["expires_at"]}

    def pair(self, code: str, name: str, *, client_ip: str, os_name: str = "",
             capabilities: dict[str, Any] | None = None, slots: int = 1) -> tuple[str, str]:
        """Redeem a pairing code for a worker identity. Single use, expiring, rate-limited."""
        now = self.clock()
        normalised = "".join(ch for ch in code.upper() if ch.isalnum())
        with self._pair_lock:
            recent = [t for t in self._pair_failures.get(client_ip, []) if now - t < 60]
            self._pair_failures[client_ip] = recent
            if len(recent) >= self.cfg.pair_ip_failures_per_min:
                raise errors.RateLimited("too many wrong pairing codes; wait a minute")
            p = self._pairing
            valid = (p is not None and p["expires_at"] > now
                     and hmac.compare_digest(p["hash"], hash_token(normalised)))
            if not valid:
                recent.append(now)
                if p is not None:
                    p["failures"] += 1
                    if p["failures"] >= self.cfg.pairing_max_failures:
                        self._pairing = None
                reason = ("pairing code expired or not issued" if p is None
                          or p["expires_at"] <= now else "wrong pairing code")
                with self.db.tx() as s:
                    self._event(s, "pairing.failed", f"Pairing failed from {client_ip}: {reason}")
                self._notify_events()
                raise errors.PairingFailed(reason)
            name = (name.strip() or client_ip)[:64]
            self._check_room(name)  # before burning the code, so a full cluster keeps it valid
            self._pairing = None  # single use
        with self.db.tx() as s:
            # Re-pairing the same laptop replaces its old identity instead of using a new slot.
            for old in s.scalars(select(Worker).where(
                    Worker.name == name, Worker.is_local.is_(False),
                    Worker.state != WorkerState.REVOKED)).all():
                old.state = WorkerState.REVOKED
                old.version += 1
                self._release_worker_attempts(s, old, "replaced by re-pairing")
                self._event(s, "worker.revoked", f"{old.name} replaced by a new pairing",
                            worker_id=old.id)
        return self.register_worker(name, os_name=os_name, capabilities=capabilities,
                                    slots=slots)

    def authenticate(self, token: str | None) -> Worker:
        if not token:
            raise errors.Unauthorized("missing worker token")
        with self.db.read() as s:
            worker = s.scalar(select(Worker).where(Worker.token_hash == hash_token(token)))
        if worker is None or worker.state == WorkerState.REVOKED:
            raise errors.Unauthorized("unknown or revoked worker token")
        return worker

    def update_worker(self, worker_id: str, *, capabilities: dict[str, Any] | None = None,
                      slots: int | None = None, os_name: str | None = None) -> None:
        with self.db.tx() as s:
            w = s.get(Worker, worker_id)
            if w is None:
                raise errors.NotFound("worker")
            if capabilities is not None:
                w.capabilities = capabilities
            if slots is not None:
                w.slots = max(1, slots)
            if os_name is not None:
                w.os = os_name
            w.version += 1

    def find_worker(self, id_or_name: str) -> str:
        with self.db.read() as s:
            w = s.get(Worker, id_or_name) or s.scalar(
                select(Worker).where(Worker.name == id_or_name,
                                     Worker.state != WorkerState.REVOKED))
            if w is None:
                raise errors.NotFound(f"no worker {id_or_name!r}")
            return w.id

    def last_event_id(self) -> int:
        with self.db.read() as s:
            return s.scalar(select(func.max(EventRow.id))) or 0

    def set_worker_state(self, worker_id: str, state: WorkerState) -> None:
        """Owner controls: PAUSED stops new leases, REVOKED also invalidates the token."""
        with self.db.tx() as s:
            w = s.get(Worker, worker_id)
            if w is None:
                raise errors.NotFound("worker")
            if w.state == WorkerState.REVOKED:
                raise errors.Conflict("worker is revoked")
            w.state = state
            w.version += 1
            self._event(s, f"worker.{state.lower()}", f"{w.name} {state.lower()}",
                        worker_id=w.id)
            if state == WorkerState.REVOKED:
                self._release_worker_attempts(s, w, "revoked")
        self._notify_work()
        self._notify_events()

    def _touch(self, s: Session, worker: Worker) -> Worker:
        w = s.get(Worker, worker.id)
        assert w is not None
        w.last_seen_at = self.clock()
        if w.state in (WorkerState.SUSPECT, WorkerState.LOST):
            self._event(s, "worker.online", f"{w.name} is back", worker_id=w.id)
            w.state = WorkerState.ONLINE
        return w

    # ------------------------------------------------------------------ jobs
    def submit_job(self, handler: str, params: dict[str, Any],
                   mode: str = "auto") -> dict[str, Any]:
        if mode not in MODES:
            raise errors.InvalidRequest(f"mode must be one of {sorted(MODES)}")
        now = self.clock()
        job = Job(id=new_id("job"), handler=handler, params=params, mode=mode,
                  state=JobState.SUBMITTED, created_at=now, version=1, task_count=0)
        with self.db.tx() as s:
            s.add(job)
            self._event(s, "job.submitted", f"{handler} submitted ({mode})", job_id=job.id)
            self._set_job(job, JobState.VALIDATING)
            try:
                workload = get_workload(handler)
                normalised = workload.validate(params)
                task_params = workload.split(normalised)
            except WorkloadError as exc:
                self._set_job(job, JobState.REJECTED)
                job.reason = str(exc)
                job.finished_at = now
                self._event(s, "job.rejected", str(exc), job_id=job.id)
                return self._job_dict(s, job)
            job.params = normalised
            job.task_count = len(task_params)
            self._set_job(job, JobState.ADMITTED)
            self._set_job(job, JobState.FITCHECK)
            decision = self._fitcheck(s, job, workload)
            job.placement = decision["placement"]
            job.fitcheck = decision
            self._event(s, "job.fitcheck", decision["reason"], job_id=job.id, **decision)
            for i, tp in enumerate(task_params):
                s.add(Task(id=new_id("tsk"), job_id=job.id, idx=i, params=tp,
                           state=TaskState.QUEUED, attempts=0, fence=0, eligible_at=0.0,
                           version=1))
            self._set_job(job, JobState.QUEUED)
            self._event(s, "job.queued", f"{len(task_params)} tasks queued", job_id=job.id)
            result = self._job_dict(s, job)
        self._notify_work()
        self._notify_events()
        return result

    def _fitcheck(self, s: Session, job: Job, workload: Any) -> dict[str, Any]:
        """Placeholder until FitCheck v0 (FIT tasks): honour explicit modes; for auto, use
        remote laptops only when at least one is online."""
        remote = s.scalar(select(func.count()).select_from(Worker).where(
            Worker.is_local.is_(False), Worker.state == WorkerState.ONLINE)) or 0
        if job.mode == "local":
            return {"placement": "local", "reason": "Local run requested", "remote_workers": remote}
        if job.mode == "distributed":
            return {"placement": "distributed", "reason": "Distributed run requested",
                    "remote_workers": remote}
        if remote == 0:
            return {"placement": "local", "reason": "No other laptop online: run locally",
                    "remote_workers": 0}
        return {"placement": "distributed",
                "reason": f"{remote} other laptop(s) online: distribute",
                "remote_workers": remote}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self.db.tx() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise errors.NotFound("job")
            if job.state in JOB_TERMINAL:
                return self._job_dict(s, job)
            self._cancel_tasks(s, job, "cancelled by user")
            self._set_job(job, JobState.CANCELLED)
            job.finished_at = self.clock()
            self._event(s, "job.cancelled", "Cancelled by user", job_id=job.id)
            result = self._job_dict(s, job)
        self._notify_events()
        return result

    def _cancel_tasks(self, s: Session, job: Job, why: str) -> None:
        tasks = s.scalars(select(Task).where(Task.job_id == job.id)).all()
        for t in tasks:
            if t.state in TASK_TERMINAL:
                continue
            self._set_task(t, TaskState.CANCELLED)
            t.last_error = why
            if t.current_attempt_id:
                a = s.get(Attempt, t.current_attempt_id)
                if a and a.state in ATTEMPT_LIVE:
                    a.state = AttemptState.CANCELLED
                    a.finished_at = self.clock()

    # ------------------------------------------------------------------ leasing
    def poll(self, worker: Worker, free_slots: int, wait_s: float | None = None) -> list[dict]:
        """Long-poll for work. Returns as soon as at least one lease is granted."""
        deadline = self.clock() + (self.cfg.long_poll_s if wait_s is None else wait_s)
        while True:
            leases = self._lease(worker, free_slots)
            remaining = deadline - self.clock()
            if leases or remaining <= 0 or self._stop.is_set():
                return leases
            with self.work_ready:
                self.work_ready.wait(timeout=min(remaining, 1.0))

    def _lease(self, worker: Worker, free_slots: int) -> list[dict]:
        if free_slots <= 0:
            with self.db.tx() as s:
                if self._touch(s, worker).state == WorkerState.REVOKED:
                    raise errors.Unauthorized("worker was revoked")
            return []
        now = self.clock()
        out: list[dict] = []
        with self.db.tx() as s:
            w = self._touch(s, worker)
            if w.state == WorkerState.REVOKED:
                raise errors.Unauthorized("worker was revoked")  # ends a waiting long-poll
            if w.state != WorkerState.ONLINE:
                return []
            q = (select(Task, Job).join(Job, Job.id == Task.job_id)
                 .where(Task.state.in_([TaskState.QUEUED, TaskState.RETRY_WAIT]),
                        Task.eligible_at <= now,
                        Job.state.in_([JobState.QUEUED, JobState.RUNNING]))
                 .order_by(Job.created_at, Task.idx))
            if not w.is_local:
                q = q.where(Job.placement == "distributed")
            for task, job in s.execute(q.limit(free_slots)).all():
                task.fence += 1
                task.attempts += 1
                attempt = Attempt(id=new_id("att"), task_id=task.id, job_id=job.id,
                                  worker_id=w.id, fence=task.fence, state=AttemptState.LEASED,
                                  leased_at=now, lease_expires_at=now + self.cfg.lease_ttl_s)
                s.add(attempt)
                self._set_task(task, TaskState.LEASED)
                task.current_attempt_id = attempt.id
                task.worker_id = w.id
                if job.state == JobState.QUEUED:
                    self._set_job(job, JobState.RUNNING)
                    job.started_at = now
                    self._event(s, "job.running", "First task leased", job_id=job.id)
                self._event(s, "task.leased", f"tile {task.idx} -> {w.name}", job_id=job.id,
                            task_id=task.id, worker_id=w.id, idx=task.idx,
                            attempt=task.attempts, fence=task.fence)
                out.append({"attempt_id": attempt.id, "task_id": task.id, "job_id": job.id,
                            "fence": task.fence, "handler": job.handler, "params": task.params,
                            "lease_ttl_s": self.cfg.lease_ttl_s})
        if out:
            self._notify_events()
        return out

    def _current(self, s: Session, worker: Worker, attempt_id: str,
                 fence: int) -> tuple[Attempt, Task]:
        """Fencing check (INV: only the current attempt with the current token may write)."""
        a = s.get(Attempt, attempt_id)
        if a is None or a.worker_id != worker.id:
            raise errors.NotFound("attempt")
        t = s.get(Task, a.task_id)
        assert t is not None
        if (a.fence != fence or t.fence != fence or t.current_attempt_id != a.id
                or a.state not in ATTEMPT_LIVE):
            raise errors.StaleAttempt(
                f"attempt {a.id} (fence {fence}) is not current; task fence is {t.fence}")
        return a, t

    def accept(self, worker: Worker, attempt_id: str, fence: int) -> None:
        with self.db.tx() as s:
            self._touch(s, worker)
            a, t = self._current(s, worker, attempt_id, fence)
            if a.state != AttemptState.LEASED:
                return  # idempotent
            a.state = AttemptState.ACCEPTED
            a.lease_expires_at = self.clock() + self.cfg.lease_ttl_s
            self._set_task(t, TaskState.ACCEPTED)

    def start(self, worker: Worker, attempt_id: str, fence: int) -> None:
        with self.db.tx() as s:
            self._touch(s, worker)
            a, t = self._current(s, worker, attempt_id, fence)
            if a.state == AttemptState.RUNNING:
                return
            if a.state == AttemptState.LEASED:
                a.state = AttemptState.ACCEPTED
                self._set_task(t, TaskState.ACCEPTED)
            a.state = AttemptState.RUNNING
            a.started_at = self.clock()
            a.lease_expires_at = self.clock() + self.cfg.lease_ttl_s
            self._set_task(t, TaskState.RUNNING)
            self._event(s, "task.running", f"tile {t.idx} running", job_id=t.job_id,
                        task_id=t.id, worker_id=worker.id, idx=t.idx)
        self._notify_events()

    def heartbeat(self, worker: Worker, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        """Extend leases of current attempts; tell the worker which ones to stop."""
        stop: list[str] = []
        now = self.clock()
        with self.db.tx() as s:
            w = self._touch(s, worker)
            for item in attempts:
                try:
                    a, _ = self._current(s, worker, str(item["attempt_id"]), int(item["fence"]))
                except (errors.StaleAttempt, errors.NotFound, KeyError, ValueError):
                    stop.append(str(item.get("attempt_id")))
                    continue
                a.lease_expires_at = now + self.cfg.lease_ttl_s
            state = w.state
        return {"stop": stop, "worker_state": state,
                "heartbeat_interval_s": self.cfg.heartbeat_interval_s}

    # ------------------------------------------------------------------ results
    def complete(self, worker: Worker, attempt_id: str, fence: int, data: bytes, sha256: str,
                 duration_s: float | None = None) -> dict[str, Any]:
        if len(data) > self.cfg.max_result_bytes:
            raise errors.InvalidRequest("result too large", code="RESULT_TOO_LARGE")
        actual = hashlib.sha256(data).hexdigest()
        finished_job: str | None = None
        with self.db.tx() as s:
            self._touch(s, worker)
            a, t = self._current(s, worker, attempt_id, fence)
            if a.state == AttemptState.LEASED:
                a.state = AttemptState.ACCEPTED
                self._set_task(t, TaskState.ACCEPTED)
            if t.state == TaskState.ACCEPTED:
                self._set_task(t, TaskState.RUNNING)
            self._set_task(t, TaskState.VERIFYING)
            a.finished_at = self.clock()
            a.duration_s = duration_s
            if actual != sha256.lower():
                self._fail_attempt(s, a, t, "transfer",
                                   f"SHA-256 mismatch: sent {sha256[:12]}, got {actual[:12]}")
                verdict = "retry"
            else:
                job = s.get(Job, t.job_id)
                assert job is not None
                try:
                    get_workload(job.handler).verify(t.params, data)
                except WorkloadError as exc:
                    self._fail_attempt(s, a, t, "invalid_output", str(exc))
                    verdict = "rejected"
                else:
                    path = self.cfg.artifacts_dir / job.id / f"{t.idx:05d}.bin"
                    _atomic_write(path, data)
                    a.state = AttemptState.SUCCEEDED
                    self._set_task(t, TaskState.SUCCEEDED)
                    t.output_path = str(path)
                    t.output_sha256 = actual
                    self._event(s, "task.succeeded", f"tile {t.idx} verified", job_id=job.id,
                                task_id=t.id, worker_id=worker.id, idx=t.idx,
                                duration_s=duration_s, sha256=actual)
                    verdict = "accepted"
                    pending = s.scalar(select(func.count()).select_from(Task).where(
                        Task.job_id == job.id, Task.state != TaskState.SUCCEEDED)) or 0
                    if pending == 0:
                        finished_job = job.id
        self._notify_work()
        self._notify_events()
        if finished_job:
            threading.Thread(target=self._maybe_finalize, args=(finished_job,),
                             name=f"finalize-{finished_job}", daemon=True).start()
        return {"verdict": verdict, "sha256": actual}

    def fail(self, worker: Worker, attempt_id: str, fence: int, error_class: str,
             message: str) -> None:
        with self.db.tx() as s:
            self._touch(s, worker)
            a, t = self._current(s, worker, attempt_id, fence)
            a.finished_at = self.clock()
            self._fail_attempt(s, a, t, error_class, message[:2000])
        self._notify_work()
        self._notify_events()

    def _fail_attempt(self, s: Session, a: Attempt, t: Task, error_class: str,
                      message: str, *, attempt_state: AttemptState = AttemptState.FAILED) -> None:
        a.state = attempt_state
        a.error_class = error_class
        a.error = message
        t.last_error = f"{error_class}: {message}"
        t.current_attempt_id = None
        job = s.get(Job, t.job_id)
        assert job is not None
        if error_class in RETRYABLE and t.attempts < self.cfg.max_attempts:
            delay = self._backoff(t.attempts)
            self._set_task(t, TaskState.RETRY_WAIT)
            t.eligible_at = self.clock() + delay
            self._event(s, "task.retry", f"tile {t.idx}: {error_class}, retry in {delay:.1f}s",
                        job_id=job.id, task_id=t.id, worker_id=a.worker_id, idx=t.idx,
                        error_class=error_class)
            return
        self._set_task(t, TaskState.FAILED)
        self._event(s, "task.failed", f"tile {t.idx}: {error_class}: {message}", job_id=job.id,
                    task_id=t.id, worker_id=a.worker_id, idx=t.idx, error_class=error_class)
        if job.state not in JOB_TERMINAL:
            self._cancel_tasks(s, job, f"job failed: task {t.idx}")
            self._set_job(job, JobState.FAILED)
            job.reason = f"task {t.idx} failed: {error_class}: {message}"
            job.finished_at = self.clock()
            self._event(s, "job.failed", job.reason, job_id=job.id)

    def _release_worker_attempts(self, s: Session, w: Worker, why: str) -> None:
        live = s.scalars(select(Attempt).where(Attempt.worker_id == w.id,
                                               Attempt.state.in_(list(ATTEMPT_LIVE)))).all()
        for a in live:
            t = s.get(Task, a.task_id)
            if t and t.current_attempt_id == a.id and t.state not in TASK_TERMINAL:
                a.finished_at = self.clock()
                self._fail_attempt(s, a, t, "worker_lost", why, attempt_state=AttemptState.EXPIRED)
            else:
                a.state = AttemptState.EXPIRED

    # ------------------------------------------------------------------ recovery
    def sweep(self) -> None:
        now = self.clock()
        changed = False
        with self.db.tx() as s:
            expired = s.scalars(select(Attempt).where(Attempt.state.in_(list(ATTEMPT_LIVE)),
                                                      Attempt.lease_expires_at < now)).all()
            for a in expired:
                if a.state not in ATTEMPT_LIVE:  # already settled earlier in this sweep
                    continue
                t = s.get(Task, a.task_id)
                assert t is not None
                a.finished_at = now
                if t.current_attempt_id == a.id and t.state not in TASK_TERMINAL:
                    self._fail_attempt(s, a, t, "lease_expired", "no heartbeat before lease expiry",
                                       attempt_state=AttemptState.EXPIRED)
                else:
                    a.state = AttemptState.EXPIRED
                changed = True
            suspect_after = self.cfg.heartbeat_interval_s * self.cfg.suspect_after_missed
            for w in s.scalars(select(Worker).where(
                    Worker.state.in_([WorkerState.ONLINE, WorkerState.SUSPECT]))).all():
                silent = now - w.last_seen_at
                if silent > self.cfg.lease_ttl_s and w.state != WorkerState.LOST:
                    w.state = WorkerState.LOST
                    self._event(s, "worker.lost", f"{w.name} lost ({silent:.0f}s silent)",
                                worker_id=w.id)
                    changed = True
                elif silent > suspect_after and w.state == WorkerState.ONLINE:
                    w.state = WorkerState.SUSPECT
                    self._event(s, "worker.suspect", f"{w.name} missed heartbeats",
                                worker_id=w.id)
                    changed = True
        if changed:
            self._notify_work()
            self._notify_events()

    # ------------------------------------------------------------------ finalisation
    def _maybe_finalize(self, job_id: str) -> None:
        with self.db.tx() as s:
            if job_id in self._finalizing:
                return
            job = s.get(Job, job_id)
            if job is None or job.state != JobState.RUNNING:
                return
            tasks = s.scalars(select(Task).where(Task.job_id == job_id)
                              .order_by(Task.idx)).all()
            if any(t.state != TaskState.SUCCEEDED for t in tasks):
                return
            self._finalizing.add(job_id)
            handler, params = job.handler, job.params
            paths = [(t.output_path, t.output_sha256) for t in tasks]
        try:
            outputs = []
            for path, sha in paths:
                data = Path(str(path)).read_bytes()
                if hashlib.sha256(data).hexdigest() != sha:
                    raise RuntimeError(f"stored artifact {path} changed on disk")
                outputs.append(data)
            result, name = get_workload(handler).merge(params, outputs)
            out = self.cfg.artifacts_dir / job_id / name
            _atomic_write(out, result)
            with self.db.tx() as s:
                job = s.get(Job, job_id)
                assert job is not None
                if job.state == JobState.RUNNING:
                    self._set_job(job, JobState.SUCCEEDED)
                    job.finished_at = self.clock()
                    job.result_path = str(out)
                    job.result_sha256 = hashlib.sha256(result).hexdigest()
                    wall = job.finished_at - (job.started_at or job.created_at)
                    self._event(s, "job.succeeded", f"Done in {wall:.2f}s", job_id=job_id,
                                wall_s=wall)
        except Exception as exc:
            log.exception("finalize %s failed", job_id)
            with self.db.tx() as s:
                job = s.get(Job, job_id)
                if job is not None and job.state == JobState.RUNNING:
                    self._set_job(job, JobState.FAILED)
                    job.reason = f"merge failed: {exc}"
                    job.finished_at = self.clock()
                    self._event(s, "job.failed", job.reason, job_id=job_id)
        finally:
            self._finalizing.discard(job_id)
            self._notify_events()

    # ------------------------------------------------------------------ queries
    def _job_dict(self, s: Session, job: Job) -> dict[str, Any]:
        counts = dict(s.execute(select(Task.state, func.count()).where(Task.job_id == job.id)
                                .group_by(Task.state)).all())
        end = job.finished_at or (self.clock() if job.started_at else None)
        return {
            "id": job.id, "handler": job.handler, "params": job.params, "mode": job.mode,
            "placement": job.placement, "fitcheck": job.fitcheck, "state": job.state,
            "reason": job.reason, "task_count": job.task_count, "task_states": counts,
            "created_at": job.created_at, "started_at": job.started_at,
            "finished_at": job.finished_at,
            "wall_s": (end - job.started_at) if (end and job.started_at) else None,
            "result_sha256": job.result_sha256, "has_result": job.result_path is not None,
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.db.read() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise errors.NotFound("job")
            d = self._job_dict(s, job)
            rows = s.execute(
                select(Worker.id, Worker.name, func.count(Attempt.id), func.sum(Attempt.duration_s))
                .join(Attempt, Attempt.worker_id == Worker.id)
                .where(Attempt.job_id == job_id, Attempt.state == AttemptState.SUCCEEDED)
                .group_by(Worker.id, Worker.name)).all()
            d["per_worker"] = [{"worker_id": r[0], "name": r[1], "tasks": r[2],
                                "busy_s": r[3] or 0.0} for r in rows]
            return d

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.read() as s:
            jobs = s.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit)).all()
            return [self._job_dict(s, j) for j in jobs]

    def job_tasks(self, job_id: str) -> list[dict[str, Any]]:
        with self.db.read() as s:
            if s.get(Job, job_id) is None:
                raise errors.NotFound("job")
            tasks = s.scalars(select(Task).where(Task.job_id == job_id).order_by(Task.idx)).all()
            return [{"id": t.id, "idx": t.idx, "state": t.state, "attempts": t.attempts,
                     "fence": t.fence, "worker_id": t.worker_id, "last_error": t.last_error,
                     "x0": t.params.get("x0"), "y0": t.params.get("y0"),
                     "tw": t.params.get("tw"), "th": t.params.get("th")} for t in tasks]

    def job_result(self, job_id: str) -> Path:
        with self.db.read() as s:
            job = s.get(Job, job_id)
            if job is None or not job.result_path:
                raise errors.NotFound("job result")
            return Path(job.result_path)

    def list_workers(self) -> list[dict[str, Any]]:
        now = self.clock()
        with self.db.read() as s:
            workers = s.scalars(select(Worker).order_by(Worker.paired_at)).all()
            busy = dict(s.execute(select(Attempt.worker_id, func.count())
                                  .where(Attempt.state.in_(list(ATTEMPT_LIVE)))
                                  .group_by(Attempt.worker_id)).all())
            done = dict(s.execute(select(Attempt.worker_id, func.count())
                                  .where(Attempt.state == AttemptState.SUCCEEDED)
                                  .group_by(Attempt.worker_id)).all())
            return [{"id": w.id, "name": w.name, "is_local": w.is_local, "state": w.state,
                     "os": w.os, "capabilities": w.capabilities, "slots": w.slots,
                     "running": busy.get(w.id, 0), "completed": done.get(w.id, 0),
                     "last_seen_s": round(now - w.last_seen_at, 1)} for w in workers]

    def events_since(self, after_id: int = 0, job_id: str | None = None,
                     limit: int = 500) -> list[dict[str, Any]]:
        with self.db.read() as s:
            q = select(EventRow).where(EventRow.id > after_id)
            if job_id:
                q = q.where(EventRow.job_id == job_id)
            rows = s.scalars(q.order_by(EventRow.id).limit(limit)).all()
            return [{"id": e.id, "ts": e.ts, "kind": e.kind, "job_id": e.job_id,
                     "task_id": e.task_id, "worker_id": e.worker_id, "message": e.message,
                     "data": e.data} for e in rows]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:6]}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
