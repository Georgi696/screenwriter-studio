"""Local production desk: static assets, HTTP routes, and server-sent events.

The browser patches crew, activity, and image nodes from structured snapshots.
No frontend build or third-party web framework is required.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import os
import shutil
import subprocess
import threading
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from screenwriter_studio.web.brief import BriefSuggestError, suggest_brief
from screenwriter_studio.web.board import (
    Board,
    append_log,
    apply_event,
    board_from,
    client_payload,
    event_station,
    load_status,
    save_status,
    scrub,
    board_from_folder,
)
from screenwriter_studio.models import (
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
from screenwriter_studio.studio import ScreenwriterStudio, _load_settings as load_run_settings, reference_choices
from screenwriter_studio.pricing import production_estimate, remaining_images, spent_estimate
from screenwriter_studio.schemas import Development, Screenplay
from screenwriter_studio.images.generator import run_batch
from screenwriter_studio.images.upload import SOURCE_LIMIT, thumbnail
from screenwriter_studio.references import covered_sheets
from screenwriter_studio.images.review import (
    apply_live_keep, attach_image_task, forget_image_submission, restore_version, validate_decision, versions,
)
from screenwriter_studio.paths import WORKSPACE_ROOT
from screenwriter_studio.events import StudioEvent
from screenwriter_studio.videos.plan import build_plan, read_json as read_video_json
from screenwriter_studio.videos.service import forget_submission, render_video, use_take
from screenwriter_studio import settings as studio_settings
from screenwriter_studio.errors import friendly
from screenwriter_studio.budget import shot_budget
from screenwriter_studio.videos.web import clips_zip, video_file, video_status, stream_video

STATIC = Path(__file__).resolve().parent / "static"
PAGE = STATIC / "desk.html"
PRODUCTIONS = WORKSPACE_ROOT / "productions"
DOCUMENT_SUFFIXES = {".json", ".md", ".fountain", ".txt"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DEFAULT_IDEA = "A 30-second vertical film about someone who keeps a seat on the last bus."
_MAX_BODY = 100_000
_MAX_UPLOAD_BODY = 12_000_000
_MAX_TEXT = 200_000
_MAX_IMAGE = SOURCE_LIMIT
_thumbs: dict[tuple, bytes] = {}
_thumb_lock = threading.Lock()
_MAX_UPLOADED_IMAGE = 8_000_000
_REFERENCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_STOP_NOTE = "Generation stopped. Saved images can be resumed."
_VIDEO_STOP_NOTE = "Generation stopped. Use Resume video clips to continue saved work."
_ABORT_NOTE = "This draft was aborted. The pages remain in the folder."

_lock = threading.Lock()
_board = Board()
_run_serial = 0
_future: asyncio.Future | None = None
_cancel: threading.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()


class Hub:
    """Wakes SSE clients when the board revision changes. No HTML is built here."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self.revision = 0

    def publish(self, revision: int) -> None:
        with self._cond:
            self.revision = revision
            self._cond.notify_all()

    def wait(self, seen: int, timeout: float) -> int:
        with self._cond:
            if self.revision == seen:
                self._cond.wait(timeout=timeout)
            return self.revision


hub = Hub()


def _restore() -> None:
    global _board
    data = load_status()
    _board = board_from(data) if data else Board()
    if _board.running:
        _board.running = False
        _board.active = _board.edge = None
        _board.returning = False
        _board.note = "Previous run was interrupted. Resume saved work in the Images or Video clips tab, or from the command line."
    # The manifest is authoritative if a checkpoint reached disk before the last UI event.
    if _board.folder:
        folder = Path(_board.folder)
        if not folder.is_absolute():
            folder = WORKSPACE_ROOT / folder
        try:
            folder.resolve().relative_to(PRODUCTIONS.resolve())
            spec = json.loads((folder / "images/jobs.json").read_text())
            manifest = json.loads((folder / "images/manifest.json").read_text())
            if spec.get("inspection") and isinstance(manifest.get("images"), list):
                _board.images = manifest["images"]
        except (OSError, ValueError):
            pass
    hub.revision = _board.revision


_restore()


def _secret() -> str:
    return os.environ.get("KIE_API_KEY", "")


def _image_agents() -> list[tuple[str, str]]:
    grouped: dict[str, list[str]] = {}
    for kind, model in IMAGE_MODEL_BY_KIND.items():
        grouped.setdefault(model, []).append(kind)
    order = [NANO_BANANA_MODEL, GPT_IMAGE_MODEL, SEEDREAM_MODEL, EDIT_MODEL]
    for model in grouped:
        if model not in order:
            order.append(model)
    return [(model, ", ".join(grouped[model])) for model in order if model in grouped]


def _replace(board: Board) -> None:
    """Caller holds _lock. Persists, then wakes the page."""
    global _board
    _board = board
    save_status(board, _secret())
    hub.publish(board.revision)


BOOT = uuid.uuid4().hex


def open_production(slug) -> None:
    """Put a saved production on the desk so its story, images and clips can be continued."""
    if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", slug):
        raise ValueError("Choose a production from the library.")
    folder = production_path(str(PRODUCTIONS / slug))
    if folder is None:
        raise ValueError("That production folder no longer exists.")
    with _lock:
        if _board.running:
            raise ValueError("Stop the current run before opening another production.")
        try:
            label = folder.relative_to(WORKSPACE_ROOT.resolve()).as_posix()
        except ValueError:
            label = str(folder)
        board = board_from_folder(folder, label)
        board.revision = _board.revision
        _replace(board)


def public_view() -> dict:
    with _lock:
        payload = client_payload(_board, _secret())
        payload["boot"] = BOOT
        images = []
        for image, raw in zip(payload["images"], _board.images):
            item = dict(image)
            path = str(raw.get("path") or "")
            if raw.get("state") in {"done", "review", "inspecting"} and _safe_image(path, _board.folder) is not None:
                item["src"] = "/media?id=" + urllib.parse.quote(str(raw.get("id") or ""), safe="") + "&v=" + str(raw.get("repairs", 0))
            else:
                item["src"] = ""
            image_id = urllib.parse.quote(str(raw.get("id") or ""), safe="")
            item["versions"] = [
                {"name": Path(entry["path"]).name,
                 "src": "/media?id=" + image_id + "&version=" + urllib.parse.quote(Path(entry["path"]).name, safe=""),
                 "label": _version_label(entry)}
                for entry in versions(raw) if _safe_image(str(entry["path"]), _board.folder) is not None]
            item["uncertain"] = raw.get("state") == "submitting" and not raw.get("task_id") and not _board.running
            item["edited"] = isinstance(raw.get("job_override"), dict)
            images.append(item)
        payload["images"] = images
        chat = studio_settings.load()["chat"]
        payload["crew"] = [
            {"id": "development", "name": "Development", "model": chat["development"]},
            {"id": "writer", "name": "Screenwriter", "model": chat["writer"]},
            {"id": "editor", "name": "Script editor", "model": chat["editor"]},
            {"id": "art", "name": "Image prompts", "model": chat["art"]},
        ]
        payload["image_agents"] = [
            {"model": model, "kinds": kinds} for model, kinds in _image_agents()
        ]
        payload.update(shot_plan(production_path(_board.folder)))
        payload["can_proceed"] = (not _board.running) and can_proceed(_board.folder)
        payload["pages_passed"] = saved_verdict(_board.folder)
        payload.update(cost_view(production_path(_board.folder), payload["can_proceed"]))
        payload["production"] = production_view(production_path(_board.folder))
        prompts = payload.pop("image_prompts", {})
        references = payload.pop("image_references", {})
        for item, raw in zip(payload["images"], _board.images):
            override = raw.get("job_override") if isinstance(raw.get("job_override"), dict) else {}
            item["prompt"] = scrub(str(override.get("prompt") or prompts.get(str(raw.get("id") or ""), "")), _secret())
            item["references"] = references.get(str(raw.get("id") or ""), [])
        payload["video"] = json.loads(scrub(json.dumps(video_status(production_path(_board.folder))), _secret()))
        return payload


