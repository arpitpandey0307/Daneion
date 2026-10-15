"""Logging: console plus a rotating file per role, with secrets scrubbed (OBS-01, SEC G12)."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRETS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[redacted]"),
    (re.compile(r'("token"\s*:\s*")[^"]+(")'), r"\1[redacted]\2"),
    (re.compile(r"(\bcode=)[A-Z0-9-]{4,}"), r"\1[redacted]"),
]


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = msg
        for pattern, repl in _SECRETS:
            clean = pattern.sub(repl, clean)
        if clean != msg:
            record.msg, record.args = clean, None
        return True


def setup_logging(verbose: bool, log_dir: Path | None = None, role: str = "daneion") -> Path | None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.addFilter(RedactSecrets())
    root.addHandler(console)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if log_dir is None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{role}.log"
    fh = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(RedactSecrets())
    root.addHandler(fh)
    return path
