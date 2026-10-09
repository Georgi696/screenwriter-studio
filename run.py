"""
Screenwriter studio entry point.

    uv run screenwriter_studio/run.py "a 30-second ad for a dented thermos"
    uv run screenwriter_studio/run.py "idea" --pages-only
    uv run screenwriter_studio/run.py --ui

The desk writes screenwriter_studio/desk_status.json while a run is in
progress. A browser reload reads that file instead of starting from idle.
"""

import argparse
import asyncio
import base64
import html
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv(override=True)

from models import (
    ART_MODEL,
    DEVELOPMENT_MODEL,
    EDIT_MODEL,
    EDITOR_MODEL,
    GPT_IMAGE_MODEL,
    IMAGE_MODEL_BY_KIND,
    NANO_BANANA_MODEL,
    SEEDREAM_MODEL,
    WRITER_MODEL,
)
from studio import ScreenwriterStudio


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the screenwriter crew")
    parser.add_argument("idea", nargs="?", help="The piece to develop")
    parser.add_argument("--pages-only", action="store_true", help="Stop after the script editor")
    parser.add_argument("--dry-run", action="store_true", help="Pick still models and write no images")
    parser.add_argument("--aspect", default=None, help="Lock 16:9, 9:16, or 1:1")
    parser.add_argument("--runtime", type=int, default=None, help="Lock runtime in seconds")
    parser.add_argument("--ui", action="store_true", help="Open the Gradio desk")
    return parser.parse_args()


async def _stream(idea: str, pages_only: bool, dry_run: bool, aspect: str | None, runtime: int | None) -> None:
    studio = ScreenwriterStudio()
    async for chunk in studio.run(
        idea,
        pages_only=pages_only,
        dry_run=dry_run,
        aspect_ratio=aspect,
        runtime_seconds=runtime,
    ):
        print(chunk, end="", flush=True)


STATUS_PATH = Path(__file__).resolve().parent / "desk_status.json"
AGENT_W = 136
EDGE_W = 72
LEAD_W = 64

WRITERS: list[tuple[str, str, str]] = [
    ("development", "Development", DEVELOPMENT_MODEL),
    ("writer", "Screenwriter", WRITER_MODEL),
    ("editor", "Script editor", EDITOR_MODEL),
    ("art", "Art director", ART_MODEL),
]
FORWARD = ("dev-writer", "writer-editor", "editor-art")

_FOLDER = re.compile(r"Folder: `([^`]+)`")
_STILL_START = re.compile(r"^\*\*Still\*\* (.+?) \((.+?)\) started on (.+?)\.\s*$")
_IMAGE_OK = re.compile(r"^- (.+?) \((.+?)\) — (.+?) — `(.+)`\s*$")
_IMAGE_FAIL = re.compile(r"^- (.+?) \((.*?)\) failed: (.+)\s*$")

IDEA_HELP = (
    "This is the brief the crew turns into a logline, Fountain pages, and stills. "
    "Example: a 30-second vertical film about someone who keeps a seat on the last bus."
)
PAGES_HELP = (
    "The script editor still reads the draft and can send it back. "
    "The run stops before any image is generated."
)
DRY_HELP = (
    "Image models are chosen and the still jobs are written, "
    "but no images are downloaded and no image credits are spent."
)

SHEET = """
<svg class="crew-object" width="26" height="32" viewBox="0 0 26 32" role="img" aria-label="Brief sheet">
  <path d="M3 1h12l8 8v21a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1z" fill="#f4f0e6" stroke="#1c1c1c"/>
  <path d="M15 1v8h8" fill="#e4dcc8" stroke="#1c1c1c"/>
  <path d="M6 16h12M6 20h12M6 24h8" stroke="#8a8172" fill="none"/>
</svg>
""".strip()