_cost_cache: dict = {}


def _version_label(entry: dict) -> str:
    if entry.get("repairs"):
        return f"Repair {entry['repairs']}" + (f" of take {entry['take']}" if int(entry.get("take") or 1) > 1 else "")
    return f"Take {entry['take']}" if int(entry.get("take") or 1) > 1 else "Original"


def production_view(folder: Path | None) -> dict:
    """The open production as the app bar shows it: its title and the credits it has used."""
    if folder is None:
        return {"title": "", "slug": "", "spent": None}
    development = _read_saved(folder / "00_development.json")
    title = str(development.get("title") or "") if isinstance(development, dict) else ""
    spec = _read_saved(folder / "images/jobs.json")
    manifest = _read_saved(folder / "images/manifest.json")
    plan = _read_saved(folder / "videos/jobs.json")
    clips = _read_saved(folder / "videos/manifest.json")
    spent = None
    if isinstance(spec, dict) and isinstance(manifest, dict):
        try:
            spent = spent_estimate(spec, manifest, plan if isinstance(plan, dict) else None,
                                   (clips or {}).get("clips") if isinstance(clips, dict) else None)
        except (TypeError, ValueError, KeyError):
            spent = None
    return {"title": title, "slug": folder.name, "spent": spent}


def image_references(spec: dict) -> dict[str, list[dict]]:
    """The pictures each image was asked to follow, so a review can show them side by side."""
    jobs = {str(job.get("id")): job for job in spec.get("jobs") or [] if isinstance(job, dict)}
    locked = {str(item.get("id")): item for item in spec.get("locked_references") or [] if isinstance(item, dict)}
    root = PRODUCTIONS.resolve()
    found: dict[str, list[dict]] = {}
    for image_id, job in jobs.items():
        refs = []
        for ref in job.get("references") or []:
            ref = str(ref)
            if ref in jobs:
                kind = str(jobs[ref].get("kind") or "image")
                refs.append({"label": f"{ref} · {kind}", "role": _KIND_ROLES.get(kind, ""),
                             "src": "/media?id=" + urllib.parse.quote(ref, safe="")})
            elif ref in locked:
                try:
                    path = Path(str(locked[ref].get("path") or "")).resolve()
                except OSError:
                    continue
                if path.is_file() and _under(path, root):
                    role = str(locked[ref].get("mode") or "")
                    refs.append({"label": str(locked[ref].get("name") or ref) + (f" · {role}" if role else ""), "role": role,
                                 "src": "/library-media?path=" + urllib.parse.quote(path.relative_to(root).as_posix(), safe="")})
        if refs:
            found[image_id] = refs
    return found


def cost_view(folder: Path | None, proceed: bool) -> dict:
    """Credit estimates for the next paid step, from the saved files. Cached until one of them changes."""
    result = {"estimate": None, "images_estimate": None, "image_resolution": "", "image_prompts": {}, "image_references": {}}
    if folder is None:
        return result
    names = ("00_development.json", "01_screenplay.json", "run_settings.json", "images/jobs.json", "images/manifest.json")
    stamps = []
    for path in [folder / name for name in names] + [studio_settings.PATH]:
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    key = (str(folder), proceed, tuple(stamps))
    if _cost_cache.get("key") == key:
        value = _cost_cache["value"]
        return {**value, "image_prompts": dict(value["image_prompts"]), "image_references": dict(value["image_references"])}
    try:
        if proceed:
            development = Development.model_validate(read_video_json(folder / "00_development.json"))
            screenplay = Screenplay.model_validate(read_video_json(folder / "01_screenplay.json"))
            image_count, references = load_run_settings(folder)
            locked = covered_sheets(references, [c.name for c in development.characters], list(development.locations))
            result["estimate"] = production_estimate(development, screenplay, values=studio_settings.load(),
                                                     image_count=image_count, locked_count=locked)
        spec_path = folder / "images/jobs.json"
        if spec_path.is_file():
            spec = read_video_json(spec_path)
            manifest_path = folder / "images/manifest.json"
            manifest = read_video_json(manifest_path) if manifest_path.is_file() else {}
            result["image_resolution"] = str(spec.get("resolution") or "1K")
            result["image_prompts"] = {str(job.get("id")): str(job.get("prompt") or "")
                                       for job in spec.get("jobs", []) if isinstance(job, dict)}
            result["image_references"] = image_references(spec)
            remaining = remaining_images(spec, manifest)
            if remaining["count"]:
                result["images_estimate"] = remaining
    except (OSError, ValueError, KeyError, TypeError):
        pass
    _cost_cache.update(key=key, value=result)
    return {**result, "image_prompts": dict(result["image_prompts"]), "image_references": dict(result["image_references"])}


def shot_plan(folder: Path | None) -> dict:
    """The saved shot list, so every tab can tie an image or clip back to its shot."""
    empty = {"shots": [], "runtime_seconds": 0, "aspect_ratio": ""}
    if folder is None:
        return empty
    try:
        development = json.loads((folder / "00_development.json").read_text(encoding="utf-8"))
        screenplay = json.loads((folder / "01_screenplay.json").read_text(encoding="utf-8"))
        shots = [{"id": f"s{int(shot['number']):02d}", "number": int(shot["number"]),
                  "duration_seconds": int(shot["duration_seconds"]),
                  **{key: str(shot.get(key) or "") for key in ("framing", "camera_move", "location", "action", "audio", "notes")},
                  "characters": [str(name) for name in shot.get("characters") or []],
                  "dialogue": [{"speaker": str(line.get("speaker") or ""), "text": str(line.get("text") or "")}
                               for line in shot.get("dialogue") or [] if isinstance(line, dict)]}
                 for shot in screenplay["shots"]]
        return {"shots": shots, "runtime_seconds": int(development.get("runtime_seconds") or 0),
                "aspect_ratio": str(development.get("aspect_ratio") or "")}
    except (OSError, ValueError, KeyError, TypeError):
        return empty


