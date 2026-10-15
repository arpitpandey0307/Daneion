"""Worker agent: pulls leases from the controller, runs each task in its own guarded child
process, heartbeats live attempts and uploads verified outputs (ADR-0003, ADR-0010)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import ssl
import statistics
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from daneion.build import build_info
from daneion.worker.probe import default_slots, os_name, probe
from daneion.worker.sandbox import EXIT_RESOURCE, TaskGuard, _kill_tree

log = logging.getLogger("daneion.worker")

# Only these variables reach a task process (Tier-0: scrubbed environment).
_ENV_KEEP = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP",
             "PYTHONPATH", "LANG", "LC_ALL", "HOME")
PROBE_BYTES = 2_000_000
CACHE_LIMIT_BYTES = 2 * 1024**3


@dataclass
class Running:
    lease: dict[str, Any]
    proc: subprocess.Popen[bytes] | None = None
    guard: TaskGuard | None = None
    stopped: bool = False
    started: float = field(default_factory=time.monotonic)


class StaleLease(Exception):
    pass


def _error_message(exc: Exception) -> str:
    try:
        return str(json.loads(str(exc))["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return str(exc)


def keep_awake(on: bool) -> None:
    """While this laptop is in the cluster, Windows must not go to sleep mid-job (the screen
    may still turn off). Released when the worker stops."""
    if sys.platform != "win32":
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    flags = es_continuous | (es_system_required if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


def calibrate_all() -> dict[str, float]:
    """Single-core speed for each built-in workload (best of two, ~0.5 s in total)."""
    from daneion.workloads import REGISTRY

    out = {}
    for name, wl in REGISTRY.items():
        try:
            out[name] = round(max(wl.calibrate() for _ in range(2)), 4)
        except Exception:  # a broken calibration must not stop the laptop from joining
            log.exception("calibration of %s failed", name)
    return out


class WorkerAgent:
    def __init__(self, base_url: str, token: str, *, name: str | None = None,
                 slots: int | None = None, work_dir: Path | None = None,
                 verify: bool | str | ssl.SSLContext = True, task_timeout_s: float = 900.0,
                 task_memory_mb: int = 2048, client: httpx.Client | None = None) -> None:
        self.slots = slots or default_slots()
        self.name = name
        self.work_dir = work_dir or Path.home() / ".daneion" / "worker"
        self.cache_dir = self.work_dir / "cache"
        self.task_timeout_s = task_timeout_s
        self.task_memory_mb = task_memory_mb
        self.base_url = base_url
        self.http = client or httpx.Client(
            base_url=base_url, verify=verify,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, read=40.0))
        self.heartbeat_interval_s = 3.0
        self.capabilities: dict[str, Any] = {}
        self._running: dict[str, Running] = {}
        self._lock = threading.Lock()
        self._slot_free = threading.Condition(self._lock)
        self._stop = threading.Event()

    # ---------------------------------------------------------------- http
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self.http.post(path, json=body)
        if r.status_code in (404, 409):
            raise StaleLease(r.text)
        r.raise_for_status()
        return r.json()

    # ---------------------------------------------------------------- startup
    def _clean_orphans(self) -> None:
        """Task folders left by a crash or power loss belong to attempts the controller has
        already expired; delete them. Trim the input cache if it grew too large."""
        shutil.rmtree(self.work_dir / "tasks", ignore_errors=True)
        if self.cache_dir.exists():
            files = sorted(self.cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            for p in files:
                if total <= CACHE_LIMIT_BYTES:
                    break
                total -= p.stat().st_size
                p.unlink(missing_ok=True)

    def measure_network(self) -> dict[str, float] | None:
        """RTT and throughput to the controller (FitCheck needs both). Skipped on loopback."""
        if urlparse(self.base_url).hostname in ("127.0.0.1", "localhost", "::1"):
            return None
        try:
            rtts = []
            for _ in range(5):
                t0 = time.perf_counter()
                self.http.get("/api/v1/health").raise_for_status()
                rtts.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            r = self.http.get("/api/v1/worker/netprobe", params={"size": PROBE_BYTES})
            r.raise_for_status()
            down = len(r.content) / (time.perf_counter() - t0)
            t0 = time.perf_counter()
            self.http.post("/api/v1/worker/netprobe", content=os.urandom(PROBE_BYTES),
                           headers={"Content-Type": "application/octet-stream"}
                           ).raise_for_status()
            up = PROBE_BYTES / (time.perf_counter() - t0)
        except httpx.HTTPError as exc:
            log.warning("network measurement failed: %s", exc)
            return None
        return {"rtt_ms": round(statistics.median(rtts) * 1000, 2),
                "down_mbps": round(down * 8 / 1e6, 1), "up_mbps": round(up * 8 / 1e6, 1)}

    # ---------------------------------------------------------------- main loop
    def run(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._clean_orphans()
        caps = probe()
        caps["calibration"] = calibrate_all()
        caps["task_memory_mb"] = self.task_memory_mb
        net = self._with_retry(self.measure_network) if not self._stop.is_set() else None
        if net:
            caps["net"] = net
            log.info("link to controller: %.0f ms, %.0f/%.0f Mbit/s down/up",
                     net["rtt_ms"], net["down_mbps"], net["up_mbps"])
        self.capabilities = caps
        try:
            hello = self._with_retry(lambda: self._post("/api/v1/worker/hello", {
                "name": self.name, "os": os_name(), "slots": self.slots, "capabilities": caps,
                "build": build_info()}))
        except StaleLease as exc:
            log.error("controller refused this laptop: %s", _error_message(exc))
            self.stop()
            return
        if hello is None:
            return
        keep_awake(True)
        self.heartbeat_interval_s = float(hello.get("heartbeat_interval_s", 3.0))
        log.info("connected as %s with %d slots", hello.get("name"), self.slots)
        hb = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        hb.start()
        backoff = 1.0
        while not self._stop.is_set():
            with self._lock:
                while len(self._running) >= self.slots and not self._stop.is_set():
                    self._slot_free.wait(timeout=1.0)
                free = self.slots - len(self._running)
            if self._stop.is_set():
                break
            try:
                leases = self._post("/api/v1/worker/poll", {"free_slots": free})["leases"]
                backoff = 1.0
            except (httpx.HTTPError, StaleLease) as exc:
                if (isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code == 401):
                    log.error("controller rejected this laptop's token (revoked or replaced); "
                              "pair again with `daneion join`")
                    self.stop()
                    break
                if "VERSION_MISMATCH" in str(exc):
                    log.error("controller refused this laptop: %s", _error_message(exc))
                    self.stop()
                    break
                log.warning("poll failed: %s; retrying in %.0fs", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)
                continue
            for lease in leases:
                with self._lock:
                    self._running[lease["attempt_id"]] = Running(lease)
                threading.Thread(target=self._execute, args=(lease,), daemon=True,
                                 name=f"task-{lease['attempt_id']}").start()
        self._kill_all()
        keep_awake(False)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._slot_free.notify_all()

    def _with_retry(self, fn: Any) -> Any:
        delay = 1.0
        while not self._stop.is_set():
            try:
                return fn()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    log.error("controller rejected this worker's token")
                    self.stop()
                    return None
                log.warning("controller error: %s", exc)
            except httpx.HTTPError as exc:
                log.warning("cannot reach controller: %s", exc)
            self._stop.wait(delay)
            delay = min(delay * 2, 15.0)
        return None

    # ---------------------------------------------------------------- heartbeat
    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            with self._lock:
                live = [{"attempt_id": a, "fence": r.lease["fence"]}
                        for a, r in self._running.items()]
            try:
                reply = self._post("/api/v1/worker/heartbeat", {"attempts": live})
            except (httpx.HTTPError, StaleLease) as exc:
                if (isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code == 401):
                    log.error("controller revoked this laptop; stopping all work")
                    self.stop()
                    self._kill_all()
                    return
                log.warning("heartbeat failed: %s", exc)
                continue
            for attempt_id in reply.get("stop", []):
                self._kill(attempt_id)
            if reply.get("worker_state") == "REVOKED":
                self.stop()

    def _kill(self, attempt_id: str) -> None:
        with self._lock:
            r = self._running.get(attempt_id)
            if r is None:
                return
            r.stopped = True
            guard, proc = r.guard, r.proc
        if guard:
            guard.kill()
        elif proc and proc.poll() is None:
            _kill_tree(proc)

    def _kill_all(self) -> None:
        with self._lock:
            ids = list(self._running)
        for a in ids:
            self._kill(a)

    # ---------------------------------------------------------------- inputs
    def _fetch_input(self, attempt_id: str, fence: int, sha: str) -> Path:
        """Download (or reuse from the content-addressed cache) and verify one input file."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        final = self.cache_dir / sha
        if final.exists():
            os.utime(final)  # LRU touch
            return final
        tmp = self.cache_dir / f".{uuid.uuid4().hex}.part"
        h = hashlib.sha256()
        try:
            with self.http.stream("GET", f"/api/v1/worker/attempts/{attempt_id}/inputs/{sha}",
                                  params={"fence": fence}) as r:
                if r.status_code in (404, 409):
                    raise StaleLease(r.read().decode(errors="replace"))
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        h.update(chunk)
                        fh.write(chunk)
            if h.hexdigest() != sha:
                raise OSError(f"input {sha[:12]} arrived corrupted")
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        return final

    # ---------------------------------------------------------------- execution
    def _execute(self, lease: dict[str, Any]) -> None:
        from daneion.workloads import get_workload

        attempt_id, fence = lease["attempt_id"], lease["fence"]
        task_dir = self.work_dir / "tasks" / attempt_id
        try:
            self._post(f"/api/v1/worker/attempts/{attempt_id}/accept", {"fence": fence})
            refs = get_workload(lease["handler"]).input_refs(lease["params"])
            try:
                inputs = {sha: str(self._fetch_input(attempt_id, fence, sha)) for sha in refs}
            except OSError as exc:
                self._fail(attempt_id, fence, "transfer", str(exc))
                return
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "task.json").write_text(json.dumps(
                {"handler": lease["handler"], "params": lease["params"], "inputs": inputs}),
                encoding="utf-8")
            self._post(f"/api/v1/worker/attempts/{attempt_id}/start", {"fence": fence})
            t0 = time.perf_counter()
            code, stderr = self._spawn(attempt_id, task_dir)
            duration = time.perf_counter() - t0
            with self._lock:
                stopped = self._running[attempt_id].stopped
            if stopped:
                log.info("attempt %s stopped by controller", attempt_id)
            elif code is None:
                self._fail(attempt_id, fence, "timeout", f"exceeded {self.task_timeout_s:.0f}s")
            elif code == 0:
                data = (task_dir / "output.bin").read_bytes()
                r = self.http.put(
                    f"/api/v1/worker/attempts/{attempt_id}/result", content=data,
                    headers={"X-Daneion-Fence": str(fence),
                             "X-Daneion-Sha256": hashlib.sha256(data).hexdigest(),
                             "X-Daneion-Duration": f"{duration:.4f}",
                             "Content-Type": "application/octet-stream"})
                if r.status_code in (404, 409):
                    raise StaleLease(r.text)
                r.raise_for_status()
            elif code == EXIT_RESOURCE:
                self._fail(attempt_id, fence, "resource_limit",
                           f"task exceeded its {self.task_memory_mb} MB memory cap")
            else:
                cls = "invalid_task" if code == 2 else "handler_error"
                self._fail(attempt_id, fence, cls, f"exit {code}: {stderr[-1500:]}")
        except StaleLease as exc:
            log.info("attempt %s is stale: %s", attempt_id, exc)
        except httpx.HTTPError as exc:
            # The lease will expire on the controller and the task is retried elsewhere.
            log.warning("attempt %s lost contact: %s", attempt_id, exc)
        except Exception:
            log.exception("attempt %s crashed in the agent", attempt_id)
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)
            with self._lock:
                self._running.pop(attempt_id, None)
                self._slot_free.notify_all()

    def _fail(self, attempt_id: str, fence: int, error_class: str, message: str) -> None:
        self._post(f"/api/v1/worker/attempts/{attempt_id}/fail",
                   {"fence": fence, "error_class": error_class, "message": message})

    def _spawn(self, attempt_id: str, task_dir: Path) -> tuple[int | None, str]:
        env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
        guard = TaskGuard(memory_mb=self.task_memory_mb)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "daneion.workloads.runner", str(task_dir)],
                cwd=task_dir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, **guard.popen_kwargs())
            guard.attach(proc)
            with self._lock:
                self._running[attempt_id].proc = proc
                self._running[attempt_id].guard = guard
                stopped = self._running[attempt_id].stopped
            if stopped:
                guard.kill()
            try:
                _, err = proc.communicate(timeout=self.task_timeout_s)
            except subprocess.TimeoutExpired:
                guard.kill()
                proc.communicate()
                return None, ""
            return proc.returncode, err.decode(errors="replace")
        finally:
            guard.close()
