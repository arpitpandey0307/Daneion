"""Build the downloadable Windows zip: code + prebuilt dashboard + one-click launchers.

    uv run python scripts/build_release.py            # builds the dashboard, then the zip
    uv run python scripts/build_release.py --skip-console

Output: release/Daneion-<version>-windows.zip and a matching .sha256 file. Only files tracked
by git are packed (plus apps/console/dist), so local data, tokens and specs never ship.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "apps" / "console"

# Tracked files that the installed app needs. Tests and dashboard sources stay out.
INCLUDE_PREFIXES = ("src/", "scripts/")
INCLUDE_FILES = ("pyproject.toml", "uv.lock", ".python-version")
EXCLUDE_FILES = ("scripts/build_release.py",)

LAUNCHERS = {
    "Setup.cmd": "setup.ps1",
    "Start Controller.cmd": "start-controller.ps1",
    "Join Cluster.cmd": "join.ps1",
    "Start Worker.cmd": "start-worker.ps1",
}

README = """\
Daneion {version} for Windows  (build {commit})
=================================================

Share compute between trusted laptops on the same Wi-Fi or hotspot.

1. Setup.cmd             Run once on every laptop. Installs uv and Python 3.12
                         (needs internet the first time), then checks the laptop.
2. Start Controller.cmd  On ONE laptop. Opens the dashboard at http://127.0.0.1:8750/
3. Join Cluster.cmd      On every other laptop. In the dashboard click "Add laptop",
                         copy the command it shows and paste it here.
4. Start Worker.cmd      Later, to reconnect a laptop that already joined.

Network tips
- Use a phone hotspot or your own router; campus Wi-Fi often blocks laptop-to-laptop traffic.
- Set the Wi-Fi network to Private on the controller, or run as admin:
      uv run daneion firewall
- Something wrong? Run:  uv run daneion doctor

Verify the download: compare the SHA-256 of the zip with the one on the download page.
"""


def run(cmd: list[str], cwd: Path) -> str:
    # npm is a .cmd on Windows, so it needs the shell there.
    res = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True,
                         shell=sys.platform == "win32" and cmd[0] == "npm")
    return res.stdout


def build_console() -> None:
    print("Building the dashboard...")
    run(["npm", "ci", "--no-audit", "--no-fund"], CONSOLE)
    run(["npm", "run", "build"], CONSOLE)


def tracked_files() -> list[str]:
    files = run(["git", "ls-files", "-z"], ROOT).split("\0")
    # scripts/*.cmd are replaced by launchers at the zip root.
    return sorted(f for f in files if f and f not in EXCLUDE_FILES and not f.endswith(".cmd") and
                  (f in INCLUDE_FILES or f.startswith(INCLUDE_PREFIXES)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip-console", action="store_true", help="reuse the existing dist build")
    ap.add_argument("--out", default=str(ROOT / "release"))
    args = ap.parse_args()

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    commit = run(["git", "rev-parse", "--short", "HEAD"], ROOT).strip()
    files = tracked_files()
    dirty = run(["git", "status", "--porcelain", "--", *files], ROOT).strip()
    if dirty:
        print(f"warning: uncommitted changes will ship in this zip:\n{dirty}", file=sys.stderr)

    if not args.skip_console:
        build_console()
    dist = CONSOLE / "dist"
    if not (dist / "index.html").exists():
        print("error: apps/console/dist is missing; build the dashboard first", file=sys.stderr)
        return 1

    # Fixed timestamps (the commit time) so the same commit gives the same zip bytes.
    stamp = int(run(["git", "show", "-s", "--format=%ct", "HEAD"], ROOT).strip())
    date_time = datetime.fromtimestamp(stamp).timetuple()[:6]
    top = f"Daneion-{version}"

    def add(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
        info = zipfile.ZipInfo(f"{top}/{name}", date_time)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, data)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{top}-windows.zip"
    entries: dict[str, bytes] = {f: (ROOT / f).read_bytes() for f in files}
    for path in sorted(dist.rglob("*")):
        if path.is_file():
            entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for launcher, script in LAUNCHERS.items():
        entries[launcher] = (
            "@echo off\r\n"
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\{script}"\r\n'
            "pause\r\n").encode()
    entries["README.txt"] = README.format(version=version, commit=commit).replace(
        "\n", "\r\n").encode()

    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in sorted(entries):
            add(zf, name, entries[name])

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (out_dir / f"{zip_path.name}.sha256").write_text(f"{digest}  {zip_path.name}\n")
    size_mb = zip_path.stat().st_size / 1e6
    print(f"{zip_path}  ({len(entries)} files, {size_mb:.1f} MB)\nsha256 {digest}")
    if shutil.which("gh") is None:
        print("note: install the GitHub CLI (gh) to publish it as a release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