def saved_verdict(folder: str) -> bool | None:
    """Whether the editor passed the saved pages, or nothing when there is no verdict yet."""
    path = production_path(folder)
    if path is None:
        return None
    try:
        verdict = json.loads((path / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    passed = verdict.get("passed") if isinstance(verdict, dict) else None
    return passed if isinstance(passed, bool) else None


def production_path(folder: str) -> Path | None:
    """A production directory under productions/, or nothing."""
    if not folder or not isinstance(folder, str):
        return None
    path = Path(folder)
    if not path.is_absolute():
        path = WORKSPACE_ROOT / folder
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_dir() or not _under(resolved, PRODUCTIONS):
        return None
    return resolved


def can_proceed(folder: str) -> bool:
    """Saved pages can continue when stills have not been submitted and the draft was not aborted."""
    path = production_path(folder)
    if path is None:
        return False
    if (path / "aborted.json").is_file():
        return False
    needed = ("00_development.json", "01_screenplay.json", "verdict.json")
    if not all((path / name).is_file() for name in needed):
        return False
    images = path / "images"
    if (images / "jobs.json").is_file() or (images / "manifest.json").is_file():
        return False
    try:
        verdict = json.loads((path / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(verdict, dict) and isinstance(verdict.get("passed"), bool)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _production_roots(folder: str) -> list[Path]:
    roots: list[Path] = []
    if PRODUCTIONS.is_dir():
        roots.append(PRODUCTIONS.resolve())
    if folder:
        candidate = Path(folder)
        if not candidate.is_absolute():
            candidate = WORKSPACE_ROOT / folder
        if candidate.is_dir():
            roots.append(candidate.resolve())
    return roots


def _safe_image(path: str, folder: str) -> Path | None:
    if not path:
        return None
    file = Path(path)
    try:
        if not file.is_file():
            return None
        resolved = file.resolve()
    except OSError:
        return None
    if resolved.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    if resolved.stat().st_size > _MAX_IMAGE:
        return None
    if any(_under(resolved, root) for root in _production_roots(folder)):
        return resolved
    return None


def library_catalog() -> tuple[list[dict], str]:
    """Documents and images under productions/<slug>/, plus a change signature."""
    root = PRODUCTIONS.resolve() if PRODUCTIONS.is_dir() else PRODUCTIONS
    projects: list[dict] = []
    signature: list[str] = []
    if not root.is_dir():
        return projects, ""
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        documents: list[dict] = []
        images: list[dict] = []
        videos: list[dict] = []
        for file in sorted(folder.rglob("*")):
            if not file.is_file() or file.name.startswith("."):
                continue
            resolved = file.resolve()
            if not _under(resolved, root):
                continue
            suffix = resolved.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                bucket = images
            elif suffix in DOCUMENT_SUFFIXES:
                bucket = documents
            elif suffix == ".mp4" and video_file(root, resolved.relative_to(root).as_posix()):
                bucket = videos
            else:
                continue
            relative = resolved.relative_to(root).as_posix()
            stat = resolved.stat()
            bucket.append({"path": relative, "name": resolved.relative_to(folder.resolve()).as_posix()})
            signature.append(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}")
        hints, saved = _image_hints(folder, root)
        summary = _project_summary(folder, root)
        for item in images:
            item.update(hints.get(item["path"], {}))
        projects.append({"slug": folder.name, "documents": documents, "images": images, "videos": videos,
                         "references": saved, "summary": summary})
    return projects, "|".join(signature)


_ROLE_LABELS = {"style": "style", "character": "character", "location": "location",
                "object": "object", "composition": "composition"}
_KIND_ROLES = {"character": "character", "location": "location", "product": "object", "packshot": "object"}


def _read_saved(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _project_summary(folder: Path, root: Path) -> dict:
    """How far a production got, what waits for the user, and a picture to recognise it by."""
    root = root.resolve()
    development = _read_saved(folder / "00_development.json")
    title = str(development.get("title") or "") if isinstance(development, dict) else ""
    summary = {"title": title or folder.name, "stage": "idea", "needs": 0, "images": [0, 0], "clips": [0, 0], "thumb": ""}
    if (folder / "01_screenplay.json").is_file():
        summary["stage"] = "story"
    manifest = _read_saved(folder / "images/manifest.json")
    records = [r for r in (manifest or {}).get("images") or [] if isinstance(r, dict)] if isinstance(manifest, dict) else []
    if records:
        summary["stage"] = "images"
        summary["images"] = [sum(1 for r in records if r.get("state") == "done"), len(records)]
        summary["needs"] = sum(1 for r in records if r.get("state") in {"review", "failed"})
        shots = sorted((r for r in records if re.fullmatch(r"s\d{2,3}", str(r.get("id") or ""))), key=lambda r: r["id"])
        for record in shots + records:
            try:
                path = Path(str(record.get("path") or "")).resolve()
            except OSError:
                continue
            if path.is_file() and _under(path, root) and path.suffix.lower() in _REFERENCE_SUFFIXES:
                summary["thumb"] = path.relative_to(root).as_posix()
                break
    videos = _read_saved(folder / "videos/manifest.json")
    clips = [c for c in (videos or {}).get("clips") or [] if isinstance(c, dict)] if isinstance(videos, dict) else []
    if clips:
        summary["stage"] = "clips"
        summary["clips"] = [sum(1 for c in clips if c.get("state") == "done"), len(clips)]
    return summary


def _image_hints(folder: Path, root: Path) -> tuple[dict[str, dict], list[dict]]:
    """What the studio already knows about a production's images, so the picker need not guess.

    Returns hints by library path (role, subject, a readable label, and whether the file is an
    earlier version) and the production's saved reference choices, pointing at its own copies.
    """
    def library_path(raw) -> str | None:
        try:
            path = Path(str(raw)).resolve()
            return path.relative_to(root).as_posix() if path.is_file() and _under(path, root) else None
        except (OSError, ValueError):
            return None

    hints: dict[str, dict] = {}
    saved: list[dict] = []
    settings = _read_saved(folder / "run_settings.json")
    refs = settings.get("references") if isinstance(settings, dict) else None
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or ref.get("mode") not in _ROLE_LABELS:
            continue
        path = library_path(ref.get("path"))
        if path is None:
            continue
        name = str(ref.get("name") or "")
        hint = {"role": ref["mode"], "subject": name, "label": f"{name or ref.get('id', '')} · {_ROLE_LABELS[ref['mode']]} reference"}
        if ref["mode"] == "character":
            hint.update(face=ref.get("face", True) is not False, wardrobe=ref.get("wardrobe", True) is not False)
        hints[path] = hint
        saved.append({"path": path, "mode": ref["mode"], "name": name, "instruction": str(ref.get("instruction") or ""),
                      "scope": "shots" if ref.get("shots") else "auto", "shots": list(ref.get("shots") or []),
                      **({"face": hint["face"], "wardrobe": hint["wardrobe"]} if ref["mode"] == "character" else {})})

    jobs = _read_saved(folder / "images/jobs.json")
    manifest = _read_saved(folder / "images/manifest.json")
    development = _read_saved(folder / "00_development.json")
    if not isinstance(jobs, dict) or not isinstance(manifest, dict):
        return hints, saved
    characters = development.get("characters") if isinstance(development, dict) else None
    names = {str(c.get("name", "")).casefold(): str(c.get("name")) for c in characters or [] if isinstance(c, dict)}
    kinds = {job.get("id"): job.get("kind") for job in jobs.get("jobs") or [] if isinstance(job, dict)}
    for record in manifest.get("images") or []:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            continue
        image_id, kind = record["id"], kinds.get(record["id"])
        role = _KIND_ROLES.get(kind)
        shot = re.fullmatch(r"s(\d{2,3})", image_id) if kind in {"keyframe", "scene"} else None
        if role == "character":
            subject = names.get(image_id.casefold(), image_id.replace("-", " ").replace("_", " ").title())
        else:
            subject = image_id.replace("-", " ").replace("_", " ")
        label = f"Shot {int(shot.group(1))}" if shot else f"{subject} · {kind or 'image'}"
        hint = {"label": label, **({"role": role, "subject": subject} if role else {})}
        current = library_path(record.get("path"))
        if current:
            hints.setdefault(current, hint)
        for entry in versions(record):
            earlier = library_path(entry.get("path"))
            if earlier:
                hints.setdefault(earlier, {**hint, "label": label + " · earlier version", "earlier": True})
    return hints, saved


def _image_header_kind(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def _suffix_matches_image(suffix: str, header: bytes) -> bool:
    kind = _image_header_kind(header)
    if kind == ".jpg":
        return suffix in {".jpg", ".jpeg"}
    return kind is not None and kind == suffix


def reference_file(path: str) -> Path | None:
    """A PNG, JPEG, or WebP already stored under productions/."""
    file = library_file(path)
    if file is None or file.suffix.lower() not in _REFERENCE_SUFFIXES:
        return None
    try:
        size = file.stat().st_size
        header = file.read_bytes()[:12]
    except OSError:
        return None
    if size <= 0 or size > _MAX_IMAGE or not _suffix_matches_image(file.suffix.lower(), header):
        return None
    return file


def parse_image_count(value) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 240:
        raise ValueError("Image count must be an integer between 1 and 240.")
    return value


def parse_references(value) -> list[dict]:
    """Validate reference roles and settings against files in the library."""
    chosen: list[dict] = []
    seen: set[str] = set()
    for choice in reference_choices(value):
        relative = choice["path"]
        if reference_file(relative) is None:
            raise ValueError("Reference image is not in a production folder.")
        if relative not in seen:
            seen.add(relative)
            chosen.append(choice)
    return chosen


def save_reference_upload(filename, encoded) -> dict:
    """Store an uploaded still under productions/.references without using the client path."""
    if not isinstance(filename, str) or not isinstance(encoded, str):
        raise ValueError("Upload must be a PNG, JPEG, or WebP image.")
    suffix = Path(filename).suffix.lower()
    if suffix not in _REFERENCE_SUFFIXES or Path(filename).name != filename:
        raise ValueError("Upload must be a PNG, JPEG, or WebP image.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Upload must be a PNG, JPEG, or WebP image.") from exc
    if not data or len(data) > _MAX_UPLOADED_IMAGE or not _suffix_matches_image(suffix, data[:12]):
        raise ValueError("Upload must be a PNG, JPEG, or WebP image within the size limit.")
    if not PRODUCTIONS.is_dir():
        raise ValueError("Upload path is not allowed.")
    root = PRODUCTIONS.resolve()
    dest_dir = (root / ".references").resolve()
    if not _under(dest_dir, root):
        raise ValueError("Upload path is not allowed.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / f"{uuid4().hex}{suffix}").resolve()
    if not _under(dest, dest_dir) or dest.exists():
        raise ValueError("Upload path is not allowed.")
    temporary = dest.with_suffix(dest.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(dest)
    relative = dest.relative_to(root).as_posix()
    return {
        "id": relative,
        "src": "/library-media?path=" + urllib.parse.quote(relative, safe=""),
    }


def library_file(path: str) -> Path | None:
    raw = (path or "").strip()
    if not raw or raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
        return None
    if not PRODUCTIONS.is_dir():
        return None
    root = PRODUCTIONS.resolve()
    file = (root / raw).resolve()
    if not file.is_file() or not _under(file, root):
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
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n\n… truncated"
    return scrub(text, _secret())


STAGES = ("brief", "development", "writer", "editor", "approval", "art")
_DEVELOPMENT_FIELDS = (
    "title", "logline", "job_type", "runtime_seconds", "aspect_ratio", "tone", "audience",
    "look", "palette", "lighting", "characters", "locations", "beats", "invented",
)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _draft_number(path: Path) -> int:
    head = path.name.split("_", 1)[0]
    return int(head) if head.isdigit() else 0


def stage_detail(station: str) -> dict:
    """What one station produced, read from the production folder. Paths are library-relative."""
    if station not in STAGES:
        raise ValueError("Unknown station.")
    with _lock:
        folder_label = _board.folder
        brief = _board.brief
    folder = production_path(folder_label)
    detail: dict = {"station": station, "folder": folder_label, "files": [], "data": {}}
    root = PRODUCTIONS.resolve() if PRODUCTIONS.is_dir() else None

    def add_file(path: Path, label: str = "") -> None:
        if root is None or not path.is_file() or not _under(path, root):
            return
        detail["files"].append({"path": path.resolve().relative_to(root).as_posix(), "name": label or path.name})

    data = detail["data"]
    settings = _read_json(folder / "run_settings.json") if folder else None
    settings = settings if isinstance(settings, dict) else {}
    modes = {
        item["id"]: item.get("mode", "lock") for item in settings.get("references") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if station == "brief":
        data["brief"] = brief
        data["image_count"] = settings.get("image_count")
        data["references"] = [
            {key: item[key] for key in ("id", "name", "mode", "scope", "shots", "instruction", "face", "wardrobe") if key in item}
            for item in settings.get("references") or [] if isinstance(item, dict)
        ]
        if folder:
            for item in sorted((folder / "references").glob("*")):
                add_file(item)
    elif folder is None:
        pass
    elif station == "development":
        saved = _read_json(folder / "00_development.json")
        if isinstance(saved, dict):
            data.update({key: saved.get(key) for key in _DEVELOPMENT_FIELDS})
        add_file(folder / "00_development.json")
    elif station == "writer":
        drafts = sorted((folder / "drafts").glob("*_screenplay.json"), key=_draft_number)
        final = folder / "01_screenplay.json"
        latest = _read_json(final) if final.is_file() else (_read_json(drafts[-1]) if drafts else None)
        data["drafts"] = len(drafts)
        data["final"] = final.is_file()
        if isinstance(latest, dict):
            data["shots"] = [
                {key: shot.get(key) for key in ("number", "duration_seconds", "framing", "action")}
                for shot in latest.get("shots") or [] if isinstance(shot, dict)
            ]
            data["notes"] = latest.get("notes") or []
        add_file(folder / "01_script.fountain")
        add_file(folder / "02_shots.md")
        for draft in drafts:
            add_file(draft, f"Draft {_draft_number(draft)}")
    elif station == "editor":
        verdicts = []
        for path in sorted((folder / "drafts").glob("*_verdict.json"), key=_draft_number):
            saved = _read_json(path)
            if isinstance(saved, dict):
                verdicts.append({"draft": _draft_number(path), "passed": bool(saved.get("passed")),
                                 "issues": [str(item) for item in saved.get("issues") or []]})
                add_file(path, f"Verdict on draft {_draft_number(path)}")
        data["verdicts"] = verdicts
        add_file(folder / "verdict.json")
    elif station == "approval":
        greenlight = _read_json(folder / "greenlight.json")
        data["passed"] = saved_verdict(folder_label)
        data["approved"] = isinstance(greenlight, dict) and bool(greenlight.get("proceeded"))
        data["aborted"] = (folder / "aborted.json").is_file()
        data["waiting"] = can_proceed(folder_label)
        add_file(folder / "01_script.fountain")
        add_file(folder / "02_shots.md")
        add_file(folder / "greenlight.json")
    elif station == "art":
        submitted = folder / "images" / "jobs.json"
        plan = _read_json(submitted) if submitted.is_file() else _read_json(folder / "03_still_plan.json")
        jobs = plan.get("jobs") if isinstance(plan, dict) else None
        data["submitted"] = submitted.is_file()
        data["jobs"] = [
            {"id": job.get("id"), "kind": job.get("kind"), "prompt": job.get("prompt"),
             "references": [{"id": ref, "mode": modes.get(ref, "")} for ref in job.get("references") or []]}
            for job in jobs or [] if isinstance(job, dict)
        ]
        add_file(folder / "03_still_plan.json")
        add_file(submitted)
    return json.loads(scrub(json.dumps(detail), _secret()))


def _media_for(image_id: str, version: str = "") -> Path | None:
    secret = _secret()
    with _lock:
        folder = _board.folder
        images = [dict(image) for image in _board.images]
    for image in images:
        raw_id = str(image.get("id") or "")
        if raw_id != image_id and scrub(raw_id, secret) != image_id:
            continue
        if version:
            # An earlier version is served only when it is listed in this image's own history.
            entry = next((item for item in versions(image) if Path(item["path"]).name == version), None)
            return _safe_image(str(entry["path"]), folder) if entry else None
        return _safe_image(str(image.get("path") or ""), folder)
    return None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is not None:
        return _loop
    with _lock:
        if _loop is not None:
            return _loop
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            _loop_ready.set()
            loop.run_forever()

        threading.Thread(target=_run, name="desk-loop", daemon=True).start()
        _loop_ready.wait(5)
        _loop = loop
        return loop


def _current(serial: int) -> bool:
    with _lock:
        return serial == _run_serial


def _apply(serial: int, chunk: StudioEvent) -> None:
    with _lock:
        if serial != _run_serial or (_cancel is not None and _cancel.is_set()):
            return
        station = event_station(_board, chunk)
        apply_event(_board, chunk)
        append_log(_board, station, chunk.text.strip())
        _board.running = True
        _replace(_board)


def _finish(serial: int, note: str | None) -> None:
    with _lock:
        if serial != _run_serial:
            return
        if _cancel is not None and _cancel.is_set():
            note = _VIDEO_STOP_NOTE if _board.active == "video" else _STOP_NOTE
        if note:
            _board.note = note
            _board.active = _board.edge = None
            _board.returning = False
        _board.running = False
        _replace(_board)


async def _consume(serial: int, stream) -> None:
    note = None
    try:
        async for chunk in stream:
            if not _current(serial):
                return
            _apply(serial, chunk)
    except asyncio.CancelledError:
        note = _STOP_NOTE
    except Exception as exc:
        message = str(exc)
        secret = _secret()
        if not secret or secret not in message:
            traceback.print_exc()
        note = "The run stopped: " + friendly(exc, secret)
    finally:
        _finish(serial, note)


async def _drive(serial: int, idea: str, pages_only: bool, dry_run: bool, max_shots: int | None, inspection_mode: str, cancel: threading.Event, image_count: int | None = None, references: list | None = None, approval: bool = False, runtime_seconds: int | None = None, aspect_ratio: str | None = None) -> None:
    studio = ScreenwriterStudio()
    await _consume(serial, studio.run(
        idea, pages_only=pages_only, dry_run=dry_run, max_shots=max_shots,
        inspection_mode=inspection_mode, cancel=cancel, image_count=image_count, references=references,
        approval=approval, runtime_seconds=runtime_seconds, aspect_ratio=aspect_ratio,
    ))


async def _drive_saved(serial: int, folder: Path, brief: str, dry_run: bool, inspection_mode: str, cancel: threading.Event) -> None:
    studio = ScreenwriterStudio()
    await _consume(serial, studio.proceed(
        folder, dry_run=dry_run, inspection_mode=inspection_mode, brief=brief, cancel=cancel,
    ))


def clear_log() -> None:
    """Drop the activity lines. The crew, the brief, and the stills stay."""
    with _lock:
        _board.log = []
        _board.log_stations = []
        _replace(_board)


def _cut(cancel: threading.Event | None, future) -> None:
    """Set the batch event, then cancel the crew task. The event is what the batch thread polls."""
    if cancel is not None:
        cancel.set()
    if future is not None and not future.done():
        future.cancel()


def _commit_future(serial: int, cancel: threading.Event, coro) -> None:
    """Start coro unless stop already retired this serial."""
    global _future
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    discarded = None
    with _lock:
        if serial != _run_serial:
            discarded = future
        else:
            _future = future
    if discarded is not None:
        cancel.set()
        discarded.cancel()


def clear_session() -> None:
    """Stop the crew and put the desk back to a blank brief."""
    global _future, _run_serial, _cancel
    future = None
    cancel = None
    with _lock:
        _run_serial += 1
        future = _future
        cancel = _cancel
        _future = None
        _cancel = None
        board = Board()
        board.revision = _board.revision
        _replace(board)
    _cut(cancel, future)


def stop_run() -> None:
    """Stop image generation and the in-flight crew step. Saved files stay on disk."""
    global _run_serial
    with _lock:
        cancel = _cancel
        future = _future
        if cancel is not None:
            cancel.set()
        if _board.running:
            note = _VIDEO_STOP_NOTE if _board.active == "video" else _STOP_NOTE
            _run_serial += 1
            _board.running = False
            _board.active = None
            _board.edge = None
            _board.returning = False
            _board.note = note
            for image in _board.images:
                if image.get("state") in {"running", "inspecting"}:
                    image["state"] = "failed"
                    image["error"] = _STOP_NOTE
            _replace(_board)
    if future is not None and not future.done():
        future.cancel()


def start_crew(idea: str, pages_only: bool, dry_run: bool, max_shots: int | None = None, inspection_mode: str = "manual", image_count: int | None = None, references: list | None = None, approval: bool = False, runtime_seconds: int | None = None, aspect_ratio: str | None = None) -> None:
    global _run_serial, _cancel
    _ensure_loop()
    with _lock:
        if _board.running:
            raise ValueError("A run is already in progress. Stop it first, or wait for it to finish.")
        previous_cancel = _cancel
        previous_future = _future
        cancel = threading.Event()
        _cancel = cancel
        _run_serial += 1
        serial = _run_serial
        previous = load_status()
        board = Board()
        board.revision = int((previous or {}).get("revision") or _board.revision or 0)
        board.brief = idea
        board.edge = "brief-dev"
        board.note = "The brief is on its way to Development."
        board.running = True
        _replace(board)
    _cut(previous_cancel, previous_future)
    _commit_future(
        serial, cancel,
        _drive(serial, idea, pages_only, dry_run, max_shots, inspection_mode, cancel, image_count=image_count, references=references, approval=approval, runtime_seconds=runtime_seconds, aspect_ratio=aspect_ratio),
    )


async def _resume_images(serial, out, spec, decision, cancel: threading.Event):
    def update(record):
        _apply(serial, StudioEvent("image", f"Image {record['id']}: {record.get('state', 'working')}", image=record))
    note = _STOP_NOTE
    try:
        manifest = await asyncio.to_thread(run_batch, spec, out, resume=True, decision=decision,
                                         on_record=update, cancel=cancel,
                                         on_start=lambda r: update({**r, "phase": "start"}))
        note = "Images complete." if manifest.get("status") == "complete" else "Images paused. Review the image cards or resume saved image work."
    except asyncio.CancelledError:
        cancel.set()
        note = _STOP_NOTE
        raise
    except Exception as exc:
        note = _STOP_NOTE if cancel.is_set() else scrub(str(exc), _secret())
    finally:
        _finish(serial, note)


def _keep_without_provider(out: Path, decision: dict) -> None:
    """Accept a saved review without starting another batch or a provider call."""
    if apply_live_keep(out, decision) is not None:
        return
    saved = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    images = saved.get("images")
    if not isinstance(images, list):
        raise ValueError("Saved image manifest is unreadable.")
    records = {record["id"]: record for record in images if isinstance(record, dict) and isinstance(record.get("id"), str)}
    updated = dict(validate_decision(records, decision))
    if not Path(updated.get("path", "")).is_file() or not updated.get("url"):
        raise ValueError("The reviewed image is missing; it cannot be accepted.")
    updated.update(state="done", decision="human accepted", error="")
    records[updated["id"]] = updated
    for other_id, other in list(records.items()):
        if other.get("state") != "blocked":
            continue
        blocked_by = [
            ref for ref in (other.get("blocked_by") or [])
            if not (records.get(ref, {}).get("state") == "done" and records.get(ref, {}).get("decision") == "human accepted")
        ]
        if blocked_by == list(other.get("blocked_by") or []):
            continue
        other = dict(other)
        if blocked_by:
            other["blocked_by"] = blocked_by
        else:
            other.pop("blocked_by", None)
            other["state"] = "planned"
        records[other_id] = other
    saved["images"] = [records.get(image.get("id"), image) if isinstance(image, dict) else image for image in images]
    states = [image.get("state") for image in saved["images"] if isinstance(image, dict)]
    waiting = any(state in {"review", "inspecting", "polling", "submitting", "running"} for state in states)
    if states and all(state == "done" for state in states):
        saved["status"] = "complete"
        note = "Images complete."
    else:
        saved["status"] = "paused"
        note = "Images paused. Review the image cards or resume saved image work."
    temporary = out / "manifest.tmp"
    temporary.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    temporary.replace(out / "manifest.json")
    with _lock:
        _board.images = [dict(image) for image in saved["images"] if isinstance(image, dict)]
        if not waiting:
            _board.running = False
            _board.active = _board.edge = None
            _board.returning = False
            _board.note = note
        _replace(_board)


def image_action(action: str, data: dict) -> None:
    """Choose an earlier version, or recover an uncertain submission. None of these calls a provider."""
    image_id = data.get("id")
    with _lock:
        if _board.running:
            raise ValueError("Wait for the current run to finish, or stop it first.")
        folder = production_path(_board.folder)
        if folder is None:
            raise ValueError("No saved image production is selected.")
        out = folder / "images"
        if action == "version":
            manifest = restore_version(out, image_id, str(data.get("version") or ""))
            note = "Image version changed. No credits spent."
        elif action == "forget":
            manifest = forget_image_submission(out, image_id)
            note = "Submission forgotten. Resume saved image work to send it again."
        else:
            manifest = attach_image_task(out, image_id, str(data.get("task_id") or ""))
            note = "Task ID attached. Resume saved image work to fetch the image."
        _board.images = [dict(image) for image in manifest.get("images", []) if isinstance(image, dict)]
        _board.note = note
        _replace(_board)


def video_take(shot, take) -> None:
    """Make an earlier take of a clip the current one. No provider call."""
    if not isinstance(shot, str) or not re.fullmatch(r"s\d{2,3}", shot):
        raise ValueError("Choose a shot.")
    with _lock:
        if _board.running:
            raise ValueError("Wait for the current run to finish, or stop it first.")
        folder = production_path(_board.folder)
        if folder is None:
            raise ValueError("No production is selected.")
        use_take(folder, shot, str(take or ""))
        _board.note = f"Shot {int(shot[1:])} now uses that take. No credits spent."
        _replace(_board)


def resume_images(data):
    global _run_serial, _cancel
    _ensure_loop()
    launch = None
    with _lock:
        decision = data if data else None
        keep_now = isinstance(decision, dict) and decision.get("action") == "keep"
        if _board.running and not keep_now:
            raise ValueError("Wait for the current run to finish before reviewing images.")
        folder = Path(_board.folder)
        if not folder.is_absolute():
            folder = WORKSPACE_ROOT / folder
        out = folder / "images"
        if not _board.folder or not _under(out, PRODUCTIONS):
            raise ValueError("No saved image production is selected.")
        spec = json.loads((out / "jobs.json").read_text())
        if not spec.get("inspection"):
            raise ValueError("This production predates image inspection. Resume it using the command line.")
        if not keep_now and decision:
            saved = json.loads((out / "manifest.json").read_text())
            validate_decision({r["id"]: r for r in saved["images"]}, decision)
        if keep_now:
            held_out, held_decision = out, decision
        else:
            previous_cancel = _cancel
            previous_future = _future
            cancel = threading.Event()
            _cancel = cancel
            _run_serial += 1
            serial = _run_serial
            _board.running = True
            _board.note = "Continuing saved image work."
            _replace(_board)
            launch = (serial, cancel, previous_cancel, previous_future, out, spec, decision)
    if launch is None:
        _keep_without_provider(held_out, held_decision)
        return
    serial, cancel, previous_cancel, previous_future, out, spec, decision = launch
    _cut(previous_cancel, previous_future)
    _commit_future(serial, cancel, _resume_images(serial, out, spec, decision, cancel))


def _require_proceed(inspection_mode: str) -> None:
    """Caller holds _lock. Reject a continue that would spend credits on the wrong draft."""
    if _board.running:
        raise ValueError("Wait for the current run to finish before continuing this draft.")
    if inspection_mode not in {"manual", "auto"}:
        raise ValueError("Inspection mode must be manual or auto.")
    if not can_proceed(_board.folder):
        raise ValueError("This draft cannot be continued. Image work that is already saved can be resumed from the Images tab.")


def abort_draft() -> None:
    """Drop the offer to continue. Pages stay on disk. No art director and no stills."""
    with _lock:
        if _board.running or not can_proceed(_board.folder):
            raise ValueError("This draft cannot be aborted.")
        folder = production_path(_board.folder)
        if folder is None:
            raise ValueError("This draft cannot be aborted.")
        marker = folder / "aborted.json"
        temporary = marker.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"aborted": True}) + "\n", encoding="utf-8")
        temporary.replace(marker)
        _board.note = _ABORT_NOTE
        _replace(_board)


def proceed_saved(dry_run: bool, inspection_mode: str) -> None:
    """Send the saved pages to the art director. The editor is not asked again."""
    global _run_serial, _cancel
    with _lock:
        _require_proceed(inspection_mode)
    _ensure_loop()
    with _lock:
        _require_proceed(inspection_mode)
        folder = production_path(_board.folder)
        if folder is None:
            raise ValueError("This draft cannot be continued.")
        previous_cancel = _cancel
        previous_future = _future
        cancel = threading.Event()
        _cancel = cancel
        _run_serial += 1
        serial = _run_serial
        brief = _board.brief
        _board.running = True
        _board.note = "Continuing with the saved draft."
        _replace(_board)
    _cut(previous_cancel, previous_future)
    _commit_future(
        serial, cancel,
        _drive_saved(serial, folder, brief, dry_run, inspection_mode, cancel),
    )


async def _drive_video(serial, folder, mode, sound, dry_run, cancel, selection=None, shots=None, redo=None, prompts=None):
    def update(manifest):
        complete = sum(clip.get("state") == "done" for clip in manifest["clips"])
        text = f"Video {manifest['status']}: {complete}/{len(manifest['clips'])} clips ready."
        _apply(serial, StudioEvent("video", text))
    note = None
    try:
        result = await asyncio.to_thread(render_video, folder, mode=mode, sound=sound,
                                         selection=selection, dry_run=dry_run, on_update=update, cancel=cancel,
                                         shots=shots, regenerate=redo, prompts=prompts)
        note = {"planned": "Clip plan saved. No credits spent.",
                "partial": "Clips saved. Render the remaining shots to finish the set."}.get(
            result["status"], "All video clips are ready. Open the Video clips tab to play or download them.")
    except asyncio.CancelledError:
        cancel.set()
        raise
    except Exception as exc:
        traceback.print_exc()
        note = friendly(exc, _secret())
    finally:
        _finish(serial, note)


def start_video(data: dict) -> None:
    global _run_serial, _cancel
    mode, sound = data.get("mode"), data.get("sound")
    selection = data.get("selection")
    if selection is not None and selection not in ("auto", "kling", "seedance"):
        raise ValueError("Choose auto, kling, or seedance model selection.")
    dry_run = data.get("dry_run", False)
    shots, redo = data.get("shots"), data.get("redo")
    for chosen in (shots, redo):
        if chosen is not None and (not isinstance(chosen, list) or len(chosen) > 240 or not all(
                isinstance(item, str) and re.fullmatch(r"s\d{2,3}", item) for item in chosen)):
            raise ValueError("Shots must be a list of shot IDs such as s01.")
    if (shots or redo) and dry_run:
        raise ValueError("A video plan covers every shot.")
    prompts = data.get("prompts")
    if prompts is not None and (not isinstance(prompts, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in prompts.items())
            or set(prompts) - set(shots or []) - set(redo or [])):
        raise ValueError("Edit a prompt only for the shot you render or redo.")
    if mode is not None and mode not in ("std", "pro", "4K"):
        raise ValueError("Choose std, pro, or 4K video quality.")
    if (sound is not None and type(sound) is not bool) or type(dry_run) is not bool:
        raise ValueError("Sound and dry-run settings must be boolean.")
    _ensure_loop()
    with _lock:
        if _board.running:
            raise ValueError("Wait for the current run to finish before generating video.")
        folder = production_path(_board.folder)
        if folder is None:
            raise ValueError("No saved production is selected.")
        saved = folder / "videos/jobs.json"
        settings = read_video_json(saved) if (folder / "videos/manifest.json").exists() else {}
        defaults = studio_settings.load()["video"]
        mode = mode if mode is not None else settings.get("mode", defaults["mode"])
        sound = sound if sound is not None else settings.get("sound", defaults["sound"])
        selection = selection if selection is not None else settings.get("selection", "kling" if settings else defaults["selection"])
        build_plan(folder, mode=mode, sound=sound, selection=selection, hash_images=False, frozen_plan=settings or None)
        previous_cancel, previous_future = _cancel, _future
        cancel = threading.Event()
        _cancel = cancel
        _run_serial += 1
        serial = _run_serial
        _board.running = True
        _board.active = "video"
        _board.edge = None
        _board.note = "Planning video." if dry_run else "Generating video clips from the approved images."
        _replace(_board)
    _cut(previous_cancel, previous_future)
    _commit_future(serial, cancel, _drive_video(serial, folder, mode, sound, dry_run, cancel, selection, shots, redo, prompts))


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "application/octet-stream"


class DeskHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ScreenwriterDesk"

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            # Browsers can abandon a connection before sending a request or
            # while receiving a response, including during page reloads.
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        if not self._local_request():
            return
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if route in {"/", "/index.html"}:
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        assets = {"/static/desk.css": ("desk.css", "text/css; charset=utf-8"),
                  "/static/desk.js": ("desk.js", "text/javascript; charset=utf-8")}
        if route in assets:
            filename, content_type = assets[route]
            self._send(200, (STATIC / filename).read_bytes(), content_type)
            return
        if route == "/api/status":
            self._json(200, public_view())
            return
        if route == "/api/events":
            self._events()
            return
        if route == "/api/stage":
            try:
                self._json(200, stage_detail(query.get("station", [""])[0]))
            except ValueError as exc:
                self._json(404, {"error": str(exc)})
            return
        if route == "/api/settings":
            self._json(200, {"values": studio_settings.load(), "schema": studio_settings.schema()})
            return
        if route == "/api/library":
            projects, signature = library_catalog()
            self._json(200, {"signature": signature, "projects": projects})
            return
        if route == "/api/file":
            self._file(query.get("path", [""])[0])
            return
        if route == "/media":
            self._media(query.get("id", [""])[0], query.get("version", [""])[0])
            return
        if route == "/library-media":
            self._library_media(query.get("path", [""])[0])
            return
        if route == "/library-thumb":
            self._library_thumb(query.get("path", [""])[0])
            return
        if route == "/video-zip":
            with _lock:
                folder = production_path(_board.folder)
            if folder is None or not clips_zip(self, folder):
                self._send(404, b"No finished clips", "text/plain")
            return
        if route == "/video-media":
            path = video_file(PRODUCTIONS, query.get("path", [""])[0])
            if path is None:
                self._send(404, b"Video not found", "text/plain")
            else:
                stream_video(self, path, attachment=query.get("download") == ["1"])
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        """Every failure answers with JSON, so the desk can show a sentence instead of a dropped connection."""
        try:
            self._post()
        except (ConnectionResetError, BrokenPipeError):
            raise
        except Exception as exc:
            traceback.print_exc()
            try:
                self._json(500, {"error": friendly(exc, _secret())})
            except OSError:
                pass

    def _post(self) -> None:
        if not self._local_request(mutating=True):
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/log/clear":
            clear_log()
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/session/clear":
            clear_session()
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/stop":
            stop_run()
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/abort-draft":
            try:
                abort_draft()
            except ValueError as exc:
                self._json(409, {"error": scrub(str(exc), _secret())})
                return
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/references":
            data = self._read_json(_MAX_UPLOAD_BODY)
            if data is None:
                return
            try:
                saved = save_reference_upload(data.get("filename"), data.get("data"))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, saved)
            return
        if parsed.path not in {"/api/run", "/api/brief/suggest", "/api/images/review", "/api/proceed", "/api/video", "/api/settings",
                               "/api/production/open", "/api/video/forget", "/api/video/take",
                               "/api/images/version", "/api/images/forget", "/api/images/attach"}:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        data = self._read_json()
        if data is None:
            return
        if parsed.path == "/api/settings":
            try:
                values = studio_settings.save(studio_settings.DEFAULTS if data.get("reset") is True else data.get("values"))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            with _lock:
                # Estimates and model labels on the desk follow the new choices.
                _board.revision += 1
                hub.publish(_board.revision)
            self._json(200, {"values": values})
            return
        if parsed.path == "/api/video/forget":
            shot = data.get("shot")
            with _lock:
                running = _board.running
                folder = production_path(_board.folder)
            if running or folder is None or not isinstance(shot, str) or not re.fullmatch(r"s\d{2,3}", shot):
                self._json(409, {"error": "Stop the current run and choose a shot first."})
                return
            try:
                forget_submission(folder, shot)
            except (ValueError, OSError) as exc:
                self._json(409, {"error": friendly(exc, _secret())})
                return
            with _lock:
                _replace(_board)
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/production/open":
            try:
                open_production(data.get("slug"))
            except ValueError as exc:
                self._json(409, {"error": friendly(exc, _secret())})
                return
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/video":
            try:
                start_video(data)
            except (ValueError, OSError) as exc:
                self._json(409, {"error": friendly(exc, _secret())})
                return
            self._json(202, {"ok": True})
            return
        if parsed.path in {"/api/images/version", "/api/images/forget", "/api/images/attach"}:
            try:
                image_action(parsed.path.rsplit("/", 1)[1], data)
            except (ValueError, OSError) as exc:
                self._json(409, {"error": friendly(exc, _secret())})
                return
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/video/take":
            try:
                video_take(data.get("shot"), data.get("take"))
            except (ValueError, OSError) as exc:
                self._json(409, {"error": friendly(exc, _secret())})
                return
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/images/review":
            try:
                resume_images(data)
            except (ValueError, OSError) as exc:
                self._json(409, {"error": friendly(exc, _secret())})
                return
            self._json(202, {"ok": True})
            return
        if parsed.path == "/api/proceed":
            mode = data.get("inspection_mode", "manual")
            if mode not in ("manual", "auto"):
                self._json(400, {"error": "Inspection mode must be manual or auto."})
                return
            try:
                proceed_saved(bool(data.get("dry_run")), mode)
            except (ValueError, OSError) as exc:
                self._json(409, {"error": friendly(exc, _secret())})
                return
            self._json(202, {"ok": True})
            return
        idea = data.get("idea") if isinstance(data.get("idea"), str) else ""
        if parsed.path == "/api/brief/suggest":
            film = {key: data.get(key) for key in ("runtime_seconds", "aspect_ratio", "max_shots")
                    if (type(data.get(key)) is int and data.get(key) > 0) or data.get(key) in ("16:9", "9:16", "1:1")}
            references = data.get("references")
            self._suggest(idea, film, references if isinstance(references, list) and references else None)
            return
        max_shots = data.get("max_shots")
        if max_shots is not None and (type(max_shots) is not int or not 1 <= max_shots <= 240):
            self._json(400, {"error": "Shot limit must be an integer between 1 and 240."})
            return
        runtime, ratio = data.get("runtime_seconds"), data.get("aspect_ratio")
        if runtime is not None and (type(runtime) is not int or not 6 <= runtime <= 720):
            self._json(400, {"error": "Runtime must be a whole number of seconds between 6 and 720."})
            return
        if ratio is not None and ratio not in ("16:9", "9:16", "1:1"):
            self._json(400, {"error": "Aspect ratio must be 16:9, 9:16, or 1:1."})
            return
        if runtime is not None and max_shots is not None:
            try:
                shot_budget(runtime, max_shots, studio_settings.load()["video"]["selection"])
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
        try:
            image_count = parse_image_count(data.get("image_count"))
            references = parse_references(data.get("references"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        mode = data.get("inspection_mode", "manual")
        if mode not in ("manual", "auto"):
            self._json(400, {"error": "Inspection mode must be manual or auto."})
            return
        kwargs = {}
        if image_count is not None:
            kwargs["image_count"] = image_count
        if references:
            kwargs["references"] = references
        if data.get("approval") is True:
            kwargs["approval"] = True
        if runtime is not None:
            kwargs["runtime_seconds"] = runtime
        if ratio is not None:
            kwargs["aspect_ratio"] = ratio
        if not idea.strip():
            self._json(400, {"error": "Write an idea first, or use Suggest a story."})
            return
        try:
            start_crew(idea.strip(), bool(data.get("pages_only")), bool(data.get("dry_run")), max_shots, mode, **kwargs)
        except ValueError as exc:
            self._json(409, {"error": friendly(exc, _secret())})
            return
        self._json(202, {"ok": True})

    def _local_request(self, mutating: bool = False) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self._json(403, {"error": "Use the local desk address."})
            return False
        origin = self.headers.get("Origin")
        if (origin and origin != f"http://{host}") or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._json(403, {"error": "Cross-origin access is not allowed."})
            return False
        if mutating and self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Use application/json."})
            return False
        return True

    def _read_json(self, limit: int | None = None) -> dict | None:
        cap = _MAX_BODY if limit is None else limit
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json(400, {"error": "expected JSON"})
            return None
        if length < 0 or length > cap:
            self._json(413, {"error": "brief is too long" if limit is None else "upload is too large"})
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "expected JSON"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "expected JSON"})
            return None
        return data

    def _suggest(self, idea: str, film: dict | None = None, references: list | None = None) -> None:
        """Fill the brief box. Does not start the crew."""
        options = {key: value for key, value in (("film", film), ("references", references)) if value}
        try:
            brief = suggest_brief(idea, **options)
        except BriefSuggestError as exc:
            self._json(503, {"error": str(exc)})
            return
        except Exception:
            traceback.print_exc()
            self._json(503, {"error": "Could not write a brief."})
            return
        self._json(200, {"brief": brief})

    def _file(self, path: str) -> None:
        file = library_file(path)
        if file is None:
            self._json(404, {"error": "That file is not in a production folder."})
            return
        label = file.relative_to(PRODUCTIONS.resolve()).as_posix()
        if file.suffix.lower() in IMAGE_SUFFIXES:
            self._json(
                200,
                {
                    "kind": "image",
                    "name": label,
                    "src": "/library-media?path=" + urllib.parse.quote(path, safe=""),
                },
            )
            return
        self._json(200, {"kind": "text", "name": label, "text": library_text(file)})

    def _media(self, image_id: str, version: str = "") -> None:
        file = _media_for(image_id, version)
        if file is None:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        self._send(200, file.read_bytes(), _content_type(file))

    def _library_media(self, path: str) -> None:
        file = library_file(path)
        if file is None or file.suffix.lower() not in IMAGE_SUFFIXES:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        self._send(200, file.read_bytes(), _content_type(file))

    def _library_thumb(self, path: str) -> None:
        """Pictures in lists load a small cached preview instead of the multi-megabyte original."""
        file = library_file(path)
        if file is None or file.suffix.lower() not in _REFERENCE_SUFFIXES:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            stat = file.stat()
            key = (str(file), stat.st_mtime_ns, stat.st_size)
            with _thumb_lock:
                body = _thumbs.get(key)
            if body is None:
                body = thumbnail(file.read_bytes())
                with _thumb_lock:
                    if len(_thumbs) >= 256:
                        _thumbs.pop(next(iter(_thumbs)))
                    _thumbs[key] = body
        except (OSError, ValueError):
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        self._send(200, body, "image/jpeg")

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = -1
        try:
            while True:
                revision = hub.wait(seen, 12)
                if revision == seen:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                seen = revision
                payload = json.dumps(public_view(), separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class DeskServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def bind(port: int) -> DeskServer:
    """Local desk only. Remote access requires a separately authenticated gateway."""
    return DeskServer(("127.0.0.1", port), DeskHandler)


def _open_browser(url: str) -> None:
    """Open a window on this display. xdg-open hangs on the xfce helper and never shows the page."""
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if chrome and os.environ.get("DISPLAY"):
        subprocess.Popen(
            [chrome, "--new-window", url, "--no-sandbox", "--disable-dev-shm-usage"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    webbrowser.open(url)


def serve(port: int = 7860, open_browser: bool = True) -> None:
    httpd = None
    last_error: OSError | None = None
    for candidate in range(port, port + 10):
        try:
            httpd = bind(candidate)
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        raise SystemExit(f"Could not open the desk: {last_error}")
    bound = httpd.server_address[1]
    local = f"http://127.0.0.1:{bound}"
    print(f"Screenwriter desk at {local}", flush=True)
    url = local
    if open_browser:
        _open_browser(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDesk closed.")
    finally:
        httpd.server_close()