PAGE = """
<svg class="crew-object" width="24" height="32" viewBox="0 0 24 32" role="img" aria-label="Script page">
  <rect x="2" y="1" width="20" height="30" rx="1" fill="#f7f3ea" stroke="#1c1c1c"/>
  <path d="M6 8h12M6 12h12M6 16h12M6 20h9" stroke="#5c564c" fill="none"/>
</svg>
""".strip()

SHOT = """
<svg class="crew-object" width="36" height="24" viewBox="0 0 36 24" role="img" aria-label="Shot card">
  <rect x="1" y="1" width="34" height="22" rx="2" fill="#d9e4f2" stroke="#1c1c1c"/>
  <rect x="6" y="5" width="16" height="14" fill="#f7f3ea" stroke="#1c1c1c"/>
  <path d="M26 8h6M26 12h6M26 16h4" stroke="#1c1c1c" fill="none"/>
</svg>
""".strip()

FRAME = """
<svg class="crew-object" width="28" height="22" viewBox="0 0 28 22" role="img" aria-label="Image frame">
  <rect x="1" y="1" width="26" height="20" rx="1" fill="#1c1c1c" stroke="#f4f0e6"/>
  <rect x="4" y="4" width="20" height="14" fill="#c9b89a" stroke="#f4f0e6"/>
</svg>
""".strip()

CREW_CSS = """
#crew-board .html-container,
#crew-board .html-container.pending {
  opacity: 1 !important;
  transition: none !important;
}
.library { display: flex; flex-direction: column; gap: 18px; }
.library-project h3 { margin: 0 0 8px; font-size: 14px; }
.library-label { margin: 8px 0 4px; font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; opacity: 0.7; }
.library-file {
  display: block;
  width: 100%;
  margin: 0 0 4px;
  padding: 6px 8px;
  text-align: left;
  border: 1px solid var(--border-color-primary, #666);
  border-radius: 6px;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.library-file:hover, .library-file:focus { border-color: var(--color-accent, #7c5cff); }
.library-empty { margin: 0; font-size: 12px; opacity: 0.7; }
#library-choice { display: none !important; }
.crew-board { display: flex; flex-direction: column; gap: 12px; }
.crew-note { margin: 0; font-weight: 600; }
.crew-folder { margin: 0; font-size: 12px; opacity: 0.75; }
.crew-row, .image-row { display: flex; align-items: stretch; overflow-x: auto; padding: 4px 0 8px; }
.crew-agent, .image-agent {
  box-sizing: border-box;
  border: 1px solid var(--border-color-primary, #666);
  border-radius: 8px;
  padding: 10px;
  background: var(--block-background-fill, transparent);
}
.crew-agent strong, .image-agent strong { display: block; word-break: break-word; }
.crew-model, .crew-status, .image-job { display: block; font-size: 12px; margin-top: 4px; }
.crew-agent.is-active, .image-agent.is-active {
  border-color: var(--color-accent, #7c5cff);
  animation: crew-pulse 1.05s ease-in-out infinite;
}
.crew-agent.is-active .crew-status, .image-agent.is-active .crew-status {
  color: var(--color-accent, #7c5cff);
  font-weight: 600;
}
.crew-agent.is-done, .image-agent.is-done { border-color: var(--body-text-color, currentColor); }
.crew-edge, .brief-lane {
  position: relative;
  align-self: center;
  height: 44px;
}
.crew-line, .image-line {
  position: absolute;
  left: 0;
  right: 0;
  top: 50%;
  height: 2px;
  background: var(--border-color-primary, #666);
}
.image-line { left: 50%; right: auto; top: 0; width: 2px; height: 100%; }
.crew-edge.is-live .crew-line, .brief-lane.is-live .crew-line, .image-edge.is-live .image-line,
.crew-return.is-live .crew-return-track { background: var(--color-accent, #7c5cff); }
.crew-object { position: absolute; z-index: 1; }
.brief-lane .crew-object, .crew-edge .crew-object { top: 4px; left: 0; }
.crew-edge, .brief-lane, .image-edge, .crew-return { overflow: visible; }
.crew-edge.is-live .crew-object, .brief-lane.is-live .crew-object {
  animation: crew-slide 1.8s ease-in-out forwards;
}
.crew-return { position: relative; height: 46px; }
.crew-return-track {
  position: absolute;
  top: 28px;
  height: 2px;
  background: var(--border-color-primary, #666);
}
.crew-return-track .crew-object { top: -30px; left: 0; }
.crew-return.is-live .crew-object { animation: crew-back 1.5s ease-in-out forwards; }
.crew-return-label, .image-stage-label { margin: 0; font-size: 12px; opacity: 0.75; }
.crew-return.is-live .crew-return-label { opacity: 1; font-weight: 600; color: var(--color-accent, #7c5cff); }
.image-col { width: 180px; flex: 0 0 180px; display: flex; flex-direction: column; align-items: stretch; }
.image-edge { position: relative; height: 40px; }
.image-edge .crew-object { left: calc(50% - 14px); top: 0; }
.image-edge.is-live .crew-object { animation: crew-drop 1.3s ease-in-out forwards; }
.still-img { display: block; width: 100%; height: auto; margin-top: 6px; border-radius: 4px; }
.still-thumb { width: 28px; height: 22px; object-fit: cover; border: 1px solid #f4f0e6; border-radius: 2px; background: #c9b89a; }
.option-help { margin: 4px 0 8px; }
@keyframes crew-pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.045); }
}
@keyframes crew-slide {
  from { left: 0; }
  to { left: calc(100% - 6px); }
}
@keyframes crew-back {
  from { left: calc(100% - 28px); }
  to { left: 0; }
}
@keyframes crew-drop {
  from { transform: translateY(0); }
  to { transform: translateY(24px); }
}
@media (prefers-reduced-motion: reduce) {
  .crew-agent.is-active, .image-agent.is-active,
  .crew-edge.is-live .crew-object, .brief-lane.is-live .crew-object,
  .crew-return.is-live .crew-object, .image-edge.is-live .crew-object {
    animation: none;
  }
}
#crew-log { max-height: 180px; overflow: auto; }
"""


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


