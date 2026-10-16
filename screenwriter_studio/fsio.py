"""Crash-safe writes for the files a production resumes from.

A half-written screenplay, verdict or manifest would make a paid production unreadable, so
every state file goes through a uniquely named temporary file and one atomic rename.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, durable: bool = True) -> None:
    """Write text so a reader sees the old file or the new one, never a partial one.

    durable also flushes the bytes to disk before the rename; skip it for files that are
    rewritten many times a second and can be rebuilt (the desk's reload file).
    """
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value, *, durable: bool = True) -> None:
    atomic_write_text(path, json.dumps(value, indent=2) + "\n", durable=durable)
