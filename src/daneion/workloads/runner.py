"""Child-process entry point: `python -m daneion.workloads.runner <task_dir>`.

Reads task.json from the private task directory, writes output.bin atomically.
Exit codes: 0 ok, 2 invalid task (never retry), 1 any other error (unknown => not retried).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID = 2


def main(argv: list[str]) -> int:
    from daneion.workloads import WorkloadError, get_workload

    if len(argv) != 1:
        print("usage: python -m daneion.workloads.runner <task_dir>", file=sys.stderr)
        return EXIT_INVALID
    task_dir = Path(argv[0])
    try:
        spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        workload = get_workload(spec["handler"])
        output = workload.run(spec["params"])
        workload.verify(spec["params"], output)
    except (WorkloadError, KeyError, json.JSONDecodeError) as exc:
        print(f"invalid task: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except Exception:
        traceback.print_exc(limit=5)
        return EXIT_ERROR
    tmp = task_dir / "output.bin.tmp"
    tmp.write_bytes(output)
    os.replace(tmp, task_dir / "output.bin")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