def _image_agents() -> list[tuple[str, str]]:
    grouped: dict[str, list[str]] = {}
    for kind, model in IMAGE_MODEL_BY_KIND.items():
        grouped.setdefault(model, []).append(kind)
    order = [NANO_BANANA_MODEL, GPT_IMAGE_MODEL, SEEDREAM_MODEL, EDIT_MODEL]
    for model in grouped:
        if model not in order:
            order.append(model)
    return [(model, ", ".join(grouped[model])) for model in order if model in grouped]


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


def _encoded_image(path: str) -> tuple[str, str] | None:
    file = Path(path)
    if not path or not file.is_file() or file.stat().st_size > 1_500_000:
        return None
    raw = file.read_bytes()
    kind = "image/png"
    if raw.startswith(b"\xff\xd8"):
        kind = "image/jpeg"
    elif raw.startswith(b"RIFF"):
        kind = "image/webp"
    return kind, base64.b64encode(raw).decode("ascii")


def _picture(path: str) -> str:
    encoded = _encoded_image(path)
    if encoded is None:
        if path and Path(path).is_file():
            return '<span class="crew-model">Saved on disk</span>'
        return ""
    kind, data = encoded
    return f'<img class="still-img" alt="" src="data:{kind};base64,{data}"/>'


def _frame_object(jobs: list[dict], running: bool) -> str:
    if running:
        return FRAME
    for image in reversed(jobs):
        if image.get("state") != "done":
            continue
        encoded = _encoded_image(image.get("path") or "")
        if encoded is None:
            continue
        kind, data = encoded
        return (
            f'<img class="crew-object still-thumb" alt="Still" width="28" height="22" '
            f'src="data:{kind};base64,{data}"/>'
        )
    return FRAME


