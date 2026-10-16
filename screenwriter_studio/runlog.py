"""Per-production run log: one JSON line per crew call, so spend and failures are visible.

`runs.jsonl` sits in the production folder. Each line says which role ran, on which model
and effort, how many tokens it used, how long it took, and how it ended. Nothing in it is
read back by the workflow; it exists for people (and `python -m screenwriter_studio --usage`).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

FILE = "runs.jsonl"
log = logging.getLogger("screenwriter_studio")
_lock = threading.Lock()


class _Redact(logging.Filter):
    """Keeps the API key out of every log line, whichever module wrote it."""

    def filter(self, record: logging.LogRecord) -> bool:
        key = os.environ.get("KIE_API_KEY", "")
        if len(key) >= 8:
            record.msg = str(record.getMessage()).replace(key, "[redacted]")
            record.args = ()
            if record.exc_info:
                record.exc_text = logging.Formatter().formatException(record.exc_info).replace(key, "[redacted]")
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Terminal logging for the CLI and the desk. Safe to call twice."""
    if any(isinstance(item, _Redact) for handler in log.handlers for item in handler.filters):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    handler.addFilter(_Redact())
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False


def append(folder: Path | None, entry: dict) -> None:
    """Add one line. A log that cannot be written never fails the production."""
    if folder is None:
        return
    line = json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}, ensure_ascii=False)
    try:
        with _lock, (Path(folder) / FILE).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        log.warning("Could not write %s in %s", FILE, folder)


def usage_of(result) -> dict:
    """Token counts from an Agents SDK result; empty when the result carries none."""
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    counts = {}
    for key in ("requests", "input_tokens", "output_tokens", "total_tokens"):
        try:
            counts[key] = int(getattr(usage, key))
        except (TypeError, ValueError):
            continue
    return counts


def read(folder: Path) -> list[dict]:
    try:
        lines = (Path(folder) / FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def summary(folder: Path) -> dict:
    """Totals per role: calls, retries, failures, tokens and seconds."""
    roles: dict[str, dict] = {}
    for entry in read(folder):
        role = str(entry.get("role") or "?")
        row = roles.setdefault(role, {"calls": 0, "retries": 0, "failures": 0, "input_tokens": 0,
                                      "output_tokens": 0, "seconds": 0.0, "models": []})
        row["calls"] += 1
        row["retries"] += 1 if entry.get("attempt", 1) > 1 else 0
        row["failures"] += 1 if entry.get("outcome") != "ok" else 0
        row["input_tokens"] += int(entry.get("input_tokens") or 0)
        row["output_tokens"] += int(entry.get("output_tokens") or 0)
        row["seconds"] = round(row["seconds"] + float(entry.get("seconds") or 0), 2)
        if entry.get("model") and entry["model"] not in row["models"]:
            row["models"].append(entry["model"])
    return roles
