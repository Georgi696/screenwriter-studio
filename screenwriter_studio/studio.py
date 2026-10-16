"""
Screenwriter studio — orchestrates the crew.

Pattern: orchestrator-worker, with an evaluator loop on the pages
before any image credit is spent.

  1. Development locks logline, beats, style bible     — gpt-6-astra
  2. Screenwriter writes Fountain and the shot list    — gpt-6-astra
  3. Script editor passes or sends it back             — gpt-6-1-sol
  4. Art director writes still jobs                    — gpt-6-astra
  5. Stills calls KIE.ai and saves productions/<slug>/images

Every agent authenticates with KIE_API_KEY.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import unicodedata
import queue
import random
import re
import shutil
import threading
from pathlib import Path

from agents import Runner, set_tracing_disabled
from agents.exceptions import ModelBehaviorError
from dotenv import load_dotenv
from pydantic import ValidationError

from screenwriter_studio.crew.art import art_agent
from screenwriter_studio.budget import shot_budget
from screenwriter_studio.videos.catalog import choose_model, clip_bounds
from screenwriter_studio import runlog
from screenwriter_studio.errors import provider_down, status_code
from screenwriter_studio.events import StudioEvent
from screenwriter_studio.fsio import atomic_write_json, atomic_write_text
from screenwriter_studio.crew.development import development_agent
from screenwriter_studio.crew.editor import editor_agent
from screenwriter_studio.playbooks import CAMERA_COVERAGE, PLAYBOOKS
from screenwriter_studio.paths import WORKSPACE_ROOT
from screenwriter_studio.images.upload import SOURCE_LIMIT, suffix_matches
from screenwriter_studio.pricing import production_estimate
from screenwriter_studio.models import load_kie_api_key
from screenwriter_studio.settings import chat_agent, load as load_settings
from screenwriter_studio.references import (
    REFERENCE_MODES, covered_sheets, reference_choices, reference_direction, story_role, validate_reference_shots,
)
from screenwriter_studio.schemas import Development, Screenplay, ScriptVerdict, StillPackage
from screenwriter_studio.images.service import validate_still_package, render_stills
from screenwriter_studio.crew.writer import writer_agent

load_dotenv(override=False)
set_tracing_disabled(True)
log = logging.getLogger("screenwriter_studio")

MAX_REWRITES = 2
MAX_ART_REPAIRS = 1
# Seconds to wait before each retry when KIE's chat gateway answers 429/5xx (about two minutes in all).
OUTAGE_WAITS = (5, 20, 45)
_STOP_TEXT = "Generation stopped. Saved images can be resumed.\n\n"
_MAX_REFERENCE_BYTES = SOURCE_LIMIT
_REFERENCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class _GenerationStopped(Exception):
    """The desk set the stills cancel event. Crew steps after this must not start."""


def _stop_event(cancel: threading.Event | None) -> StudioEvent | None:
    if cancel is not None and cancel.is_set():
        return StudioEvent("failed", _STOP_TEXT)
    return None


def _raise_if_stopped(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _GenerationStopped()


def _still_line(record: dict) -> str:
    kind = record.get("kind") or "still"
    if record.get("phase") == "start":
        return f"**Still** {record['id']} ({kind}) started on {record.get('model')}.\n\n"
    if record.get("dry_run"):
        return f"- {record['id']} ({kind}) planned on {record.get('model')}; no image generated.\n\n"
    if record.get("state") == "review":
        return f"- {record['id']}: awaiting review — {record.get('inspection', {}).get('summary', '')}\n\n"
    if record.get("state") == "blocked":
        return f"- {record['id']}: waiting for approved references.\n\n"
    if record.get("error"):
        return f"- {record['id']} ({kind}) failed: {record['error']}\n\n"
    return f"- {record['id']} ({kind}) — {record.get('model')} — `{record.get('path')}`\n\n"


async def _stream_stills(package: StillPackage, images: Path, *, dry_run: bool, inspection_mode="manual", inspection_context=None, cancel: threading.Event | None = None, locked_references=None):
    """Yield each still as it starts and again when the file is ready."""
    events: queue.Queue[dict] = queue.Queue()
    if cancel is None:
        cancel = threading.Event()

    def on_start(record: dict) -> None:
        events.put({**record, "phase": "start"})

    def on_record(record: dict) -> None:
        events.put({**record, "phase": "done"})

    task = asyncio.create_task(
        asyncio.to_thread(
            render_stills,
            package,
            images,
            dry_run=dry_run,
            inspection_mode=inspection_mode,
            inspection_context=inspection_context,
            on_record=on_record,
            on_start=on_start,
            cancel=cancel,
            locked_references=locked_references,
        )
    )
    try:
        while not task.done():
            while True:
                try:
                    record = events.get_nowait()
                except queue.Empty:
                    break
                yield StudioEvent("image", _still_line(record), image=record)
            await asyncio.sleep(0.2)
        while True:
            try:
                record = events.get_nowait()
            except queue.Empty:
                break
            yield StudioEvent("image", _still_line(record), image=record)
        manifest = task.result()
    except (asyncio.CancelledError, GeneratorExit):
        cancel.set()
        task.cancel()
        raise
    except (RuntimeError, ValueError, SystemExit) as exc:
        yield StudioEvent("failed", f"**Stills stopped:** {exc}\n\n")
        return

    if manifest.get("status") == "paused":
        yield StudioEvent("complete", "Stills paused for image review. Open Stills to see findings and approve changes.\n\n")
        return
    failed = sum(bool(record.get("error")) for record in manifest["images"])
    if failed:
        yield StudioEvent("failed", f"{failed} still job(s) failed. Resume the saved jobs to retry.\n\n")
        return
    label = "Dry run: models chosen, no images downloaded." if dry_run else "Stills are in `images/`."
    yield StudioEvent("complete", label + "\n\n")


def _folder_label(folder: Path) -> str:
    try:
        return folder.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    except ValueError:
        return folder.as_posix()


def _image_work_started(folder: Path) -> bool:
    images = folder / "images"
    return (images / "jobs.json").is_file() or (images / "manifest.json").is_file()


def _load_saved(folder: Path) -> tuple[Development, Screenplay, ScriptVerdict]:
    try:
        development = Development.model_validate_json((folder / "00_development.json").read_text(encoding="utf-8"))
        screenplay = Screenplay.model_validate_json((folder / "01_screenplay.json").read_text(encoding="utf-8"))
        verdict = ScriptVerdict.model_validate_json((folder / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise ValueError("This folder does not have a saved development, screenplay, and verdict.") from exc
    return development, screenplay, verdict


def _slug(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if not cleaned and value.strip():
        # A Cyrillic or CJK title has no ASCII letters; keep productions apart anyway.
        cleaned = "film-" + hashlib.sha256(value.strip().encode()).hexdigest()[:8]
    return (cleaned or "untitled")[:48]


def _under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _reference_source(relative: str) -> Path:
    raw = (relative or "").strip()
    if not raw or raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
        raise ValueError("Reference image is not in a production folder.")
    root = (WORKSPACE_ROOT / "productions").resolve()
    file = (root / raw).resolve()
    if not file.is_file() or not _under_root(file, root):
        raise ValueError("Reference image is not in a production folder.")
    suffix = file.suffix.lower()
    if suffix not in _REFERENCE_SUFFIXES:
        raise ValueError("Reference image must be a PNG, JPEG, or WebP.")
    try:
        size = file.stat().st_size
        header = file.read_bytes()[:12]
    except OSError as exc:
        raise ValueError("Reference image is not available.") from exc
    if size <= 0 or size > _MAX_REFERENCE_BYTES or not suffix_matches(suffix, header):
        raise ValueError("Reference image must be a PNG, JPEG, or WebP within the size limit.")
    return file


def _plan_references(choices: list[dict]) -> list[dict]:
    planned = []
    seen: set[str] = set()
    counts = {mode: 0 for mode in REFERENCE_MODES}
    for choice in choices:
        relative, mode = choice["path"], choice["mode"]
        if relative in seen:
            continue
        seen.add(relative)
        source = _reference_source(relative)
        counts[mode] += 1
        prefix = "ref" if mode == "lock" else mode
        planned.append({
            **{key: value for key, value in choice.items() if key != "path"},
            "id": f"{prefix}-{counts[mode]:02d}",
            "name": choice.get("name") or source.name,
            "source": relative,
            "source_path": source,
            "mode": mode,
        })
    return planned


def _copy_references(folder: Path, planned: list[dict]) -> list[dict]:
    """Copy locked stills into the new production. The source files stay where they are."""
    if not planned:
        return []
    root = (WORKSPACE_ROOT / "productions").resolve()
    folder_resolved = folder.resolve()
    if not _under_root(folder_resolved, root):
        raise ValueError("Reference image is outside the workspace.")
    dest_root = (folder_resolved / "references").resolve()
    if not _under_root(dest_root, folder_resolved):
        raise ValueError("Reference image is outside the workspace.")
    dest_root.mkdir(parents=True, exist_ok=True)
    stored = []
    for item in planned:
        source: Path = item["source_path"]
        dest = (dest_root / f"{item['id']}{source.suffix.lower()}").resolve()
        if not _under_root(dest, dest_root):
            raise ValueError("Reference image is outside the workspace.")
        if dest.exists():
            raise ValueError("Reference destination already exists.")
        shutil.copyfile(source, dest)
        stored.append({
            **{key: value for key, value in item.items() if key != "source_path"},
            "id": item["id"],
            "name": item["name"],
            "source": item["source"],
            "path": str(dest),
            "mode": item.get("mode", "lock"),
        })
    return stored


def _locked(references: list[dict] | None) -> list[dict]:
    return [item for item in references or [] if item.get("mode", "lock") == "lock"]


def _styles(references: list[dict] | None) -> list[dict]:
    return [item for item in references or [] if item.get("mode") == "style"]


STORY_NAMING = (
    "Name a referenced character exactly as its reference. Begin the location a Location reference "
    "shows with the reference's name. The studio attaches each picture and its image directions itself: in "
    "character and location locks, write only what the picture does not show and what the story adds (time, "
    "weather, dirt, injuries, removed or added clothing). Do not mention reference ids and do not write "
    "instructions about copying, keeping or preserving a reference."
)


def _reference_note(references: list[dict] | None, story: bool = False) -> str:
    """References as listed to the crew. The story crew (story=True) gets roles, not image directions."""
    lines = []
    locked = _locked(references)
    if locked:
        lines += [
            "Locked visual references. Keep these faces, costumes, and places in the shots where they appear.",
            "This is a different plot, not a rewrite of the previous story.",
            "Do not replace the earlier production.",
        ]
        lines += [f"- {item['id']}: {item.get('name') or item.get('source')}" for item in locked]
    styles = _styles(references)
    if styles:
        lines += [
            "Style references. They set the look only: colour grade, lighting, lens, texture, and finish.",
            "They do not fix the story, the cast, the wardrobe, or the places.",
        ]
        lines += [f"- {item['id']}: {item.get('name') or item.get('source')}" for item in styles]
    for item in references or []:
        scope = "Only shots " + ", ".join(str(n) for n in item["shots"]) if item.get("shots") else "Relevant shots automatically"
        if story:
            lines.append(f"- {item.get('name') or item['id']}: {story_role(item)} Applies to: {scope}.")
        else:
            lines.append(f"- {item['id']} ({item.get('name', '')}): {reference_direction(item)} Applies to: {scope}.")
    if story and references:
        lines.append(STORY_NAMING)
    return "\n".join(lines)


_SETTINGS = "run_settings.json"


def _save_settings(folder: Path, *, image_count: int | None, references: list[dict], brief: str = "",
                   flow: dict | None = None) -> None:
    """What proceed() and resume() need to continue the same run after a pause or a failure."""
    payload = {"image_count": image_count, "references": references, "brief": brief, "flow": flow or {}}
    atomic_write_json(folder / _SETTINGS, payload)


def _saved_flow(folder: Path) -> dict:
    """The options the run started with, so resuming it behaves the same way."""
    try:
        data = json.loads((folder / _SETTINGS).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    flow = data.get("flow") if isinstance(data, dict) else None
    flow = flow if isinstance(flow, dict) else {}
    mode = flow.get("inspection_mode")
    return {"pages_only": flow.get("pages_only") is True, "approval": flow.get("approval", True) is not False,
            "dry_run": flow.get("dry_run") is True, "inspection_mode": mode if mode in {"manual", "auto"} else "manual"}


def story_unfinished(folder: Path) -> bool:
    """True when development is saved but the screenwriter/editor loop never reached a verdict."""
    if not (folder / "00_development.json").is_file() or (folder / "aborted.json").is_file() or _image_work_started(folder):
        return False
    return not ((folder / "01_screenplay.json").is_file() and (folder / "verdict.json").is_file())


def _saved_brief(folder: Path) -> str:
    try:
        data = json.loads((folder / _SETTINGS).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("brief") or "") if isinstance(data, dict) else ""


def _reusable_plan(folder: Path, development: Development, screenplay: Screenplay,
                   image_count: int | None, reference_ids: list[str]) -> StillPackage | None:
    """A valid image plan saved by an earlier attempt that stopped before any image was made."""
    path = folder / "03_still_plan.json"
    if not path.is_file() or _image_work_started(folder):
        return None
    try:
        package = StillPackage.model_validate_json(path.read_text(encoding="utf-8"))
        validate_still_package(package, development, screenplay, image_count=image_count, reference_ids=reference_ids)
    except (OSError, ValueError, ValidationError):
        return None
    return package


def _playbook(job_type: str) -> str:
    """Match a free-text job type ("Vertical social ad") to a playbook instead of silently using narrative."""
    text = (job_type or "").lower()
    for name in ("vertical", "ad", "explainer", "trailer", "sketch", "narrative"):
        if re.search(rf"\b{name}\b", text):
            return PLAYBOOKS[name]
    return PLAYBOOKS["narrative"]


def video_feasibility(development: Development, screenplay: Screenplay, *, image_count: int | None = None,
                      locked_count: int = 0, values: dict | None = None) -> list[str]:
    """Reasons one clip per shot would be impossible with the current settings.

    Checked before the art director and any image is paid for: shot lengths and the
    runtime, a video model for every shot at the chosen quality, sound and language,
    and an image budget large enough for one image per shot plus its references.
    """
    video = (values or load_settings())["video"]
    issues = _script_issues(development, screenplay, video["selection"])
    for shot in screenplay.shots:
        try:
            choose_model(development, shot, mode=video["mode"], sound=video["sound"], selection=video["selection"])
        except ValueError as exc:
            issues.append(str(exc))
    if image_count is not None:
        needed = len(screenplay.shots) + max(0, len(development.characters) + len(development.locations) - locked_count)
        if image_count < needed:
            issues.append(
                f"Images to generate is {image_count}, but {len(screenplay.shots)} shot images plus character and "
                f"location sheets need about {needed}. Every shot needs its own image to become a clip."
            )
    return issues


def _covered(development: Development, references: list[dict] | None) -> int:
    """Character and location sheets already shown by a reference, so not counted as images to make."""
    return covered_sheets(references, [c.name for c in development.characters], list(development.locations))


def _estimate_line(development: Development, screenplay: Screenplay, image_count: int | None,
                   references: list[dict] | None) -> str:
    """The credit estimate for the images and clips that approving these pages leads to."""
    locked_count = _covered(development, references)
    try:
        estimate = production_estimate(development, screenplay, values=load_settings(),
                                       image_count=image_count, locked_count=locked_count)
    except (KeyError, TypeError, ValueError):
        return ""
    return estimate["text"] + "\n\n"


def _load_settings(folder: Path) -> tuple[int | None, list[dict]]:
    try:
        data = json.loads((folder / _SETTINGS).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, []
    if not isinstance(data, dict):
        return None, []
    count = data.get("image_count")
    if type(count) is not int or not 1 <= count <= 240:
        count = None
    references = []
    root = (folder / "references").resolve()
    for item in data.get("references") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("path"), str):
            continue
        if not _under_root(Path(item["path"]), root) or not Path(item["path"]).is_file():
            continue
        mode = item.get("mode") if item.get("mode") in REFERENCE_MODES else "lock"
        references.append({**item, "mode": mode})
    return count, references


def _project_dir(slug: str) -> Path:
    base = WORKSPACE_ROOT / "productions" / _slug(slug)
    occupied = base.exists() and any(base.iterdir())
    if not occupied:
        return base
    number = 2
    while True:
        candidate = WORKSPACE_ROOT / "productions" / f"{_slug(slug)}-{number}"
        if not candidate.exists() or not any(candidate.iterdir()):
            return candidate
        number += 1


class ScreenwriterStudio:
    def __init__(self) -> None:
        self._folder: Path | None = None
        self._pending: list[dict] = []

    def _bind(self, folder: Path) -> None:
        """Calls made before the production folder existed are written to its run log now."""
        self._folder = folder
        for entry in self._pending:
            runlog.append(folder, entry)
        self._pending = []

    def _note(self, entry: dict) -> None:
        if self._folder is None:
            self._pending.append(entry)
        else:
            runlog.append(self._folder, entry)

    async def _call(self, role: str, agent, message: str, output, *, max_turns: int):
        """One crew call. An unusable reply gets one more try that shows the model what was wrong.

        Every attempt is logged with its model, tokens and time, so spend is visible per role.
        """
        chosen = chat_agent(role, agent)
        model = str(getattr(chosen.model, "model", chosen.model))
        prompt = message
        outages = 0
        attempt = 0
        while attempt < 2:
            attempt += 1
            started = time.monotonic()
            entry = {"role": role, "model": model, "attempt": attempt}
            try:
                result = await Runner.run(chosen, prompt, max_turns=max_turns)
                value = result.final_output_as(output)
            except (ModelBehaviorError, ValidationError) as exc:
                reason = " ".join(str(exc).split())[:400]
                self._note({**entry, "outcome": "invalid_output", "error": reason,
                            "seconds": round(time.monotonic() - started, 2)})
                if attempt == 2:
                    raise
                log.warning("%s reply was unusable (%s); asking once more", role, reason)
                prompt = (message + "\n\nYour previous reply could not be used: " + reason
                          + "\nReply again with output that matches the required schema exactly.")
                continue
            except Exception as exc:
                self._note({**entry, "outcome": "error", "error": str(exc)[:300],
                            "seconds": round(time.monotonic() - started, 2)})
                if provider_down(exc) and outages < len(OUTAGE_WAITS):
                    # KIE's gateway failed or is rate-limiting. Nothing was produced, so wait and ask again.
                    wait = OUTAGE_WAITS[outages] * random.uniform(0.7, 1.3)
                    outages += 1
                    log.warning("%s: provider unavailable (%s); retry %d in %.0fs", role, status_code(exc), outages, wait)
                    await asyncio.sleep(wait)
                    attempt -= 1  # an outage does not use up the one retry for an unusable reply
                    continue
                raise
            self._note({**entry, "outcome": "ok", "seconds": round(time.monotonic() - started, 2),
                        **runlog.usage_of(result)})
            return value
        raise AssertionError("unreachable")  # pragma: no cover

    async def run(
        self,
        idea: str,
        *,
        pages_only: bool = False,
        dry_run: bool = False,
        aspect_ratio: str | None = None,
        runtime_seconds: int | None = None,
        max_shots: int | None = None,
        inspection_mode: str = "manual",
        cancel: threading.Event | None = None,
        image_count: int | None = None,
        references: list | None = None,
        approval: bool = False,
    ):
        """approval pauses after a passed draft. proceed() is the sign-off that continues it."""
        if inspection_mode not in {"manual", "auto"}:
            raise ValueError("Inspection mode must be manual or auto.")
        if runtime_seconds is not None and not 6 <= runtime_seconds <= 720:
            raise ValueError("Runtime must be between 6 and 720 seconds.")
        if aspect_ratio is not None and aspect_ratio not in {"16:9", "9:16", "1:1"}:
            raise ValueError("Aspect ratio must be 16:9, 9:16, or 1:1.")
        if max_shots is not None and not 1 <= max_shots <= 240:
            raise ValueError("Shot limit must be between 1 and 240.")
        if image_count is not None and (type(image_count) is not int or not 1 <= image_count <= 240):
            raise ValueError("Image count must be an integer between 1 and 240.")
        planned = _plan_references(reference_choices(references))
        selection = load_settings()["video"]["selection"]
        if runtime_seconds is not None:
            shot_budget(runtime_seconds, max_shots, selection)
        shortest, longest = clip_bounds(selection)
        runtime_range = None
        if max_shots is not None and runtime_seconds is None:
            # Without a runtime, Development must pick one this many shots can fill.
            runtime_range = (max(6, max_shots * shortest), min(720, max_shots * longest))
        locks = []
        if max_shots is not None:
            locks.append(f"The user allows at most {max_shots} shots.")
        if runtime_range:
            locks.append(f"With at most {max_shots} shots, the runtime must be between {runtime_range[0]} and {runtime_range[1]} seconds.")
        if image_count is not None and max_shots is None:
            locks.append(
                f"The user allows at most {image_count} generated stills. That number is the still budget."
            )
        elif image_count is not None:
            locks.append(
                f"The user allows at most {image_count} generated stills. "
                f"The shot list is capped separately at {max_shots} shots."
            )
        reference_lock = _reference_note(planned, story=True)
        if reference_lock:
            locks.append(reference_lock)
        if aspect_ratio:
            locks.append(f"Aspect ratio is locked at {aspect_ratio}.")
        if runtime_seconds:
            locks.append(f"Runtime is locked at {runtime_seconds} seconds.")
        lock_block = ("\n".join(locks) + "\n") if locks else ""

        stopped = _stop_event(cancel)
        if stopped is not None:
            yield stopped
            return

        if not load_kie_api_key():
            yield StudioEvent("failed",
                "KIE_API_KEY is not set. Add it to `.env` in the repo root. "
                "Keys are created at https://kie.ai/api-key\n"
            )
            return

        try:
            _raise_if_stopped(cancel)
            yield StudioEvent("development", "**Development** is locking the logline and the beats.\n\n")
            development = await self._develop(idea, lock_block)
            # Enforce user locks in code; model output cannot renegotiate them.
            development = development.model_copy(update={
                "runtime_seconds": runtime_seconds if runtime_seconds is not None else development.runtime_seconds,
                "aspect_ratio": aspect_ratio or development.aspect_ratio,
                "max_shots": max_shots,
            })
            if runtime_range and not runtime_range[0] <= development.runtime_seconds <= runtime_range[1]:
                # Keep the paid Development result instead of failing on its runtime choice.
                development = development.model_copy(update={"runtime_seconds": min(max(development.runtime_seconds, runtime_range[0]), runtime_range[1])})
            shot_budget(development.runtime_seconds, development.max_shots, selection)
            folder = _project_dir(development.slug or development.title)
            folder.mkdir(parents=True, exist_ok=True)
            self._bind(folder)
            stored = _copy_references(folder, planned)
            _save_settings(folder, image_count=image_count, references=stored, brief=idea, flow={
                "pages_only": pages_only, "approval": approval, "dry_run": dry_run, "inspection_mode": inspection_mode})
            atomic_write_text(folder / "00_development.json", development.model_dump_json(indent=2) + "\n")
            yield StudioEvent("developed",
                f"**{development.title}** · {development.job_type} · "
                f"{development.runtime_seconds}s · {development.aspect_ratio}\n\n"
                f"{development.logline}\n\n"
                f"Folder: `{folder.relative_to(WORKSPACE_ROOT)}`\n\n", folder=str(folder.relative_to(WORKSPACE_ROOT))
            )

            async for event in self._story(
                folder, development, stored, idea, pages_only=pages_only, approval=approval, dry_run=dry_run,
                inspection_mode=inspection_mode, cancel=cancel, image_count=image_count,
            ):
                yield event
        except _GenerationStopped:
            yield StudioEvent("failed", _STOP_TEXT)

    async def _story(self, folder: Path, development: Development, stored: list[dict], idea: str, *,
                     pages_only: bool, approval: bool, dry_run: bool, inspection_mode: str,
                     cancel: threading.Event | None, image_count: int | None,
                     screenplay: Screenplay | None = None):
        """Screenwriter, then the editor loop, then (unless paused) the image steps.

        Shared by run() and resume(). A saved screenplay skips the first write.
        """
        _raise_if_stopped(cancel)
        if screenplay is None:
            yield StudioEvent("writing", "**Screenwriter** is writing the pages.\n\n")
            screenplay = await self._write(development, previous=None, issues=None, references=stored)
        verdict = ScriptVerdict(passed=False, issues=["not yet read"])
        drafts = folder / "drafts"
        drafts.mkdir(exist_ok=True)
        # A resumed run keeps the earlier drafts and numbers its own after them.
        earlier = len(list(drafts.glob("*_screenplay.json")))
        previous_issues: list[str] | None = None
        for attempt in range(MAX_REWRITES + 1):
            number = earlier + attempt + 1
            atomic_write_text(drafts / f"{number:02d}_screenplay.json", screenplay.model_dump_json(indent=2) + "\n")
            _raise_if_stopped(cancel)
            yield StudioEvent("reviewing", f"**Script editor** is reading draft {number}.\n\n")
            verdict = await self._edit(development, screenplay, previous_issues)
            atomic_write_text(drafts / f"{number:02d}_verdict.json", verdict.model_dump_json(indent=2) + "\n")
            # The latest draft is always on disk, so a stop or an error mid-loop can be continued.
            atomic_write_text(folder / "01_screenplay.json", screenplay.model_dump_json(indent=2) + "\n")
            atomic_write_text(folder / "verdict.json", verdict.model_dump_json(indent=2) + "\n")
            if verdict.passed:
                yield StudioEvent("approved", "The editor passed the draft.\n\n")
                break
            listed = "\n".join(f"- {issue}" for issue in verdict.issues)
            yield StudioEvent("rejected", f"The editor sent it back:\n{listed}\n\n")
            if attempt == MAX_REWRITES:
                yield StudioEvent("failed", "Rewrite limit reached. The last draft is the one on disk.\n\n")
                break
            _raise_if_stopped(cancel)
            previous_issues = list(verdict.issues)
            yield StudioEvent("rewriting", "**Screenwriter** is rewriting from the editor's notes.\n\n")
            screenplay = await self._write(development, previous=screenplay, issues=verdict.issues, references=stored)

        atomic_write_text(folder / "01_screenplay.json", screenplay.model_dump_json(indent=2) + "\n")
        atomic_write_text(folder / "01_script.fountain", screenplay.fountain.rstrip() + "\n")
        atomic_write_text(folder / "02_shots.md", _shots_markdown(screenplay))
        atomic_write_text(folder / "verdict.json", verdict.model_dump_json(indent=2) + "\n")
        yield StudioEvent("pages", f"Pages: `{(folder / '01_script.fountain').relative_to(WORKSPACE_ROOT)}`\n\n")

        if not verdict.passed:
            yield StudioEvent(
                "failed",
                "The editor did not pass the draft. Use Proceed with this draft to continue into stills.\n\n",
            )
            return

        if pages_only:
            yield StudioEvent("complete", "Pages only. Stills were not requested.\n\n")
            return

        if approval:
            yield StudioEvent(
                "awaiting",
                "The pages are waiting for your approval. Approve them in the desk, or run "
                f"`python -m screenwriter_studio --proceed {folder.relative_to(WORKSPACE_ROOT)}` from the workspace, "
                "to write the image prompts and generate the images.\n\n"
                + _estimate_line(development, screenplay, image_count, stored),
            )
            return

        async for event in self._produce(
            folder, development, screenplay, idea, dry_run=dry_run, inspection_mode=inspection_mode, cancel=cancel,
            image_count=image_count, references=stored,
        ):
            yield event

    async def resume(self, folder: Path, *, cancel: threading.Event | None = None):
        """Continue a story run that stopped before the pages were finished.

        Development is not paid for again. A provider outage, a stop or a restart during the
        screenwriter or editor steps leaves the saved development (and any saved draft) behind;
        this picks up from there with the options the run started with.
        """
        if not story_unfinished(folder):
            raise ValueError("This production has nothing to retry: its pages are already written.")
        try:
            development = Development.model_validate_json((folder / "00_development.json").read_text(encoding="utf-8"))
        except (OSError, ValidationError, json.JSONDecodeError) as exc:
            raise ValueError("The saved development cannot be read, so this production cannot be retried.") from exc
        screenplay = None
        try:
            screenplay = Screenplay.model_validate_json((folder / "01_screenplay.json").read_text(encoding="utf-8"))
        except (OSError, ValidationError, json.JSONDecodeError):
            pass
        self._bind(folder)
        stopped = _stop_event(cancel)
        if stopped is not None:
            yield stopped
            return
        if not load_kie_api_key():
            yield StudioEvent("failed", "KIE_API_KEY is not set. Add it to `.env` in the repo root. "
                                        "Keys are created at https://kie.ai/api-key\n")
            return
        image_count, references = _load_settings(folder)
        flow = _saved_flow(folder)
        try:
            async for event in self._story(
                folder, development, references, _saved_brief(folder), pages_only=flow["pages_only"],
                approval=flow["approval"], dry_run=flow["dry_run"], inspection_mode=flow["inspection_mode"],
                cancel=cancel, image_count=image_count, screenplay=screenplay,
            ):
                yield event
        except _GenerationStopped:
            yield StudioEvent("failed", _STOP_TEXT)

    async def proceed(
        self,
        folder: Path,
        *,
        dry_run: bool = False,
        inspection_mode: str = "manual",
        brief: str = "",
        cancel: threading.Event | None = None,
    ):
        """Continue a saved production into art and stills.

        The editor is not asked again. A failed verdict stays on disk, and
        greenlight.json records that the saved draft was continued.
        """
        if inspection_mode not in {"manual", "auto"}:
            raise ValueError("Inspection mode must be manual or auto.")
        development, screenplay, verdict = _load_saved(folder)
        self._bind(folder)
        brief = brief or _saved_brief(folder)
        if _image_work_started(folder):
            raise ValueError("Image work is already saved. Resume it instead of starting again.")
        stopped = _stop_event(cancel)
        if stopped is not None:
            yield stopped
            return
        if not load_kie_api_key():
            yield StudioEvent(
                "failed",
                "KIE_API_KEY is not set. Add it to `.env` in the repo root. "
                "Keys are created at https://kie.ai/api-key\n",
            )
            return

        label = _folder_label(folder)
        atomic_write_json(folder / "greenlight.json", {
            "proceeded": True,
            "editor_passed": verdict.passed,
            "note": "Continued with the saved draft.",
        })
        if verdict.passed:
            yield StudioEvent("signed_off", "You approved the pages. Continuing to the art director.\n\n", folder=label)
        else:
            yield StudioEvent(
                "greenlit",
                "Proceeding with the saved draft. The editor did not pass it.\n\n",
                folder=label,
            )
        image_count, references = _load_settings(folder)
        try:
            async for event in self._produce(
                folder, development, screenplay, brief, dry_run=dry_run, inspection_mode=inspection_mode, cancel=cancel,
                image_count=image_count, references=references,
            ):
                yield event
        except _GenerationStopped:
            yield StudioEvent("failed", _STOP_TEXT)

    async def _produce(self, folder, development, screenplay, brief, *, dry_run, inspection_mode, cancel=None, image_count=None, references=None):
        _raise_if_stopped(cancel)
        locked_count = _covered(development, references)
        problems = video_feasibility(development, screenplay, image_count=image_count, locked_count=locked_count)
        if problems:
            listed = "\n".join(f"- {problem}" for problem in problems)
            yield StudioEvent("failed",
                "These shots could not all become video clips with the current settings, so no image prompts or "
                f"images were paid for:\n{listed}\n\nChange Models and quality in the Idea tab (video model, "
                "quality or sound), or start again with a different length or shot count.\n\n")
            return
        validate_reference_shots(references or [], [shot.model_dump() for shot in screenplay.shots])
        reference_ids = [item["id"] for item in references] if references else []
        package = _reusable_plan(folder, development, screenplay, image_count, reference_ids)
        if package is not None:
            yield StudioEvent("art", "**Art director** plan from the last attempt is reused; no new prompts were paid for.\n\n")
        else:
            yield StudioEvent("art", "**Art director** is writing the still jobs.\n\n"
                              + _estimate_line(development, screenplay, image_count, references))
            package = await self._art(development, screenplay, references=references, image_count=image_count)
            for repair in range(MAX_ART_REPAIRS + 1):
                atomic_write_text(folder / "03_still_plan.json", package.model_dump_json(indent=2) + "\n")
                try:
                    validate_still_package(
                        package, development, screenplay, image_count=image_count, reference_ids=reference_ids,
                    )
                    break
                except ValueError as exc:
                    if repair == MAX_ART_REPAIRS:
                        raise
                    # Nothing was sent to the image provider; show the art director what was wrong.
                    yield StudioEvent("art", "**Art director** is correcting the plan: " + str(exc) + "\n\n")
                    _raise_if_stopped(cancel)
                    package = await self._art(development, screenplay, references=references, image_count=image_count,
                                              previous=package, issues=[str(exc)])
        images = folder / "images"
        _raise_if_stopped(cancel)
        yield StudioEvent("stills", "**Stills** is calling KIE.ai.\n\n")
        locked = [
            dict(item) for item in references
        ] if references else None
        async for line in _stream_stills(
            package,
            images,
            dry_run=dry_run,
            inspection_mode=inspection_mode,
            inspection_context={
                "brief": brief,
                "development": development.model_dump(),
                "shots": [shot.model_dump() for shot in screenplay.shots],
            },
            cancel=cancel,
            locked_references=locked,
        ):
            yield line

    async def _develop(self, idea: str, lock_block: str) -> Development:
        return await self._call("development", development_agent, f"{lock_block}Brief:\n{idea.strip()}",
                                Development, max_turns=3)

    async def _write(
        self,
        development: Development,
        previous: Screenplay | None,
        issues: list[str] | None,
        references: list[dict] | None = None,
    ) -> Screenplay:
        return await self._call("writer", writer_agent,
                                writer_user_message(development, previous, issues, references),
                                Screenplay, max_turns=4)

    async def _edit(self, development: Development, screenplay: Screenplay, previous_issues: list[str] | None = None) -> ScriptVerdict:
        # Rules the code can check exactly come first: a draft that breaks one goes straight
        # back to the writer, so the editor model is only paid to read drafts that can pass.
        extra = _script_issues(development, screenplay) + _shot_issues(development, screenplay)
        if extra:
            self._note({"role": "editor", "outcome": "skipped", "reason": "deterministic issues",
                        "issues": len(extra)})
            return ScriptVerdict(passed=False, issues=extra)
        verdict = await self._call("editor", editor_agent,
                                   editor_user_message(development, screenplay, previous_issues),
                                   ScriptVerdict, max_turns=3)
        if not verdict.passed and not verdict.issues:
            # Nothing to fix: rewriting blind would only spend two more drafts.
            log.warning("The editor rejected a draft without naming an issue; treating it as passed.")
            verdict.passed = True
        if verdict.issues:
            verdict.passed = False
        return verdict

    async def _art(
        self,
        development: Development,
        screenplay: Screenplay,
        references: list[dict] | None = None,
        image_count: int | None = None,
        previous: StillPackage | None = None,
        issues: list[str] | None = None,
    ) -> StillPackage:
        message = art_user_message(development, screenplay, references, image_count)
        if previous is not None and issues:
            message += (
                "\n\nYour previous still plan was rejected before any image was made:\n"
                + "\n".join(f"- {issue}" for issue in issues)
                + "\nReturn the complete corrected plan. Keep what was valid.\nPrevious plan:\n"
                + previous.model_dump_json()
            )
        return await self._call("art", art_agent, message, StillPackage, max_turns=4)


def _clip_sentence(selection: str | None = None) -> str:
    """The clip length the screenwriter has to hit for the saved video model."""
    selection = selection or load_settings()["video"]["selection"]
    shortest, longest = clip_bounds(selection)
    sentence = f"Each clip is {shortest}–{longest} seconds."
    if selection == "auto":
        sentence += f" Clips longer than {clip_bounds('kling')[1]} seconds use Seedance 2.5."
    return sentence


def writer_user_message(
    development: Development,
    previous: Screenplay | None,
    issues: list[str] | None,
    references: list[dict] | None = None,
) -> str:
    """First draft gets the lock and the playbook. A rewrite does not.

    The writer's instructions already hold the craft rules, and look, palette,
    and lighting are for the art director. Sending that bulk again on every
    editor return spends context the rewrite does not use. The revision loop
    stays bounded in run(); this only stops the stale brief from riding along.
    See https://github.com/kunwardhruv/Supervisor-Multi-Agent-Content-Team
    """
    playbook = _playbook(development.job_type)
    selection = load_settings()["video"]["selection"]
    budget = shot_budget(development.runtime_seconds, development.max_shots, selection)
    clip = _clip_sentence(selection)
    if previous is not None and issues:
        lock = {
            "title": development.title,
            "job_type": development.job_type,
            "runtime_seconds": development.runtime_seconds,
            "aspect_ratio": development.aspect_ratio,
            "language": development.language,
            "logline": development.logline,
            "shot_budget": budget,
            "clip_seconds": clip,
            "beats": [beat.model_dump() for beat in development.beats],
            "character_names": [character.name for character in development.characters],
            "locations": development.locations,
        }
        message = "\n\n".join(
            [
                "The editor rejected the previous draft. Rewrite it. Do not renegotiate the lock.",
                json.dumps(lock, separators=(",", ":")),
                "Issues:\n" + "\n".join(f"- {issue}" for issue in issues),
                "Previous screenplay and shots:\n" + previous.model_dump_json(),
                CAMERA_COVERAGE,
            ]
        )
    else:
        message = "\n\n".join(
            [
                "Locked development. Do not renegotiate it.",
                development.model_dump_json(indent=2),
                (
                    f"Shot budget: {budget} shots maximum for {development.runtime_seconds} seconds "
                    f"(a configurable still-generation limit). Each shot becomes one generated image. "
                    f"Do not write more than {budget} shots. "
                    f"{clip} "
                    "Vary the cameras on those shots. Do not add shots to get a new angle."
                ),
                "Playbook:\n" + playbook,
                CAMERA_COVERAGE,
            ]
        )
    note = _reference_note(references, story=True)
    if note:
        message += "\n\n" + note
    if _locked(references):
        message += (
            "\nKeep the locked faces, costumes, and places. This is a different plot, not a rewrite of the previous story."
        )
    if _styles(references):
        message += "\nStyle references are for the art director. Write the story the development lock describes."
    return message


def editor_user_message(development: Development, screenplay: Screenplay, previous_issues: list[str] | None = None) -> str:
    """Checklist payload. Compact JSON: this whole draft is resent on every pass."""
    budget = shot_budget(development.runtime_seconds, development.max_shots)
    payload = {
        "runtime_seconds": development.runtime_seconds,
        "shot_budget": budget,
        "logline": development.logline,
        "beats": [beat.model_dump() for beat in development.beats],
        "fountain": screenplay.fountain,
        "shots": [shot.model_dump() for shot in screenplay.shots],
    }
    if previous_issues:
        payload["previous_issues"] = previous_issues
    return "Check this draft against the checklist.\n\n" + json.dumps(payload, separators=(",", ":"))


def art_user_message(
    development: Development,
    screenplay: Screenplay,
    references: list[dict] | None = None,
    image_count: int | None = None,
) -> str:
    """What the art director reads: the checked model card, then the locked piece."""
    budget = shot_budget(development.runtime_seconds, development.max_shots)
    keyframes = min(budget, image_count) if image_count is not None else budget
    payload = {
        "aspect_ratio": development.aspect_ratio,
        "runtime_seconds": development.runtime_seconds,
        "max_keyframes": keyframes,
        "look": development.look,
        "palette": development.palette,
        "lighting": development.lighting,
        "characters": [character.model_dump() for character in development.characters],
        "locations": development.locations,
        "shots": [shot.model_dump() for shot in screenplay.shots],
    }
    if image_count is not None:
        payload["max_images"] = image_count
    body = (
        "Write the still jobs for this locked piece.\n\n"
        "Write each keyframe's camera as a concrete camera angle, height, and subject distance. "
        "Vary those cameras across the supplied shots. Do not add keyframes.\n\n"
        + CAMERA_COVERAGE
        # The checked image-model card is already in the art director's instructions;
        # repeating it here only doubled the tokens on every call.
    )
    notes = []
    if image_count is not None and development.max_shots is None:
        notes.append(
            f"The still budget is {image_count} generated images. "
            f"Write at most {image_count} new still jobs. Do not write more."
        )
    elif image_count is not None:
        notes.append(
            f"Generate at most {image_count} new stills. "
            f"The shot list is capped at {development.max_shots} shots. "
            "Do not write more image jobs than the image count."
        )
    if image_count is not None and image_count < len(screenplay.shots):
        notes.append(
            f"The image count is smaller than the {len(screenplay.shots)} shots. "
            "Keyframes must use shot ids and may cover only as many shots as fit. "
            "Do not add jobs past the image count."
        )
    note = _reference_note(references)
    if note:
        notes.append(note)
    if _locked(references):
        notes.append(
            "Attach a locked id only to the stills whose shot shows that person or place. "
            "Do not attach it to every keyframe. "
            "Do not invent new character or location jobs for people and places these images already show. "
            "Do not generate the locked references again."
        )
    if _styles(references):
        notes.append(
            "Style ids are attached to keyframes and location jobs by the stills step, respecting any selected shots. "
            "Do not build character or location jobs from them and do not describe their people or places. "
            "Write the look they share into applicable prompts only: grade, light, lens, texture."
        )
    if references:
        notes.append(
            "Respect each reference role and shot scope. Attach character, location and object ids only where "
            "that named subject appears; match reference names to the shot's characters or location when possible. "
            "For composition, attach the id to the shots whose framing it should guide. "
            "Explicit shot selections are enforced by the stills step; do not use those references on shared sheets. "
            "Do not recreate a character or location sheet already fully covered by an automatic reference. "
            "Face-only or wardrobe-only references may guide a new sheet with the other attributes from the script. "
            "Write only the selected attributes into prompts, respecting face and wardrobe switches."
        )
    if notes:
        body += "\n" + "\n\n".join(notes)
    return body + "\nLocked piece:\n" + json.dumps(payload, indent=2)


def _script_issues(development: Development, screenplay: Screenplay, selection: str | None = None) -> list[str]:
    issues = []
    selection = selection or load_settings()["video"]["selection"]
    budget = shot_budget(development.runtime_seconds, development.max_shots, selection)
    if len(screenplay.shots) > budget:
        issues.append(f"Shot list has {len(screenplay.shots)} shots; limit is {budget}. Consolidate coherently; preserve the story and runtime.")
    shortest, longest = clip_bounds(selection)
    for shot in screenplay.shots:
        if not shortest <= shot.duration_seconds <= longest:
            issues.append(
                f"Shot {shot.number} is {shot.duration_seconds}s; clips for this video model are {shortest}–{longest} seconds."
            )
    duration = sum(shot.duration_seconds for shot in screenplay.shots)
    if duration != development.runtime_seconds:
        issues.append(f"Shot durations total {duration}s; required runtime is {development.runtime_seconds}s. Rebalance durations.")
    numbers = [shot.number for shot in screenplay.shots]
    if numbers != list(range(1, len(numbers) + 1)):
        issues.append("Shot numbers must be unique and sequential, starting at 1.")
    return issues


_VOICE_OVER = {"narrator", "v.o.", "voice-over", "voiceover"}


def _plain(text: str) -> str:
    return " ".join(text.lower().split())


def _shot_issues(development: Development, screenplay: Screenplay) -> list[str]:
    """Editor notes the code can check exactly. They send a draft back but never block paid work."""
    issues = []
    cast = {_plain(character.name): character.name for character in development.characters}
    places = [_plain(place) for place in development.locations]
    for shot in screenplay.shots:
        spoken = sum(len(line.text.split()) for line in shot.dialogue)
        spoken += sum(len(line.split()) for line in re.findall(r'["“「]([^"”」]{1,600})["”」]', shot.audio))
        if spoken > 3 * shot.duration_seconds:
            issues.append(
                f"Shot {shot.number} has {spoken} spoken words in {shot.duration_seconds}s; keep it to about "
                f"{int(2.5 * shot.duration_seconds)} so the generated speech is not rushed or cut off."
            )
        unknown = [name for name in shot.characters if _plain(name) not in cast]
        if unknown and cast:
            issues.append(f"Shot {shot.number} lists {', '.join(unknown)} in characters; use the locked names exactly: "
                          f"{', '.join(cast.values())}.")
        place = _plain(shot.location)
        if place and places and not any(place == known or place in known or known in place for known in places):
            issues.append(f"Shot {shot.number} location \"{shot.location}\" is not a locked location; copy one exactly: "
                          + "; ".join(development.locations) + ".")
        speakers = {_plain(line.speaker) for line in shot.dialogue if line.text.strip()}
        if len(speakers) > 1:
            issues.append(f"Shot {shot.number} has {len(speakers)} speakers; keep one speaker per shot and move the "
                          "other lines to the neighbouring shots or cut them.")
        strangers = sorted(speaker for speaker in speakers if speaker not in cast and speaker not in _VOICE_OVER)
        if strangers and cast:
            issues.append(f"Shot {shot.number} dialogue speaker {', '.join(strangers)} is not a locked character; "
                          "use a locked name or NARRATOR.")
    return issues


def _shots_markdown(screenplay: Screenplay) -> str:
    rows = ["| # | Dur | Framing | Move | Cast | Location | Action | Dialogue | Audio | Notes |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for shot in screenplay.shots:
        dialogue = " / ".join(f'{line.speaker}: "{line.text}"' for line in shot.dialogue)
        cells = [
            str(shot.number),
            f"{shot.duration_seconds}s",
            shot.framing,
            shot.camera_move,
            ", ".join(shot.characters),
            shot.location,
            shot.action,
            dialogue,
            shot.audio,
            shot.notes,
        ]
        rows.append("| " + " | ".join(cell.replace("|", "/").replace("\n", " ") for cell in cells) + " |")
    notes = "\n".join(f"- {note}" for note in screenplay.notes)
    return "\n".join(rows) + ("\n\n" + notes if notes else "") + "\n"
