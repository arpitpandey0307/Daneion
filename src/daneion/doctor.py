"""`daneion doctor`: the pre-demo checklist (OBS-05). Run it on every laptop before presenting.

Checks this laptop (Python, identical maths, power, disk, network profile) and then, depending on
what it finds, the controller side (ports, console build, firewall, every laptop in the cluster,
optionally a real distributed job) and/or the worker side (pinned TLS link, token, code version).
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil

from daneion.build import build_info

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


class Doctor:
    def __init__(self, data_dir: Path, console_url: str, run_job: bool) -> None:
        self.data_dir = data_dir
        self.console_url = console_url
        self.run_job = run_job
        self.checks: list[Check] = []

    def add(self, status: str, title: str, detail: str = "") -> None:
        self.checks.append(Check(status, title, detail))
        tag = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}[status]
        print(f"{tag} {title}" + (f"\n       {detail}" if detail else ""), flush=True)

    # ------------------------------------------------------------------ this laptop
    def laptop(self) -> None:
        v = sys.version_info
        if (3, 12) <= (v.major, v.minor) < (3, 14):
            self.add(OK, f"Python {platform.python_version()}")
        else:
            self.add(FAIL, f"Python {platform.python_version()} is not supported",
                     "Daneion needs Python 3.12 or 3.13; run it through `uv run`.")
        b = build_info()
        self.add(INFO, f"Daneion {b['version']}, workload code {b['workloads']}",
                 "Every laptop must show the same workload code.")

        from daneion.workloads.fractal import CALIBRATION, REFERENCE_TILE_SHA256, FractalTiles

        t0 = time.perf_counter()
        out = FractalTiles().run(dict(CALIBRATION))
        took = time.perf_counter() - t0
        if hashlib.sha256(out).hexdigest() == REFERENCE_TILE_SHA256:
            self.add(OK, "Reference tile reproduced bit for bit",
                     f"single core: {took * 1000:.0f} ms for the test tile")
        else:
            self.add(FAIL, "This laptop computes a DIFFERENT reference tile",
                     "Its results would not match the others. Do not use it as a worker.")

        launch = _task_launch_time()
        if launch < 2.0:
            self.add(OK, f"Tasks start quickly ({launch:.1f}s for 8 at once)")
        else:
            self.add(WARN, f"Tasks start slowly right now ({launch:.1f}s for 8 at once, "
                     "normally under 1s)",
                     "Something else is busy (antivirus scan, Windows Update). Jobs will be "
                     "slow; wait a few minutes and run doctor again before benchmarking.")

        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if battery is None:
            self.add(INFO, "No battery information")
        elif battery.power_plugged:
            self.add(OK, f"On mains power ({battery.percent:.0f}%)")
        else:
            self.add(WARN, f"On battery ({battery.percent:.0f}%)",
                     "Plug in: laptops throttle the CPU on battery and benchmarks get slower.")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.data_dir).free / 2**30
        self.add(OK if free >= 2 else WARN, f"{free:.0f} GB free in {self.data_dir}")

        from daneion.net import lan_addresses

        ips = lan_addresses()
        if ips:
            self.add(OK, f"LAN address: {', '.join(ips)}")
        else:
            self.add(WARN, "No Wi-Fi/LAN address", "Connect to the demo hotspot or router.")
        profile = _network_profile()
        if profile and "Public" in profile:
            self.add(WARN, f"Network profile: {profile}",
                     "On the controller laptop this blocks workers. Fix: daneion firewall")
        elif profile:
            self.add(OK, f"Network profile: {profile}")

    # ------------------------------------------------------------------ controller
    def controller(self) -> None:
        try:
            health = httpx.get(f"{self.console_url}/api/v1/health", timeout=3).json()
        except httpx.HTTPError:
            health = None
        if health is None:
            busy = [p for p in (8750, 8751) if _port_in_use(p)]
            if busy:
                self.add(FAIL, f"Port(s) {busy} are taken by another program",
                         "Close it, or start the controller with --port/--lan-port.")
            else:
                self.add(INFO, "No controller running here (ports 8750/8751 are free)",
                         "Start it with: uv run daneion controller")
            return
        self.add(OK, f"Controller running at {self.console_url}")
        if health.get("build", {}).get("workloads") != build_info()["workloads"]:
            self.add(WARN, "The running controller uses older code than this checkout",
                     "Restart it so it picks up the latest code.")

        dist = Path(__file__).resolve().parents[2] / "apps" / "console" / "dist" / "index.html"
        if dist.exists():
            self.add(OK, "Dashboard is built", f"Open {self.console_url}/")
        else:
            self.add(WARN, "Dashboard not built",
                     "cd apps/console, then npm install and npm run build; restart the controller.")

        if sys.platform == "win32":
            rule = _powershell("Get-NetFirewallRule -DisplayName 'Daneion*' -ErrorAction "
                               "SilentlyContinue | Where-Object Enabled -eq True | "
                               "Measure-Object | Select-Object -ExpandProperty Count")
            if rule.strip() not in ("", "0"):
                self.add(OK, "Firewall rule for workers present")
            else:
                self.add(WARN, "No Daneion firewall rule",
                         "Other laptops may be blocked. Run `daneion firewall` for the fix.")

        with httpx.Client(base_url=self.console_url, timeout=10) as http:
            workers = [w for w in http.get("/api/v1/workers").json() if w["state"] != "REVOKED"]
            online = [w for w in workers if w["state"] == "ONLINE"]
            status = OK if len(online) >= 2 else WARN
            self.add(status, f"{len(online)} of {len(workers)} laptops online, "
                             f"{sum(w['slots'] for w in online)} CPU slots",
                     "" if len(online) >= 2 else "Add laptops with: daneion pair")
            mine = build_info()["workloads"]
            for w in workers:
                caps = w["capabilities"] or {}
                build = (caps.get("build") or {}).get("workloads")
                net = caps.get("net") or {}
                line = f"{w['name']}: {w['state'].lower()}, {w['slots']} slots"
                if net:
                    line += f", link {net['down_mbps']:.0f} Mbit/s, {net['rtt_ms']:.0f} ms"
                if w["state"] != "ONLINE":
                    self.add(WARN, line, "Not online: check its Wi-Fi and that `daneion worker` "
                                         "is running there.")
                elif build and build != mine:
                    self.add(FAIL, line,
                             "Different workload code: install the same Daneion version there.")
                elif net and (net["down_mbps"] < 20 or net["rtt_ms"] > 40):
                    self.add(WARN, line, "Slow link: move closer to the hotspot or use a router.")
                else:
                    self.add(OK, line)
            if self.run_job:
                self._test_job(http, len(online))

    def _test_job(self, http: httpx.Client, online: int) -> None:
        from daneion.workloads.fractal import PRESETS, REFERENCE_QUICK_PNG_SHA256

        job = http.post("/api/v1/jobs", json={"handler": "fractal_tiles_v1",
                                              "params": PRESETS["quick"],
                                              "mode": "distributed"}).json()
        deadline = time.monotonic() + 120
        while job["state"] not in ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"):
            if time.monotonic() > deadline:
                self.add(FAIL, "Test job did not finish in 2 minutes", job["id"])
                return
            time.sleep(0.5)
            job = http.get(f"/api/v1/jobs/{job['id']}").json()
        who = ", ".join(f"{w['name']} {w['tasks']}" for w in job.get("per_worker", []))
        if job["state"] != "SUCCEEDED":
            self.add(FAIL, f"Test job {job['state'].lower()}", job.get("reason") or "")
        elif job["result_sha256"] != REFERENCE_QUICK_PNG_SHA256:
            self.add(FAIL, "Test job result differs from the reference image",
                     "Some laptop computes different results. Run doctor on each laptop.")
        else:
            used = len(job.get("per_worker", []))
            status = OK if used >= min(online, 2) else WARN
            self.add(status, f"Test job across {used} laptop(s): {job['wall_s']:.1f}s, "
                             "result identical to the reference", f"tiles: {who}")

    # ------------------------------------------------------------------ worker
    def worker(self) -> None:
        d = self.data_dir / "worker"
        creds_path = d / "credentials.json"
        if not creds_path.exists():
            self.add(INFO, "Not paired as a worker",
                     "On a worker laptop: daneion join <ip> <code> --fp <fingerprint>")
            return
        from daneion.tls import pinned_context

        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        url = creds["url"]
        try:
            ctx = pinned_context(d / "controller.crt")
            with httpx.Client(base_url=url, verify=ctx, timeout=5,
                              headers={"Authorization": f"Bearer {creds['token']}"}) as http:
                t0 = time.perf_counter()
                health = http.get("/api/v1/health").json()
                rtt = (time.perf_counter() - t0) * 1000
                auth = http.get("/api/v1/worker/netprobe", params={"size": 1})
        except httpx.ConnectError as exc:
            self.add(FAIL, f"Cannot reach the controller at {url}",
                     f"{exc}. Same Wi-Fi? Controller running? Its IP may have changed: "
                     "pair again with the new address.")
            return
        except httpx.HTTPError as exc:
            self.add(FAIL, f"Controller at {url} did not answer properly", str(exc))
            return
        self.add(OK, f"Controller reachable over pinned TLS ({rtt:.0f} ms)", url)
        if auth.status_code == 401:
            self.add(FAIL, f"This laptop ({creds['name']}) was removed from the cluster",
                     "Pair again: daneion pair on the controller, then daneion join here.")
        else:
            self.add(OK, f"Worker identity valid ({creds['name']})")
        if health.get("build", {}).get("workloads") != build_info()["workloads"]:
            self.add(FAIL, "Controller runs different workload code",
                     "Install the same Daneion version on both laptops "
                     "(same zip, or git pull + uv sync).")
        else:
            self.add(OK, "Same workload code as the controller")

    def run(self, role: str) -> int:
        print(f"Daneion doctor on {socket.gethostname()}\n")
        self.laptop()
        if role in ("auto", "controller"):
            print()
            self.controller()
        if role in ("auto", "worker"):
            print()
            self.worker()
        fails = sum(c.status == FAIL for c in self.checks)
        warns = sum(c.status == WARN for c in self.checks)
        print(f"\n{fails} problem(s), {warns} warning(s)."
              + (" Ready for the demo." if not fails and not warns else ""))
        return 1 if fails else 0


def _task_launch_time(n: int = 8) -> float:
    """Wall time to start n task-like Python processes in parallel, the way the worker does."""
    from daneion.worker.sandbox import TaskGuard

    t0 = time.perf_counter()
    procs = [subprocess.Popen([sys.executable, "-c", "import daneion.workloads"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **TaskGuard(memory_mb=512).popen_kwargs())
             for _ in range(n)]
    for p in procs:
        p.wait()
    return time.perf_counter() - t0


def _port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _powershell(command: str) -> str:
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", command],
                              capture_output=True, text=True, timeout=15, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _network_profile() -> str:
    if sys.platform != "win32":
        return ""
    out = _powershell("Get-NetConnectionProfile | ForEach-Object "
                      "{ $_.InterfaceAlias + ' = ' + $_.NetworkCategory }")
    return "; ".join(line.strip() for line in out.splitlines() if line.strip())


def doctor(args: Any) -> int:
    return Doctor(Path(args.data_dir), args.controller, args.run_job).run(args.role)
