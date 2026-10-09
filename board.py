"""Crew board state. The desk and the tests share this; the page does not.

Status lines still come from studio.py. apply_chunk is the only place that
turns those lines into who is working, whether the editor sent the draft
back, and which stills have started or finished.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

STATUS_PATH = Path(__file__).resolve().parent / "desk_status.json"

_FOLDER = re.compile(r"Folder: `([^`]+)`")
_STILL_START = re.compile(r"^\*\*Still\*\* (.+?) \((.+?)\) started on (.+?)\.\s*$")
_IMAGE_OK = re.compile(r"^- (.+?) \((.+?)\) — (.+?) — `(.+)`\s*$")
_IMAGE_FAIL = re.compile(r"^- (.+?) \((.*?)\) failed: (.+)\s*$")

_CREW = {"development", "writer", "editor", "art"}


@dataclass
class Board:
    active: str | None = None
    done: set[str] = field(default_factory=set)
    edge: str | None = None
    returning: bool = False
    note: str = "Waiting for a brief."
    images: list[dict] = field(default_factory=list)
    brief: str = ""
    folder: str = ""
    running: bool = False
    revision: int = 0
    log: list[str] = field(default_factory=list)


def scrub(text: str, secret: str) -> str:
    """Drop the API key if a chunk or a traceback ever repeats it."""
    if not text or not secret or len(secret) < 8 or secret not in text:
        return text
    return text.replace(secret, "[redacted]")


def _plain(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line.replace("**", "").replace("`", "").strip()


def _upsert_image(board: Board, image: dict) -> None:
    for index, current in enumerate(board.images):
        if current["id"] == image["id"]:
            board.images[index] = {**current, **image}
            return
    board.images.append(image)


def apply_chunk(board: Board, chunk: str) -> None:
    """Move the crew from one real stream chunk. Idle until the pipeline speaks."""
    found = _FOLDER.search(chunk)
    if found:
        board.folder = found.group(1)
    if "KIE_API_KEY is not set" in chunk:
        board.active = None
        board.edge = None
        board.note = "KIE_API_KEY is not set."
        return
    if "**Development** is locking" in chunk:
        board.active = "development"
        board.edge = "brief-dev"
        board.returning = False
        board.note = "Development is locking the logline and the beats."
    elif "**Screenwriter** is writing" in chunk:
        board.done.add("development")
        board.active = "writer"
        board.edge = "dev-writer"
        board.returning = False
        board.note = "Screenwriter is writing the pages."
    elif "**Screenwriter** is rewriting" in chunk:
        board.active = "writer"
        board.edge = "editor-writer"
        board.returning = True
        board.note = "Screenwriter is rewriting from the editor's notes."
    elif "**Script editor** is reading" in chunk:
        board.done.add("writer")
        board.active = "editor"
        board.edge = "writer-editor"
        board.returning = False
        board.note = _plain(chunk)
    elif "The editor passed the draft." in chunk:
        board.done.update({"writer", "editor"})
        board.active = None
        board.edge = "editor-art"
        board.returning = False
        board.note = "The editor passed the draft."
    elif "The editor sent it back:" in chunk:
        board.returning = True
        board.edge = "editor-writer"
        board.active = "editor"
        board.note = "The editor sent the draft back."
    elif "Rewrite limit reached." in chunk:
        board.done.update({"writer", "editor"})
        board.active = None
        board.edge = None
        board.returning = False
        board.note = "Rewrite limit reached. The last draft is the one on disk."
    elif chunk.startswith("Pages:"):
        board.done.add("writer")
        board.note = _plain(chunk)
    elif "Stills were not generated." in chunk:
        board.active = None
        board.edge = None
        board.returning = False
        board.note = "The editor did not pass the draft. Stills were not generated."
    elif "Pages only." in chunk:
        board.active = None
        board.edge = None
        board.note = "Pages only. Stills were not requested."
    elif "**Art director**" in chunk:
        board.done.update({"development", "writer", "editor"})
        board.active = "art"
        board.edge = "editor-art"
        board.returning = False
        board.note = "Art director is writing the still jobs."
    elif "**Stills** is calling" in chunk:
        board.done.add("art")
        board.active = None
        board.edge = "art-images"
        board.note = "Stills is calling KIE.ai."
    elif "**Stills stopped:**" in chunk:
        board.active = None
        board.edge = None
        board.note = _plain(chunk)
    elif chunk.startswith("Stills are in") or chunk.startswith("Dry run:"):
        board.active = None
        board.edge = None
        board.note = _plain(chunk)
    elif "Folder:" in chunk:
        board.done.add("development")
        board.active = None
        board.edge = "dev-writer"
        board.note = _plain(chunk)

    for line in chunk.splitlines():
        stripped = line.strip()
        started = _STILL_START.match(stripped)
        if started:
            _upsert_image(
                board,
                {
                    "id": started.group(1),
                    "kind": started.group(2),
                    "model": started.group(3),
                    "path": "",
                    "error": "",
                    "state": "running",
                },
            )
            board.done.add("art")
            board.active = None
            board.edge = "art-images"
            board.note = f"{started.group(1)} · {started.group(2)} · {started.group(3)}"
            continue
        ok = _IMAGE_OK.match(stripped)
        if ok:
            _upsert_image(
                board,
                {
                    "id": ok.group(1),
                    "kind": ok.group(2),
                    "model": ok.group(3),
                    "path": ok.group(4),
                    "error": "",
                    "state": "done",
                },
            )
            board.done.add("art")
            board.edge = "art-images"
            board.note = f"{ok.group(1)} · {ok.group(3)}"
            continue
        failed = _IMAGE_FAIL.match(stripped)
        if failed:
            _upsert_image(
                board,
                {
                    "id": failed.group(1),
                    "kind": failed.group(2),
                    "model": "",
                    "path": "",
                    "error": failed.group(3),
                    "state": "failed",
                },
            )
            board.note = f"{failed.group(1)} failed"


def token_station(board: Board) -> str:
    """Where the work piece should sit after this chunk.

    brief-dev with nobody active yet keeps the piece on the brief, so the
    next development line is a visible move rather than a jump.
    """
    if board.returning or board.edge == "editor-writer":
        return "writer"
    if board.edge == "art-images":
        return "images"
    if board.active in _CREW:
        return board.active
    if board.edge == "editor-art":
        return "art"
    if board.edge == "dev-writer":
        return "writer"
    if board.edge == "brief-dev":
        return "brief"
    if "art" in board.done and board.images:
        return "images"
    if "art" in board.done:
        return "art"
    if "editor" in board.done:
        return "editor"
    if "writer" in board.done:
        return "writer"
    if "development" in board.done:
        return "development"
    return "brief"


def token_glyph(board: Board) -> str:
    """Sheet, page, shot card, or frame, matching what is traveling."""
    if board.returning:
        return "page"
    station = token_station(board)
    if station == "images":
        return "frame"
    if station == "art":
        return "shot"
    if station in {"writer", "editor"}:
        return "page"
    return "sheet"


def client_payload(board: Board, secret: str = "") -> dict:
    """JSON the page is allowed to see. Paths stay server-side. The key never leaves."""
    images = []
    for image in board.images:
        images.append(
            {
                "id": scrub(str(image.get("id") or ""), secret),
                "kind": scrub(str(image.get("kind") or ""), secret),
                "model": scrub(str(image.get("model") or ""), secret),
                "error": scrub(str(image.get("error") or ""), secret),
                "state": image.get("state") or "",
            }
        )
    return {
        "revision": board.revision,
        "running": board.running,
        "brief": scrub(board.brief, secret),
        "folder": scrub(board.folder, secret),
        "active": board.active,
        "done": sorted(board.done),
        "edge": board.edge,
        "returning": board.returning,
        "note": scrub(board.note, secret),
        "log": [scrub(line, secret) for line in board.log],
        "images": images,
        "station": token_station(board),
        "glyph": token_glyph(board),
    }


def load_status() -> dict | None:
    if not STATUS_PATH.is_file():
        return None
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def board_from(data: dict) -> Board:
    board = Board()
    board.active = data.get("active")
    board.done = set(data.get("done") or [])
    board.edge = data.get("edge")
    board.returning = bool(data.get("returning"))
    board.note = data.get("note") or "Waiting for a brief."
    board.images = [dict(image) for image in (data.get("images") or [])]
    board.brief = data.get("brief") or ""
    board.folder = data.get("folder") or ""
    board.running = bool(data.get("running"))
    board.revision = int(data.get("revision") or 0)
    board.log = [str(line) for line in (data.get("log") or [])]
    return board


def save_status(board: Board, secret: str = "") -> None:
    """Write the reload file. Image paths stay so a refresh can still show them."""
    board.revision += 1
    images = []
    for image in board.images:
        stored = dict(image)
        stored["error"] = scrub(str(stored.get("error") or ""), secret)
        images.append(stored)
    payload = {
        "revision": board.revision,
        "running": board.running,
        "brief": scrub(board.brief, secret),
        "folder": board.folder,
        "active": board.active,
        "done": sorted(board.done),
        "edge": board.edge,
        "returning": board.returning,
        "note": scrub(board.note, secret),
        "log": [scrub(line, secret) for line in board.log],
        "images": images,
    }
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATUS_PATH)
