"""Per-task process guard (ADR-0014 Tier-0 controls, EXEC-05).

Windows: each task process is put in its own Job Object with a memory cap, a process limit
(launcher + interpreter only: the task cannot start a shell or any other program) and
kill-on-close, so cancelling or crashing never leaves orphans. Elsewhere: an address-space
rlimit and a new session, killed as a group. This is resource control, not a security
sandbox: built-in handlers only, run as the normal user (stated honestly in the ADR).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

log = logging.getLogger("daneion.worker.sandbox")

EXIT_RESOURCE = 3  # runner exit code when the task hit its memory cap


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _EXTENDED_LIMIT_INFO = 9
    _LIMIT_ACTIVE_PROCESS = 0x0008
    _LIMIT_PROCESS_MEMORY = 0x0100
    _LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
    _LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.c_void_p]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


class TaskGuard:
    """Create before spawning, `attach()` right after Popen, `close()` when done."""

    # 2 = the venv launcher (uv's python.exe is a trampoline) + the real interpreter.
    # The task itself still cannot start a shell or any other program.
    def __init__(self, memory_mb: int = 2048, max_processes: int = 2) -> None:
        self.memory_bytes = memory_mb * 1024 * 1024
        self.max_processes = max_processes
        self.enforced: list[str] = []
        self._job: Any = None
        self._proc: subprocess.Popen[bytes] | None = None
        if sys.platform == "win32":
            self._create_job()

    # ------------------------------------------------------------ spawn helpers
    def popen_kwargs(self) -> dict[str, Any]:
        if sys.platform == "win32":
            return {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP
                                      | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                                      | subprocess.CREATE_NO_WINDOW)}
        limit = self.memory_bytes

        def _limits() -> None:  # runs in the child before exec
            import os
            import resource

            os.nice(5)
            try:
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except (ValueError, OSError):
                pass  # macOS often refuses RLIMIT_AS; reported as advisory below

        self.enforced = ["process group kill", "lower priority",
                         "memory cap (rlimit, advisory on macOS)"]
        return {"start_new_session": True, "preexec_fn": _limits}

    def attach(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        if self._job and not _k32.AssignProcessToJobObject(self._job, int(proc._handle)):  # type: ignore[attr-defined]
            log.warning("could not assign task to Job Object (error %s); limits not enforced",
                        ctypes.get_last_error())
            self.enforced = ["lower priority"]

    # ------------------------------------------------------------ windows job
    def _create_job(self) -> None:
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            log.warning("CreateJobObject failed (error %s)", ctypes.get_last_error())
            return
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = (
            _LIMIT_KILL_ON_JOB_CLOSE | _LIMIT_PROCESS_MEMORY | _LIMIT_ACTIVE_PROCESS
            | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
        info.BasicLimitInformation.ActiveProcessLimit = self.max_processes
        info.ProcessMemoryLimit = self.memory_bytes
        if not _k32.SetInformationJobObject(job, _EXTENDED_LIMIT_INFO, ctypes.byref(info),
                                            ctypes.sizeof(info)):
            log.warning("SetInformationJobObject failed (error %s)", ctypes.get_last_error())
            _k32.CloseHandle(job)
            return
        self._job = job
        self.enforced = ["Job Object kill-tree", f"memory cap {self.memory_bytes >> 20} MB",
                         f"max {self.max_processes} processes", "below-normal priority"]

    def peak_memory_mb(self) -> float | None:
        if not self._job:
            return None
        info = _ExtendedLimits()
        ok = _k32.QueryInformationJobObject(self._job, _EXTENDED_LIMIT_INFO, ctypes.byref(info),
                                            ctypes.sizeof(info), None)
        return round(info.PeakProcessMemoryUsed / 2**20, 1) if ok else None

    # ------------------------------------------------------------ teardown
    def kill(self) -> None:
        if self._job:
            _k32.TerminateJobObject(self._job, 1)
        if self._proc is not None and self._proc.poll() is None:
            _kill_tree(self._proc)

    def close(self) -> None:
        if self._job:
            _k32.CloseHandle(self._job)  # KILL_ON_JOB_CLOSE: anything left dies here
            self._job = None


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    import psutil

    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except psutil.NoSuchProcess:
        pass
