"""One-line, path-free error text for the desk.

Exceptions from pydantic, the filesystem, FFmpeg or a provider can carry absolute paths,
tracebacks and documentation links. People need the cause in one sentence; the full
detail still goes to the terminal that runs the desk.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from screenwriter_studio.paths import WORKSPACE_ROOT

_MAX = 300
_ABSOLUTE = re.compile(r"(?:/(?:Users|home|var|private|tmp|opt|Volumes)/|[A-Za-z]:\\)[^\s'\"]*")


def friendly(error: object, secret: str = "") -> str:
    text = str(error or "").strip()
    if secret and len(secret) >= 8:
        text = text.replace(secret, "[redacted]")
    if isinstance(error, FileNotFoundError) and getattr(error, "filename", None):
        return f"A required file is missing: {Path(str(error.filename)).name}."
    if isinstance(error, PermissionError) and getattr(error, "filename", None):
        return f"The studio cannot read or write {Path(str(error.filename)).name}. Check the folder permissions."
    if "validation error" in text and ("pydantic" in text or "errors.pydantic.dev" in text):
        first = text.splitlines()[0]
        return f"A saved file or a model reply is not in the expected format ({first})."
    for root in (str(WORKSPACE_ROOT), str(Path(tempfile.gettempdir()).resolve()), tempfile.gettempdir(), str(Path.home())):
        if root and root != "/":
            text = text.replace(root + "/", "").replace(root, "")
    text = _ABSOLUTE.sub(lambda match: Path(match.group(0)).name or "a local file", text)
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("Traceback")]
    text = lines[0] if lines else "Something went wrong. The terminal running the desk has the details."
    return text if len(text) <= _MAX else text[: _MAX - 1].rstrip() + "…"
