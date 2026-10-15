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


_GATEWAY_CODES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_STATUS = re.compile(r"(?:Error code|HTTP|status)[:\s]*(\d{3})", re.I)


def status_code(error: object) -> int | None:
    """The HTTP status of a provider error, from the SDK attribute or the message text."""
    code = getattr(error, "status_code", None)
    if isinstance(code, int):
        return code
    match = _STATUS.search(str(error or ""))
    return int(match.group(1)) if match else None


def provider_down(error: object) -> bool:
    """KIE (or the Cloudflare gateway in front of it) failed or is overloaded; trying again may work."""
    text = str(error or "")
    return status_code(error) in _GATEWAY_CODES or bool(re.search(r"bad gateway|gateway time-?out|cloudflare", text, re.I))


def provider_message(error: object) -> str:
    code = status_code(error)
    if code == 429:
        return "KIE is rate-limiting requests right now (429). Wait a minute and try again."
    label = f" ({code})" if code else ""
    return (f"KIE's server did not answer{label}. This is on KIE's side, not in your files; "
            "try again in a minute. Nothing was generated.")


def friendly(error: object, secret: str = "") -> str:
    text = str(error or "").strip()
    if secret and len(secret) >= 8:
        text = text.replace(secret, "[redacted]")
    if provider_down(error):
        return provider_message(error)
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
