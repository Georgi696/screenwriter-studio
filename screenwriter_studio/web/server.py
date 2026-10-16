"""Local production desk: static assets, HTTP routes, and server-sent events.

The browser patches crew, activity, and image nodes from structured snapshots.
No frontend build or third-party web framework is required.
"""

from __future__ import annotations

import asyncio
import json
import re
import os
import shutil
import subprocess
import threading
import time
import logging
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from screenwriter_studio.fsio import atomic_write_json
from screenwriter_studio import runlog
from screenwriter_studio.web import library
from screenwriter_studio.web.library import (
    _REFERENCE_SUFFIXES,
    _safe_image,
    _under,
    _version_label,
    can_proceed,
    cost_view,
    library_catalog,
    library_file,
    library_text,
    parse_image_count,
    parse_references,
    production_path,
    production_view,
    save_reference_upload,
    saved_verdict,
    shot_plan,
)
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
    EDIT_MODEL,
    GPT_IMAGE_MODEL,
    IMAGE_MODEL_BY_KIND,
    NANO_BANANA_MODEL,
    SEEDREAM_MODEL,
)
from screenwriter_studio.studio import ScreenwriterStudio, story_unfinished
from screenwriter_studio.images.generator import run_batch
from screenwriter_studio.images.upload import thumbnail
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

log = logging.getLogger("screenwriter_studio")
STATIC = Path(__file__).resolve().parent / "static"
PAGE = STATIC / "desk.html"
DEFAULT_IDEA = "A 30-second vertical film about someone who keeps a seat on the last bus."
_MAX_BODY = 100_000
_MAX_UPLOAD_BODY = 12_000_000
_thumbs: dict[tuple, bytes] = {}
_thumb_lock = threading.Lock()
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
            folder.resolve().relative_to(library.PRODUCTIONS.resolve())
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
    folder = production_path(str(library.PRODUCTIONS / slug))
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
    """The page's snapshot. The board is copied under the lock; every file read happens after it
    is released, so a slow disk never blocks the crew thread that is publishing the next event."""
    with _lock:
        secret = _secret()
        payload = client_payload(_board, secret)
        raw_images = [dict(raw) for raw in _board.images]
        folder_label = _board.folder
        running = _board.running
    folder = production_path(folder_label)
    images = []
    for image, raw in zip(payload["images"], raw_images):
        item = dict(image)
        path = str(raw.get("path") or "")
        if raw.get("state") in {"done", "review", "inspecting"} and _safe_image(path, folder_label) is not None:
            item["src"] = "/media?id=" + urllib.parse.quote(str(raw.get("id") or ""), safe="") + "&v=" + str(raw.get("repairs", 0))
        else:
            item["src"] = ""
        image_id = urllib.parse.quote(str(raw.get("id") or ""), safe="")
        item["versions"] = [
            {"name": Path(entry["path"]).name,
             "src": "/media?id=" + image_id + "&version=" + urllib.parse.quote(Path(entry["path"]).name, safe=""),
             "label": _version_label(entry)}
            for entry in versions(raw) if _safe_image(str(entry["path"]), folder_label) is not None]
        item["uncertain"] = raw.get("state") == "submitting" and not raw.get("task_id") and not running
        item["edited"] = isinstance(raw.get("job_override"), dict)
        images.append(item)
    payload["boot"] = BOOT
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
    payload.update(shot_plan(folder))
    payload["can_proceed"] = (not running) and can_proceed(folder_label)
    payload["can_resume"] = (not running) and folder is not None and story_unfinished(folder)
    payload["pages_passed"] = saved_verdict(folder_label)
    payload.update(cost_view(folder, payload["can_proceed"]))
    payload["production"] = production_view(folder)
    prompts = payload.pop("image_prompts", {})
    references = payload.pop("image_references", {})
    for item, raw in zip(payload["images"], raw_images):
        override = raw.get("job_override") if isinstance(raw.get("job_override"), dict) else {}
        item["prompt"] = scrub(str(override.get("prompt") or prompts.get(str(raw.get("id") or ""), "")), secret)
        item["references"] = references.get(str(raw.get("id") or ""), [])
    payload["video"] = json.loads(scrub(json.dumps(video_status(folder)), secret))
    return payload


_COALESCE_SECONDS = 0.05
_view_cache: tuple[int, bytes] = (-1, b"")
_view_lock = threading.Lock()


def view_bytes(revision: int) -> bytes:
    """The serialized snapshot for a board revision, built once and shared by every open page."""
    global _view_cache
    with _view_lock:
        if _view_cache[0] != revision:
            _view_cache = (revision, json.dumps(public_view(), separators=(",", ":")).encode("utf-8"))
        return _view_cache[1]



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
    root = library.PRODUCTIONS.resolve() if library.PRODUCTIONS.is_dir() else None

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
            log.exception("Desk request or run failed")
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
    atomic_write_json(out / "manifest.json", saved)
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
        if not _board.folder or not _under(out, library.PRODUCTIONS):
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
        atomic_write_json(folder / "aborted.json", {"aborted": True})
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