def _agent_html(agent_id: str, name: str, model: str, board: Board) -> str:
    classes = ["crew-agent"]
    if board.active == agent_id:
        classes.append("is-active")
        status = "Working"
    elif agent_id in board.done:
        classes.append("is-done")
        status = "Done"
    else:
        status = "Waiting"
    return (
        f'<div class="{" ".join(classes)}" style="flex:0 0 {AGENT_W}px;width:{AGENT_W}px">'
        f"<strong>{html.escape(name)}</strong>"
        f'<span class="crew-model">{html.escape(model)}</span>'
        f'<span class="crew-status">{status}</span>'
        "</div>"
    )


def _lane(live: bool, width: int, obj: str, kind: str) -> str:
    klass = f"{kind} is-live" if live else kind
    return (
        f'<div class="{klass}" style="flex:0 0 {width}px;width:{width}px">'
        f'<div class="crew-line"></div>{obj}</div>'
    )


def _image_column(model: str, kinds: str, board: Board) -> str:
    jobs = [image for image in board.images if image.get("model") == model]
    running = any(image.get("state") == "running" for image in jobs)
    finished = any(image.get("state") == "done" for image in jobs)
    if running:
        status, klass = "Working", "image-agent is-active"
    elif finished:
        status, klass = "Done", "image-agent is-done"
    else:
        status, klass = "Waiting", "image-agent"
    edge = "image-edge is-live" if running else "image-edge"
    token = _frame_object(jobs, running)
    body = [
        f'<div class="image-job">{html.escape(image.get("id", ""))} · {html.escape(image.get("kind", ""))}</div>'
        for image in jobs
        if image.get("id")
    ]
    pictures = [_picture(image.get("path") or "") for image in jobs if image.get("state") == "done"]
    return (
        '<div class="image-col">'
        f'<div class="{edge}"><div class="image-line"></div>{token}</div>'
        f'<div class="{klass}">'
        f"<strong>{html.escape(model)}</strong>"
        f'<span class="crew-model">{html.escape(kinds)}</span>'
        f'<span class="crew-status">{status}</span>'
        f'{"".join(body)}{"".join(pictures)}'
        "</div></div>"
    )


def render_board(board: Board) -> str:
    brief_live = board.edge == "brief-dev" or board.active == "development"
    parts = [_lane(brief_live, LEAD_W, SHEET, "brief-lane")]
    objects = {"writer-editor": PAGE, "editor-art": SHOT}
    for index, (agent_id, name, model) in enumerate(WRITERS):
        if index:
            edge_id = FORWARD[index - 1]
            obj = "" if board.returning and edge_id == "writer-editor" else objects.get(edge_id, "")
            parts.append(_lane(board.edge == edge_id, EDGE_W, obj, "crew-edge"))
        parts.append(_agent_html(agent_id, name, model, board))
    return_margin = LEAD_W + AGENT_W + EDGE_W
    return_width = AGENT_W + EDGE_W + AGENT_W
    return_class = "crew-return is-live" if board.returning else "crew-return"
    return_label = "Script page sent back" if board.returning else "Editor loop, up to two rewrites"
    return_obj = PAGE if board.returning else ""
    columns = "".join(_image_column(model, kinds, board) for model, kinds in _image_agents())
    folder = f'<p class="crew-folder">{html.escape(board.folder)}</p>' if board.folder else ""
    return (
        '<div class="crew-board">'
        f'<p class="crew-note">{html.escape(board.note)}</p>{folder}'
        f'<div class="crew-row">{"".join(parts)}</div>'
        f'<div class="{return_class}">'
        f'<div class="crew-return-track" style="left:{return_margin}px;width:{return_width}px">{return_obj}</div>'
        f'<p class="crew-return-label" style="margin-left:{return_margin}px">{return_label}</p>'
        "</div>"
        '<p class="image-stage-label">Image agents, handed off from the art director</p>'
        f'<div class="image-row">{columns}</div>'
        "</div>"
    )


