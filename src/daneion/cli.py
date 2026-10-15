"""`daneion` command line: run a controller, pair and run workers, submit jobs."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from daneion import __version__
from daneion.config import ControllerConfig, default_data_dir

CONSOLE_URL = "http://127.0.0.1:8750"


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


def _serve_in_thread(server: Any, name: str) -> threading.Thread:
    def run() -> None:
        try:
            server.run()
        except SystemExit:  # uvicorn exits this way when it cannot bind; caller reports it
            pass

    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


def cmd_controller(args: argparse.Namespace) -> int:
    import uvicorn

    from daneion.controller.api import create_app
    from daneion.controller.service import Controller
    from daneion.net import lan_addresses
    from daneion.tls import FINGERPRINT_CHARS, ensure_controller_cert
    from daneion.worker.agent import WorkerAgent
    from daneion.worker.probe import default_slots

    cfg = ControllerConfig(data_dir=Path(args.data_dir), port=args.port,
                           lan_host=None if args.no_lan else args.lan_host,
                           lan_port=args.lan_port, local_worker=not args.no_local_worker)
    ctl = Controller(cfg)
    ctl.start_background()

    lan_info: dict[str, Any] | None = None
    lan_server = None
    if cfg.lan_host:
        cert, key, fp = ensure_controller_cert(cfg.tls_dir)
        addresses = lan_addresses() if cfg.lan_host == "0.0.0.0" else [cfg.lan_host]
        lan_info = {"fingerprint": fp[:FINGERPRINT_CHARS], "port": cfg.lan_port,
                    "addresses": addresses}
        lan_server = uvicorn.Server(uvicorn.Config(
            create_app(ctl, role="lan"), host=cfg.lan_host, port=cfg.lan_port,
            ssl_certfile=str(cert), ssl_keyfile=str(key), log_level="warning",
            timeout_keep_alive=60))
        lan_thread = _serve_in_thread(lan_server, "lan-listener")
        for _ in range(100):  # uvicorn exits its thread quietly when the port is taken
            if lan_server.started or not lan_thread.is_alive():
                break
            time.sleep(0.05)
        if not lan_server.started:
            print(f"ERROR: cannot listen for workers on {cfg.lan_host}:{cfg.lan_port} "
                  "(port in use? another controller running?). Use --lan-port or --no-lan.",
                  file=sys.stderr)
            ctl.stop_background()
            return 2

    console = Path(args.console_dir) if args.console_dir else _find_console()
    server = uvicorn.Server(uvicorn.Config(create_app(ctl, console, lan_info=lan_info),
                                           host="127.0.0.1", port=cfg.port, log_level="warning",
                                           timeout_keep_alive=60))
    agent = None
    if cfg.local_worker:
        slots = args.slots or default_slots()
        token = _local_worker_token(ctl, cfg, slots)
        agent = WorkerAgent(f"http://127.0.0.1:{cfg.port}", token, slots=slots,
                            work_dir=cfg.data_dir / "local-worker")
        threading.Thread(target=agent.run, name="local-worker", daemon=True).start()

    print(f"Daneion controller {__version__}  (data: {cfg.data_dir})")
    print(f"  Console (this laptop only): http://127.0.0.1:{cfg.port}/")
    if lan_info:
        ips = ", ".join(lan_info["addresses"]) or "no LAN address found - check Wi-Fi"
        print(f"  Workers join over TLS on:   {ips} port {cfg.lan_port}")
        print(f"  Certificate fingerprint:    {lan_info['fingerprint']}")
        print("  To add a laptop:            daneion pair")
        print("  Other laptops can't connect? daneion firewall")
    try:
        server.run()
    finally:
        if agent:
            agent.stop()
        if lan_server:
            lan_server.should_exit = True
        ctl.stop_background()
    return 0


def _find_console() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        dist = parent / "apps" / "console" / "dist"
        if (dist / "index.html").exists():
            return dist
    return None


def cmd_pair(args: argparse.Namespace) -> int:
    with httpx.Client(base_url=args.controller, timeout=10) as http:
        r = http.post("/api/v1/pairing")
    if r.status_code >= 400:
        print(r.text, file=sys.stderr)
        return 1
    info = r.json()
    print(f"Pairing code {info['code']}  (valid {info['ttl_s']:.0f}s, single use)")
    print("On the other laptop, in the Daneion folder, run:\n")
    print(f"  uv run {info['command']}\n")
    if len(info["addresses"]) > 1:
        print(f"If that address does not work, try: {', '.join(info['addresses'][1:])}")
    return 0


# ---------------------------------------------------------------------- worker side
def _creds_dir(args: argparse.Namespace) -> Path:
    return Path(args.data_dir) / "worker"


def cmd_join(args: argparse.Namespace) -> int:
    from daneion.tls import fetch_server_cert, fingerprint, pinned_context
    from daneion.worker.probe import default_slots, os_name, probe

    try:
        pem = fetch_server_cert(args.host, args.port)
    except OSError as exc:
        print(f"Cannot reach {args.host}:{args.port}: {exc}\n"
              "Check: same Wi-Fi/hotspot, controller running, Windows firewall allows port "
              f"{args.port} (network profile = Private).", file=sys.stderr)
        return 2
    fp = fingerprint(pem)
    expected = args.fp.lower().replace(":", "")
    if len(expected) < 8 or not fp.startswith(expected):
        print(f"Certificate fingerprint mismatch: controller shows {expected}, got {fp[:16]}.\n"
              "Refusing to join (wrong address or someone in the middle).", file=sys.stderr)
        return 3
    d = _creds_dir(args)
    d.mkdir(parents=True, exist_ok=True)
    cert_path = d / "controller.crt"
    cert_path.write_text(pem, encoding="ascii")
    url = f"https://{args.host}:{args.port}"
    slots = args.slots or default_slots()
    name = args.name or socket.gethostname()
    with httpx.Client(base_url=url, verify=pinned_context(cert_path), timeout=15) as http:
        r = http.post("/api/v1/worker/pair", json={
            "code": args.code, "name": name, "os": os_name(), "slots": slots,
            "capabilities": probe()})
    if r.status_code >= 400:
        print(f"Pairing refused: {r.json().get('error', {}).get('message', r.text)}",
              file=sys.stderr)
        return 4
    body = r.json()
    (d / "credentials.json").write_text(json.dumps({
        "url": url, "token": body["token"], "worker_id": body["worker_id"], "name": name,
        "fingerprint": fp, "slots": slots}, indent=2), encoding="utf-8")
    print(f"Joined as {name} ({slots} slots). Credentials saved in {d}.")
    if args.no_run:
        return 0
    args.controller = None
    return cmd_worker(args)


def cmd_worker(args: argparse.Namespace) -> int:
    from daneion.tls import pinned_context
    from daneion.worker.agent import WorkerAgent

    d = _creds_dir(args)
    creds_path = d / "credentials.json"
    if not creds_path.exists():
        print("This laptop is not paired yet. Run `daneion pair` on the controller, then "
              "`daneion join <ip> <code> --fp <fingerprint>` here.", file=sys.stderr)
        return 2
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    url = getattr(args, "controller", None) or creds["url"]
    agent = WorkerAgent(url, creds["token"], name=creds["name"],
                        slots=args.slots or creds.get("slots"),
                        task_memory_mb=getattr(args, "task_mem_mb", 2048),
                        work_dir=d, verify=pinned_context(d / "controller.crt"))
    print(f"Worker {creds['name']} -> {url}  (Ctrl+C to leave)")
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop()
    return 0


def _job_params(args: argparse.Namespace) -> dict[str, Any]:
    from daneion.workloads import get_workload

    params = dict(get_workload(args.handler).presets.get(args.preset, {}))
    params.update(json.loads(args.params) if args.params else {})
    if getattr(args, "source", None):
        params["source_dir"] = str(Path(args.source).resolve())
    return params


def _print_fitcheck(fc: dict[str, Any] | None) -> None:
    if not fc:
        return
    print(f"FitCheck: {fc['reason']}")
    for line in fc.get("reasons", []):
        print(f"  - {line}")


def cmd_submit(args: argparse.Namespace) -> int:
    params = _job_params(args)
    with httpx.Client(base_url=args.controller, timeout=30) as http:
        r = http.post("/api/v1/jobs", json={"handler": args.handler, "params": params,
                                            "mode": args.mode})
        if r.status_code >= 400:
            print(r.text, file=sys.stderr)
            return 1
        job = r.json()
        print(f"{job['id']}  {job['state']}  placement={job['placement']}  "
              f"tasks={job['task_count']}")
        _print_fitcheck(job["fitcheck"])
        if job["state"] == "REJECTED":
            print(f"Rejected: {job['reason']}", file=sys.stderr)
            return 1
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


def cmd_bench(args: argparse.Namespace) -> int:
    body = {"handler": args.handler, "params": _job_params(args), "runs": args.runs,
            "warmup": args.warmup}
    with httpx.Client(base_url=args.controller, timeout=30) as http:
        r = http.post("/api/v1/benchmarks", json=body)
        if r.status_code >= 400:
            print(r.json()["error"]["message"], file=sys.stderr)
            return 1
        b = r.json()
        print(f"Benchmark {b['id']}: {b['warmup']} warm-up + {b['runs']} measured runs per mode")
        shown = 0
        try:
            while b["state"] == "RUNNING":
                time.sleep(1)
                b = http.get(f"/api/v1/benchmarks/{b['id']}").json()
                for res in b["results"][shown:]:
                    tag = " (warm-up)" if res["warmup"] else ""
                    who = ", ".join(f"{w['name']}:{w['tasks']}" for w in res["workers"])
                    print(f"  {res['mode']:<12} {res['wall_s']:7.2f}s{tag:<11} [{who}]")
                shown = len(b["results"])
        except KeyboardInterrupt:
            http.post(f"/api/v1/benchmarks/{b['id']}/cancel")
            print("cancelled")
            return 1
    if b["state"] != "DONE":
        print(f"Benchmark {b['state'].lower()}: {b['error'] or ''}", file=sys.stderr)
        return 1
    rep = b["report"]
    print("\nProof-of-Speed")
    for mode, m in rep["modes"].items():
        pred = (f"   FitCheck predicted {m['predicted_s']:.1f}s "
                f"({m['prediction_error']:+.0%})") if m.get("predicted_s") else ""
        print(f"  {mode:<12} median {m['median_s']:7.2f}s  (min {m['min_s']:.2f}, "
              f"max {m['max_s']:.2f}, n={m['runs']}){pred}")
    if rep.get("speedup"):
        print(f"  speedup      {rep['speedup']:.2f}x  ({rep['time_saved_s']:+.1f}s saved)")
    same = "yes" if rep["identical_outputs"] else "NO - outputs differ!"
    print(f"  identical output in every run: {same}")
    if args.out:
        Path(args.out).write_text(json.dumps(b, indent=2), encoding="utf-8")
        print(f"  saved {args.out}")
    return 0


def cmd_sample_images(args: argparse.Namespace) -> int:
    from daneion.workloads.images import make_samples

    folder = Path(args.folder)
    t0 = time.perf_counter()
    paths = make_samples(folder, args.count)
    size = sum(p.stat().st_size for p in paths)
    print(f"{len(paths)} photos ({size / 1e6:.0f} MB) in {folder} "
          f"({time.perf_counter() - t0:.1f}s)")
    return 0


def cmd_workers(args: argparse.Namespace) -> int:
    with httpx.Client(base_url=args.controller, timeout=10) as http:
        for action, target in (("revoke", args.revoke), ("pause", args.pause),
                               ("resume", args.resume)):
            if target:
                r = http.post(f"/api/v1/workers/{target}/{action}")
                if r.status_code >= 400:
                    print(r.json()["error"]["message"], file=sys.stderr)
                    return 1
                print(f"{action}d {target}")
        workers = http.get("/api/v1/workers").json()
    for w in workers:
        caps = w["capabilities"]
        gpu = ", ".join(g["name"] for g in caps.get("gpus", [])) or "-"
        print(f"{w['state']:<8} {w['name']:<30} {w['slots']:>3} slots  "
              f"{caps.get('cpu', '?')[:34]:<34} {caps.get('ram_gb', '?')} GB  {gpu}  "
              f"seen {w['last_seen_s']}s ago")
    return 0


FIREWALL_HELP = """\
Windows blocks incoming connections on networks marked "Public" (most Wi-Fi and hotspots).
On the CONTROLLER laptop, open PowerShell as Administrator once and run:

  Get-NetConnectionProfile | Where-Object NetworkCategory -eq Public |
    Set-NetConnectionProfile -NetworkCategory Private
  New-NetFirewallRule -DisplayName "Daneion workers (TCP {port})" -Direction Inbound `
    -Protocol TCP -LocalPort {port} -Action Allow -Profile Private

Workers only make outgoing connections, so worker laptops need no firewall change.
Also avoid campus/guest Wi-Fi: it often blocks laptop-to-laptop traffic (client isolation).
Use a phone hotspot or your own router.
"""


def cmd_firewall(args: argparse.Namespace) -> int:
    print(FIREWALL_HELP.format(port=args.lan_port))
    if sys.platform == "win32":
        import subprocess

        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-NetConnectionProfile | ForEach-Object "
                              "{ $_.InterfaceAlias + ': ' + $_.NetworkCategory }"],
                             capture_output=True, text=True, check=False).stdout.strip()
        if out:
            print("Current network profile(s):\n  " + out.replace("\n", "\n  "))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="daneion", description="LAN compute sharing")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("controller", help="run the controller (and its local worker)")
    c.add_argument("--port", type=int, default=8750, help="console port (localhost only)")
    c.add_argument("--lan-host", default="0.0.0.0", help="interface for workers to connect to")
    c.add_argument("--lan-port", type=int, default=8751)
    c.add_argument("--no-lan", action="store_true", help="single-laptop mode, no worker listener")
    c.add_argument("--data-dir", default=str(default_data_dir()))
    c.add_argument("--slots", type=int, default=None, help="local worker slots")
    c.add_argument("--no-local-worker", action="store_true")
    c.add_argument("--console-dir", default=None)
    c.set_defaults(fn=cmd_controller)

    pa = sub.add_parser("pair", help="issue a one-time code for a new laptop (on the controller)")
    pa.add_argument("--controller", default=CONSOLE_URL)
    pa.set_defaults(fn=cmd_pair)

    j = sub.add_parser("join", help="pair this laptop with a controller, then start working")
    j.add_argument("host", help="controller LAN address shown by `daneion pair`")
    j.add_argument("code", help="pairing code, e.g. K7QM-2XPA")
    j.add_argument("--fp", required=True, help="certificate fingerprint shown by `daneion pair`")
    j.add_argument("--port", type=int, default=8751)
    j.add_argument("--name", default=None)
    j.add_argument("--slots", type=int, default=None)
    j.add_argument("--data-dir", default=str(default_data_dir()))
    j.add_argument("--no-run", action="store_true", help="only pair, do not start the worker")
    j.set_defaults(fn=cmd_join)

    w = sub.add_parser("worker", help="run this laptop's worker (after `daneion join`)")
    w.add_argument("--controller", default=None, help="override the saved controller URL")
    w.add_argument("--slots", type=int, default=None)
    w.add_argument("--task-mem-mb", type=int, default=2048, help="memory cap per task")
    w.add_argument("--data-dir", default=str(default_data_dir()))
    w.set_defaults(fn=cmd_worker)

    s = sub.add_parser("submit", help="submit a job")
    s.add_argument("handler", nargs="?", default="fractal_tiles_v1")
    s.add_argument("--controller", default=CONSOLE_URL)
    s.add_argument("--mode", default="auto", choices=["auto", "local", "distributed"])
    s.add_argument("--preset", default="quick", help="named parameter set, e.g. quick or demo")
    s.add_argument("--params", default=None, help="JSON object of workload parameters")
    s.add_argument("--source", default=None, help="image folder (for image_batch_v1)")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=cmd_submit)

    b = sub.add_parser("bench", help="Proof-of-Speed: repeat a job local vs distributed")
    b.add_argument("handler", nargs="?", default="fractal_tiles_v1")
    b.add_argument("--controller", default=CONSOLE_URL)
    b.add_argument("--preset", default="demo")
    b.add_argument("--params", default=None)
    b.add_argument("--source", default=None, help="image folder (for image_batch_v1)")
    b.add_argument("--runs", type=int, default=5, help="measured runs per mode")
    b.add_argument("--warmup", type=int, default=1, help="unmeasured runs per mode first")
    b.add_argument("--out", default=None, help="save the full report as JSON")
    b.set_defaults(fn=cmd_bench)

    si = sub.add_parser("sample-images", help="create photo-like JPEGs for image_batch_v1")
    si.add_argument("folder")
    si.add_argument("--count", type=int, default=160)
    si.set_defaults(fn=cmd_sample_images)

    ws = sub.add_parser("workers", help="list laptops in the cluster")
    ws.add_argument("--controller", default=CONSOLE_URL)
    ws.add_argument("--revoke", metavar="NAME_OR_ID", help="remove a laptop (frees its slot)")
    ws.add_argument("--pause", metavar="NAME_OR_ID", help="stop giving this laptop new work")
    ws.add_argument("--resume", metavar="NAME_OR_ID")
    ws.set_defaults(fn=cmd_workers)

    dr = sub.add_parser("doctor", help="pre-demo checklist for this laptop and the cluster")
    dr.add_argument("--role", choices=["auto", "controller", "worker"], default="auto")
    dr.add_argument("--controller", default=CONSOLE_URL)
    dr.add_argument("--data-dir", default=str(default_data_dir()))
    dr.add_argument("--run-job", action="store_true",
                    help="also run a quick distributed job and check its result")
    dr.set_defaults(fn=lambda a: __import__("daneion.doctor", fromlist=["doctor"]).doctor(a))

    fw = sub.add_parser("firewall", help="show how to let other laptops reach this controller")
    fw.add_argument("--lan-port", type=int, default=8751)
    fw.set_defaults(fn=cmd_firewall)

    args = p.parse_args(argv)
    from daneion.logs import setup_logging

    role = args.cmd if args.cmd in ("controller", "worker", "join") else None
    data_dir = getattr(args, "data_dir", None)
    setup_logging(args.verbose, Path(data_dir) / "logs" if role and data_dir else None,
                  role or "daneion")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
