"""Worker agent: pulls leases from the controller, runs each task in its own child process,
heartbeats live attempts and uploads verified outputs (ADR-0003, ADR-0010)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from daneion.worker.probe import default_slots, os_name, probe

log = logging.getLogger("daneion.worker")

# Only these variables reach a task process (Tier-0: scrubbed environment).
_ENV_KEEP = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP",
             "PYTHONPATH", "LANG", "LC_ALL", "HOME")


@dataclass
class Running:
    lease: dict[str, Any]
    proc: subprocess.Popen[bytes] | None = None
    stopped: bool = False
    started: float = field(default_factory=time.monotonic)


class StaleLease(Exception):
    pass


class WorkerAgent:
    def __init__(self, base_url: str, token: str, *, name: str | None = None,
                 slots: int | None = None, work_dir: Path | None = None,
                 verify: bool | str | ssl.SSLContext = True, task_timeout_s: float = 900.0,
                 client: httpx.Client | None = None) -> None:
        self.slots = slots or default_slots()
        self.name = name
        self.work_dir = work_dir or Path.home() / ".daneion" / "worker"
        self.task_timeout_s = task_timeout_s
        self.http = client or httpx.Client(
            base_url=base_url, verify=verify,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, read=40.0))
        self.heartbeat_interval_s = 3.0
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

    # ---------------------------------------------------------------- main loop
    def run(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        hello = self._with_retry(lambda: self._post("/api/v1/worker/hello", {
            "name": self.name, "os": os_name(), "slots": self.slots,
            "capabilities": probe()}))
        if hello is None:
            return
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
            proc = r.proc
        if proc and proc.poll() is None:
            _kill_tree(proc)

    def _kill_all(self) -> None:
        with self._lock:
            ids = list(self._running)
        for a in ids:
            self._kill(a)

    # ---------------------------------------------------------------- execution
    def _execute(self, lease: dict[str, Any]) -> None:
        attempt_id, fence = lease["attempt_id"], lease["fence"]
        task_dir = self.work_dir / "tasks" / attempt_id
        try:
            self._post(f"/api/v1/worker/attempts/{attempt_id}/accept", {"fence": fence})
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "task.json").write_text(
                json.dumps({"handler": lease["handler"], "params": lease["params"]}),
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
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                                       | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, "-m", "daneion.workloads.runner", str(task_dir)],
            cwd=task_dir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, **kwargs)
        with self._lock:
            self._running[attempt_id].proc = proc
            if self._running[attempt_id].stopped:
                _kill_tree(proc)
        try:
            _, err = proc.communicate(timeout=self.task_timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            return None, ""
        return proc.returncode, err.decode(errors="replace")


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    import psutil

    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except psutil.NoSuchProcess:
        pass