PRODUCTIONS = Path(__file__).resolve().parent.parent / "productions"
DOCUMENT_SUFFIXES = {".json", ".md", ".fountain", ".txt"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

LIBRARY_JS = """
() => {
  if (window.__libraryBound) return;
  window.__libraryBound = true;
  document.addEventListener("click", (event) => {
    const button = event.target.closest("#library [data-path]");
    if (!button) return;
    event.preventDefault();
    const box = document.querySelector("#library-choice textarea, #library-choice input");
    if (!box) return;
    const path = button.getAttribute("data-path") || "";
    const proto = box.tagName === "TEXTAREA" ? window.HTMLTextAreaElement : window.HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(proto.prototype, "value");
    if (setter && setter.set) setter.set.call(box, path);
    else box.value = path;
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
"""


def library_catalog() -> tuple[list[dict], str]:
    """Documents and images under productions/<slug>/, plus a change signature."""
    root = PRODUCTIONS.resolve()
    projects: list[dict] = []
    signature: list[str] = []
    if not root.is_dir():
        return projects, ""
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        documents: list[dict] = []
        images: list[dict] = []
        for file in sorted(folder.rglob("*")):
            if not file.is_file() or file.name.startswith("."):
                continue
            resolved = file.resolve()
            if not resolved.is_relative_to(root):
                continue
            suffix = resolved.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                bucket = images
            elif suffix in DOCUMENT_SUFFIXES:
                bucket = documents
            else:
                continue
            relative = resolved.relative_to(root).as_posix()
            stat = resolved.stat()
            bucket.append({"path": relative, "name": resolved.relative_to(folder.resolve()).as_posix()})
            signature.append(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}")
        projects.append({"slug": folder.name, "documents": documents, "images": images})
    return projects, "|".join(signature)


def render_library(projects: list[dict]) -> str:
    if not projects:
        return '<nav class="library"><p class="library-empty">No productions yet.</p></nav>'
    sections: list[str] = []
    for project in projects:
        parts = [f'<section class="library-project"><h3>{html.escape(project["slug"])}</h3>']
        for label, items in (("Documents", project["documents"]), ("Images", project["images"])):
            parts.append(f'<p class="library-label">{label}</p>')
            if not items:
                parts.append('<p class="library-empty">None yet.</p>')
                continue
            parts.extend(
                f'<button type="button" class="library-file" data-path="{html.escape(item["path"], quote=True)}">'
                f'{html.escape(item["name"])}</button>'
                for item in items
            )
        parts.append("</section>")
        sections.append("".join(parts))
    return f'<nav class="library">{"".join(sections)}</nav>'


def library_file(path: str) -> Path | None:
    raw = (path or "").strip()
    if not raw or raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
        return None
    root = PRODUCTIONS.resolve()
    file = (root / raw).resolve()
    if not file.is_file() or not file.is_relative_to(root):
        return None
    if file.suffix.lower() not in DOCUMENT_SUFFIXES | IMAGE_SUFFIXES:
        return None
    return file


def library_text(file: Path) -> str:
    text = file.read_text(encoding="utf-8", errors="replace")
    if file.suffix.lower() == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2)
        except json.JSONDecodeError:
            pass
    if len(text) > 200_000:
        text = text[:200_000] + "\n\n… truncated"
    return text


def gallery_items(board: Board) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for image in board.images:
        path = image.get("path") or ""
        if image.get("state") == "done" and path and Path(path).is_file():
            items.append((path, f"{image.get('id', '')} · {image.get('model', '')}"))
    return items


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


def save_status(board: Board, log: list[str], *, running: bool) -> None:
    board.revision += 1
    board.running = running
    board.log = list(log)
    payload = {
        "revision": board.revision,
        "running": running,
        "brief": board.brief,
        "folder": board.folder,
        "active": board.active,
        "done": sorted(board.done),
        "edge": board.edge,
        "returning": board.returning,
        "note": board.note,
        "log": board.log,
        "images": board.images,
    }
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATUS_PATH)


