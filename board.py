"""Crew board state. The desk and the tests share this; the page does not.

Structured events from studio.py move the crew and images. Activity text
is only for display; wording changes cannot change workflow state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from events import StudioEvent

STATUS_PATH = Path(__file__).resolve().parent / "desk_status.json"

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


def apply_event(board: Board, event: StudioEvent) -> None:
    """Apply semantic events. Display wording never controls workflow state."""
    if event.folder:
        board.folder = event.folder
    board.note = _plain(event.text)
    kind = event.kind
    stages = {
        "development": ("development", "brief-dev", set()),
        "developed": (None, "dev-writer", {"development"}),
        "writing": ("writer", "dev-writer", {"development"}),
        "reviewing": ("editor", "writer-editor", {"writer"}),
        "approved": (None, "editor-art", {"writer", "editor"}),
        "art": ("art", "editor-art", {"development", "writer", "editor"}),
        "stills": (None, "art-images", {"art"}),
    }
    if kind in stages:
        board.active, board.edge, done = stages[kind]
        board.done.update(done)
        board.returning = False
    elif kind in {"rejected", "rewriting"}:
        board.active = "writer" if kind == "rewriting" else "editor"
        board.edge = "editor-writer"
        board.returning = True
        board.done.discard("writer")
        board.done.discard("editor")
    elif kind in {"failed", "complete"}:
        board.active = board.edge = None
        board.returning = False
    elif kind == "pages":
        board.done.add("writer")
    elif kind == "image" and event.image is not None:
        record = dict(event.image)
        record["state"] = (
            "failed" if record.get("error") else
            "running" if record.get("phase") == "start" else
            "planned" if record.get("dry_run") else "done"
        )
        _upsert_image(board, record)
        board.done.add("art")
        board.active = None
        board.edge = "art-images"


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