async def _drive_resume(serial: int, folder: Path, cancel: threading.Event) -> None:
    studio = ScreenwriterStudio()
    await _consume(serial, studio.resume(folder, cancel=cancel))


def resume_story() -> None:
    """Retry a story run that stopped at the screenwriter or editor. Development is not paid for again."""
    global _run_serial, _cancel
    _ensure_loop()
    with _lock:
        if _board.running:
            raise ValueError("Wait for the current run to finish before retrying.")
        folder = production_path(_board.folder)
        if folder is None or not story_unfinished(folder):
            raise ValueError("This production has nothing to retry.")
        previous_cancel = _cancel
        previous_future = _future
        cancel = threading.Event()
        _cancel = cancel
        _run_serial += 1
        serial = _run_serial
        _board.running = True
        _board.note = "Retrying from the saved development."
        _replace(_board)
    _cut(previous_cancel, previous_future)
    _commit_future(serial, cancel, _drive_resume(serial, folder, cancel))


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
        log.exception("Desk request or run failed")
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


def parse_run_request(data: dict) -> dict:
    """Validate a /api/run body into start_crew arguments. Raises ValueError with the sentence to show."""
    idea = data.get("idea") if isinstance(data.get("idea"), str) else ""
    max_shots = data.get("max_shots")
    if max_shots is not None and (type(max_shots) is not int or not 1 <= max_shots <= 240):
        raise ValueError("Shot limit must be an integer between 1 and 240.")
    runtime, ratio = data.get("runtime_seconds"), data.get("aspect_ratio")
    if runtime is not None and (type(runtime) is not int or not 6 <= runtime <= 720):
        raise ValueError("Runtime must be a whole number of seconds between 6 and 720.")
    if ratio is not None and ratio not in ("16:9", "9:16", "1:1"):
        raise ValueError("Aspect ratio must be 16:9, 9:16, or 1:1.")
    if runtime is not None and max_shots is not None:
        shot_budget(runtime, max_shots, studio_settings.load()["video"]["selection"])
    image_count = parse_image_count(data.get("image_count"))
    references = parse_references(data.get("references"))
    mode = data.get("inspection_mode", "manual")
    if mode not in ("manual", "auto"):
        raise ValueError("Inspection mode must be manual or auto.")
    if not idea.strip():
        raise ValueError("Write an idea first, or use Suggest a story.")
    request: dict = {"idea": idea.strip(), "pages_only": bool(data.get("pages_only")),
                     "dry_run": bool(data.get("dry_run")), "max_shots": max_shots, "inspection_mode": mode}
    if image_count is not None:
        request["image_count"] = image_count
    if references:
        request["references"] = references
    if data.get("approval") is True:
        request["approval"] = True
    if runtime is not None:
        request["runtime_seconds"] = runtime
    if ratio is not None:
        request["aspect_ratio"] = ratio
    return request


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
            path = video_file(library.PRODUCTIONS, query.get("path", [""])[0])
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
            log.exception("Desk request or run failed")
            try:
                self._json(500, {"error": friendly(exc, _secret())})
            except OSError:
                pass

    # path -> (handler, body). body None reads nothing, "json" a small body, "upload" a large one.
    _POST_ROUTES = {
        "/api/log/clear": ("_post_log_clear", None),
        "/api/session/clear": ("_post_session_clear", None),
        "/api/stop": ("_post_stop", None),
        "/api/abort-draft": ("_post_abort_draft", None),
        "/api/references": ("_post_reference_upload", "upload"),
        "/api/settings": ("_post_settings", "json"),
        "/api/video/forget": ("_post_video_forget", "json"),
        "/api/production/open": ("_post_production_open", "json"),
        "/api/video": ("_post_video", "json"),
        "/api/video/take": ("_post_video_take", "json"),
        "/api/images/version": ("_post_image_action", "json"),
        "/api/images/forget": ("_post_image_action", "json"),
        "/api/images/attach": ("_post_image_action", "json"),
        "/api/images/review": ("_post_images_review", "json"),
        "/api/proceed": ("_post_proceed", "json"),
        "/api/resume": ("_post_resume", "json"),
        "/api/brief/suggest": ("_post_brief_suggest", "json"),
        "/api/run": ("_post_run", "json"),
    }

    def _post(self) -> None:
        if not self._local_request(mutating=True):
            return
        route = self._POST_ROUTES.get(urllib.parse.urlparse(self.path).path)
        if route is None:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        handler, body = route
        data: dict | None = {}
        if body is not None:
            data = self._read_json(_MAX_UPLOAD_BODY if body == "upload" else None)
            if data is None:
                return
        getattr(self, handler)(data)

    def _act(self, action, ok: int = 200, errors=(ValueError, OSError)) -> None:
        """Run action; a refusal becomes a 409 with a one-line reason the desk can show."""
        try:
            action()
        except errors as exc:
            self._json(409, {"error": friendly(exc, _secret())})
            return
        self._json(ok, {"ok": True})

    def _post_log_clear(self, data: dict) -> None:
        clear_log()
        self._json(200, {"ok": True})

    def _post_session_clear(self, data: dict) -> None:
        clear_session()
        self._json(200, {"ok": True})

    def _post_stop(self, data: dict) -> None:
        stop_run()
        self._json(200, {"ok": True})

    def _post_abort_draft(self, data: dict) -> None:
        try:
            abort_draft()
        except ValueError as exc:
            self._json(409, {"error": scrub(str(exc), _secret())})
            return
        self._json(200, {"ok": True})

    def _post_reference_upload(self, data: dict) -> None:
        try:
            saved = save_reference_upload(data.get("filename"), data.get("data"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, saved)

    def _post_settings(self, data: dict) -> None:
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

    def _post_video_forget(self, data: dict) -> None:
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

    def _post_production_open(self, data: dict) -> None:
        self._act(lambda: open_production(data.get("slug")), errors=(ValueError,))

    def _post_video(self, data: dict) -> None:
        self._act(lambda: start_video(data), ok=202)

    def _post_video_take(self, data: dict) -> None:
        self._act(lambda: video_take(data.get("shot"), data.get("take")))

    def _post_image_action(self, data: dict) -> None:
        self._act(lambda: image_action(urllib.parse.urlparse(self.path).path.rsplit("/", 1)[1], data))

    def _post_images_review(self, data: dict) -> None:
        self._act(lambda: resume_images(data), ok=202)

    def _post_proceed(self, data: dict) -> None:
        mode = data.get("inspection_mode", "manual")
        if mode not in ("manual", "auto"):
            self._json(400, {"error": "Inspection mode must be manual or auto."})
            return
        self._act(lambda: proceed_saved(bool(data.get("dry_run")), mode), ok=202)

    def _post_resume(self, data: dict) -> None:
        self._act(resume_story, ok=202)

    def _post_brief_suggest(self, data: dict) -> None:
        idea = data.get("idea") if isinstance(data.get("idea"), str) else ""
        film = {key: data.get(key) for key in ("runtime_seconds", "aspect_ratio", "max_shots")
                if (type(data.get(key)) is int and data.get(key) > 0) or data.get(key) in ("16:9", "9:16", "1:1")}
        references = data.get("references")
        avoid = data.get("avoid")
        avoid = [text for text in avoid if isinstance(text, str)][:3] if isinstance(avoid, list) else None
        self._suggest(idea, film, references if isinstance(references, list) and references else None, avoid)

    def _post_run(self, data: dict) -> None:
        try:
            request = parse_run_request(data)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        head = [request.pop(name) for name in ("idea", "pages_only", "dry_run", "max_shots", "inspection_mode")]
        self._act(lambda: start_crew(*head, **request), ok=202, errors=(ValueError,))

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

    def _suggest(self, idea: str, film: dict | None = None, references: list | None = None, avoid: list | None = None) -> None:
        """Fill the brief box. Does not start the crew."""
        options = {key: value for key, value in (("film", film), ("references", references), ("avoid", avoid)) if value}
        try:
            brief = suggest_brief(idea, **options)
        except BriefSuggestError as exc:
            self._json(503, {"error": str(exc)})
            return
        except Exception:
            log.exception("Desk request or run failed")
            self._json(503, {"error": "Could not write a brief."})
            return
        self._json(200, {"brief": brief})

    def _file(self, path: str) -> None:
        file = library_file(path)
        if file is None:
            self._json(404, {"error": "That file is not in a production folder."})
            return
        label = file.relative_to(library.PRODUCTIONS.resolve()).as_posix()
        if file.suffix.lower() in library.IMAGE_SUFFIXES:
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
        if file is None or file.suffix.lower() not in library.IMAGE_SUFFIXES:
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
                # A burst of image events becomes one push: wait a beat, then send the latest state.
                time.sleep(_COALESCE_SECONDS)
                revision = hub.revision
                seen = revision
                payload = view_bytes(revision)
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
        # Chrome refuses to start as root without --no-sandbox (containers); never drop it for a normal user.
        flags = ["--no-sandbox", "--disable-dev-shm-usage"] if os.geteuid() == 0 else []
        subprocess.Popen(
            [chrome, "--new-window", url, *flags],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    webbrowser.open(url)


def serve(port: int = 7860, open_browser: bool = True) -> None:
    runlog.configure_logging()
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
