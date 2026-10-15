"""`daneion` command line: run a controller, run a worker, submit jobs."""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

from daneion import __version__
from daneion.config import ControllerConfig, default_data_dir


def _local_worker_token(ctl: object, cfg: ControllerConfig, slots: int) -> str:
    """Reuse the controller's own worker identity across restarts so it never eats a node slot."""
    from daneion.controller import errors
    from daneion.controller.service import Controller
    from daneion.worker.probe import os_name, probe

    assert isinstance(ctl, Controller)
    path = cfg.data_dir / "local_worker.token"
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        try:
            ctl.authenticate(token)
            return token
        except errors.Unauthorized:
            pass
    _, token = ctl.register_worker(f"{socket.gethostname()} (controller)", is_local=True,
                                   os_name=os_name(), capabilities=probe(), slots=slots)
    path.write_text(token, encoding="utf-8")
    return token


def cmd_controller(args: argparse.Namespace) -> int:
    import uvicorn

    from daneion.controller.api import create_app
    from daneion.controller.service import Controller
    from daneion.worker.agent import WorkerAgent
    from daneion.worker.probe import default_slots

    cfg = ControllerConfig(data_dir=Path(args.data_dir), host=args.host, port=args.port,
                           local_worker=not args.no_local_worker)
    ctl = Controller(cfg)
    ctl.start_background()
    console = Path(args.console_dir) if args.console_dir else _find_console()
    app = create_app(ctl, console)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning",
                                           timeout_keep_alive=60))
    agent = None
    if cfg.local_worker:
        slots = args.slots or default_slots()
        token = _local_worker_token(ctl, cfg, slots)
        agent = WorkerAgent(f"http://127.0.0.1:{cfg.port}", token, slots=slots,
                            work_dir=cfg.data_dir / "local-worker")
        threading.Thread(target=agent.run, name="local-worker", daemon=True).start()
    print(f"Daneion controller {__version__} on http://{cfg.host}:{cfg.port}"
          f"  (data: {cfg.data_dir})")
    if console:
        print(f"Console: http://127.0.0.1:{cfg.port}/")
    try:
        server.run()
    finally:
        if agent:
            agent.stop()
        ctl.stop_background()
    return 0


def _find_console() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        dist = parent / "apps" / "console" / "dist"
        if (dist / "index.html").exists():
            return dist
    return None


def cmd_worker(args: argparse.Namespace) -> int:
    from daneion.worker.agent import WorkerAgent

    agent = WorkerAgent(args.controller, args.token, name=args.name, slots=args.slots)
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop()
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    from daneion.workloads import get_workload

    params = dict(get_workload(args.handler).presets.get(args.preset, {}))
    params.update(json.loads(args.params) if args.params else {})
    with httpx.Client(base_url=args.controller, timeout=30) as http:
        r = http.post("/api/v1/jobs", json={"handler": args.handler, "params": params,
                                            "mode": args.mode})
        if r.status_code >= 400:
            print(r.text, file=sys.stderr)
            return 1
        job = r.json()
        reason = job["fitcheck"]["reason"] if job["fitcheck"] else ""
        print(f"{job['id']}  {job['state']}  placement={job['placement']}  "
              f"tasks={job['task_count']}  ({reason})")
        if not args.wait:
            return 0
        while True:
            job = http.get(f"/api/v1/jobs/{job['id']}").json()
            done = job["task_states"].get("SUCCEEDED", 0)
            print(f"\r{job['state']:<10} {done}/{job['task_count']} tiles  "
                  f"{(job['wall_s'] or 0):6.1f}s", end="", flush=True)
            if job["state"] in ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"):
                print()
                for w in job.get("per_worker", []):
                    print(f"  {w['name']:<32} {w['tasks']:>4} tiles  busy {w['busy_s']:.1f}s")
                if job["reason"]:
                    print(f"  reason: {job['reason']}")
                return 0 if job["state"] == "SUCCEEDED" else 1
            time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="daneion", description="LAN compute sharing")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("controller", help="run the controller (and its local worker)")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8750)
    c.add_argument("--data-dir", default=str(default_data_dir()))
    c.add_argument("--slots", type=int, default=None, help="local worker slots")
    c.add_argument("--no-local-worker", action="store_true")
    c.add_argument("--console-dir", default=None)
    c.set_defaults(fn=cmd_controller)

    w = sub.add_parser("worker", help="run a worker that joins a controller")
    w.add_argument("--controller", required=True, help="e.g. http://192.168.1.10:8750")
    w.add_argument("--token", required=True)
    w.add_argument("--name", default=None)
    w.add_argument("--slots", type=int, default=None)
    w.set_defaults(fn=cmd_worker)

    s = sub.add_parser("submit", help="submit a job")
    s.add_argument("handler", nargs="?", default="fractal_tiles_v1")
    s.add_argument("--controller", default="http://127.0.0.1:8750")
    s.add_argument("--mode", default="auto", choices=["auto", "local", "distributed"])
    s.add_argument("--preset", default="quick", help="named parameter set, e.g. quick or demo")
    s.add_argument("--params", default=None, help="JSON object of workload parameters")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=cmd_submit)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