def _view(data: dict | None) -> tuple[str, list[tuple[str, str]], str]:
    board = board_from(data) if data else Board()
    log = "\n\n".join(board.log[-12:])
    return render_board(board), gallery_items(board), log


_run_task: asyncio.Task | None = None


def _ui() -> None:
    import gradio as gr

    studio = ScreenwriterStudio()
    stored = load_status()
    opening = board_from(stored) if stored else Board()

    async def _drive(brief: str, pages_only: bool, dry_run: bool) -> None:
        previous = load_status()
        board = Board()
        board.revision = int((previous or {}).get("revision") or 0)
        board.brief = brief
        board.edge = "brief-dev"
        board.note = "The brief is on its way to Development."
        log: list[str] = []
        save_status(board, log, running=True)
        try:
            async for chunk in studio.run(brief, pages_only=pages_only, dry_run=dry_run):
                apply_chunk(board, chunk)
                log.append(chunk.strip())
                save_status(board, log, running=True)
        except asyncio.CancelledError:
            board.note = "Run cancelled."
            save_status(board, log, running=False)
            raise
        except Exception as exc:
            board.note = str(exc)
            save_status(board, log, running=False)
        else:
            save_status(board, log, running=False)

    async def go(idea: str, pages_only: bool, dry_run: bool):
        global _run_task
        if _run_task is not None and not _run_task.done():
            _run_task.cancel()
            try:
                await _run_task
            except asyncio.CancelledError:
                pass
        brief = idea.strip() or "A 30-second vertical film about someone who keeps a seat on the last bus."
        start = load_status()
        start_revision = int((start or {}).get("revision") or 0)
        _run_task = asyncio.create_task(_drive(brief, pages_only, dry_run))
        seen = start_revision
        first = True
        while True:
            data = load_status()
            revision = int((data or {}).get("revision") or 0)
            running = bool((data or {}).get("running"))
            if revision != seen:
                seen = revision
                markup, pictures, log = _view(data)
                yield (
                    markup,
                    gr.update(value=pictures, visible=bool(pictures)),
                    log,
                    "" if first else gr.skip(),
                )
                first = False
            task_done = _run_task.done()
            if task_done and revision == seen and revision > start_revision and not running:
                break
            await asyncio.sleep(0.4)

    def toggle(showing: bool):
        opened = not showing
        return opened, gr.update(visible=opened)

    def poll(seen: int):
        data = load_status()
        revision = int((data or {}).get("revision") or 0)
        try:
            seen_revision = int(seen or 0)
        except (TypeError, ValueError):
            seen_revision = -1
        if revision == seen_revision:
            return gr.skip(), gr.skip(), gr.skip(), gr.skip()
        markup, pictures, log = _view(data)
        return markup, gr.update(value=pictures, visible=bool(pictures)), log, revision

    def restore():
        data = load_status()
        markup, pictures, log = _view(data)
        revision = int((data or {}).get("revision") or 0)
        return markup, gr.update(value=pictures, visible=bool(pictures)), log, revision

    def poll_library(seen: str):
        projects, signature = library_catalog()
        if signature == (seen or ""):
            return gr.skip(), gr.skip()
        return render_library(projects), signature

    def show_library_file(path: str):
        file = library_file(path)
        if file is None:
            return (
                "That file is not in a production folder.",
                gr.update(visible=False),
                gr.update(value=None, visible=False),
            )
        label = file.relative_to(PRODUCTIONS.resolve()).as_posix()
        if file.suffix.lower() in IMAGE_SUFFIXES:
            return (
                f"**{label}**",
                gr.update(visible=False),
                gr.update(value=str(file), visible=True),
            )
        return (
            f"**{label}**",
            gr.update(value=library_text(file), visible=True, label=file.name),
            gr.update(value=None, visible=False),
        )

    projects, signature = library_catalog()
    with gr.Blocks(title="Screenwriter Studio", css=CREW_CSS, js=LIBRARY_JS) as demo:
        with gr.Sidebar(label="Library", open=True, width=300):
            gr.Markdown("### Library\nDocuments and images the crew has written.")
            library = gr.HTML(render_library(projects), elem_id="library")
        gr.Markdown(
            "# Screenwriter Studio\n"
            "A sheet, a page, a shot card, and image frames travel with the work. "
            "Reload keeps the same crew status."
        )
        reader_title = gr.Markdown("Choose a document or an image from the library.")
        reader_text = gr.Textbox(label="Document", lines=16, interactive=False, visible=False)
        reader_image = gr.Image(label="Image", visible=False, height=360)
        board = gr.HTML(render_board(opening), elem_id="crew-board")
        gallery = gr.Gallery(
            label="Stills",
            columns=4,
            height=280,
            allow_preview=True,
            visible=bool(gallery_items(opening)),
            value=gallery_items(opening) or None,
        )
        with gr.Row():
            idea = gr.Textbox(label="Idea", lines=3, scale=8)
            idea_help = gr.Button("Help", scale=0, min_width=72)
        idea_open = gr.State(False)
        idea_copy = gr.Markdown(IDEA_HELP, visible=False, elem_classes=["option-help"])
        with gr.Row():
            with gr.Column():
                with gr.Row():
                    pages_only = gr.Checkbox(label="Pages only", scale=4)
                    pages_help = gr.Button("Help", scale=0, min_width=72)
                pages_open = gr.State(False)
                pages_copy = gr.Markdown(PAGES_HELP, visible=False, elem_classes=["option-help"])
            with gr.Column():
                with gr.Row():
                    dry_run = gr.Checkbox(label="Dry-run stills", scale=4)
                    dry_help = gr.Button("Help", scale=0, min_width=72)
                dry_open = gr.State(False)
                dry_copy = gr.Markdown(DRY_HELP, visible=False, elem_classes=["option-help"])
        run_crew = gr.Button("Run the crew", variant="primary")
        log = gr.Markdown("\n\n".join(opening.log[-12:]), elem_id="crew-log")
        revision = gr.State(opening.revision)
        library_seen = gr.State(signature)
        library_choice = gr.Textbox(elem_id="library-choice", show_label=False)
        # Gradio fades every output while an event runs. The status poll must
        # not use the crew panel for that, or the whole block blinks while idle.
        status_sink = gr.Markdown(visible=False)
        quiet = {"show_progress": "hidden", "show_progress_on": status_sink}
        outputs = [board, gallery, log, idea]
        idea.submit(go, [idea, pages_only, dry_run], outputs, **quiet)
        run_crew.click(go, [idea, pages_only, dry_run], outputs, **quiet)
        idea_help.click(toggle, [idea_open], [idea_open, idea_copy], **quiet)
        pages_help.click(toggle, [pages_open], [pages_open, pages_copy], **quiet)
        dry_help.click(toggle, [dry_open], [dry_open, dry_copy], **quiet)
        library_choice.change(
            show_library_file,
            [library_choice],
            [reader_title, reader_text, reader_image],
            **quiet,
        )
        demo.load(restore, outputs=[board, gallery, log, revision], **quiet)
        timer = gr.Timer(1)
        timer.tick(poll, [revision], [board, gallery, log, revision], **quiet)
        library_timer = gr.Timer(1)
        library_timer.tick(poll_library, [library_seen], [library, library_seen], **quiet)

    demo.launch(allowed_paths=[str(PRODUCTIONS.resolve())])


if __name__ == "__main__":
    args = _parse()
    if args.ui:
        _ui()
    elif not args.idea:
        print("Pass an idea, or run with --ui.", file=sys.stderr)
        raise SystemExit(2)
    else:
        asyncio.run(_stream(args.idea, args.pages_only, args.dry_run, args.aspect, args.runtime))
