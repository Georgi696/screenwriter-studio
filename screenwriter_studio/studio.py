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
import json
import queue
import re
import shutil
import threading
from pathlib import Path

from agents import Runner, set_tracing_disabled
from dotenv import load_dotenv
from pydantic import ValidationError

from screenwriter_studio.crew.art import art_agent
from screenwriter_studio.budget import shot_budget
from screenwriter_studio.events import StudioEvent
from screenwriter_studio.images.catalog import art_director_card
from screenwriter_studio.crew.development import development_agent
from screenwriter_studio.crew.editor import editor_agent
from screenwriter_studio.playbooks import CAMERA_COVERAGE, PLAYBOOKS
from screenwriter_studio.paths import WORKSPACE_ROOT
from screenwriter_studio.models import load_kie_api_key
from screenwriter_studio.settings import chat_agent
from screenwriter_studio.schemas import Development, Screenplay, ScriptVerdict, StillPackage
from screenwriter_studio.images.service import validate_still_package, render_stills
from screenwriter_studio.crew.writer import writer_agent

load_dotenv(override=True)
set_tracing_disabled(True)

MAX_REWRITES = 2
_STOP_TEXT = "Generation stopped. Saved images can be resumed.\n\n"
_MAX_REFERENCE_BYTES = 8_000_000
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
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (cleaned or "untitled")[:48]


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
    if size <= 0 or size > _MAX_REFERENCE_BYTES or not _suffix_matches_image(suffix, header):
        raise ValueError("Reference image must be a PNG, JPEG, or WebP within the size limit.")
    return file


REFERENCE_MODES = ("style", "lock")


def reference_choices(references) -> list[tuple[str, str]]:
    """(path, mode) pairs. A bare path is a lock, which is how references worked before modes."""
    if references is None:
        return []
    if not isinstance(references, list):
        raise ValueError("Reference images must be chosen from the library.")
    chosen: list[tuple[str, str]] = []
    for item in references:
        if isinstance(item, dict):
            path, mode = item.get("path"), item.get("mode", "lock")
        else:
            path, mode = item, "lock"
        if type(path) is not str or not path.strip():
            raise ValueError("Reference images must be chosen from the library.")
        if mode not in REFERENCE_MODES:
            raise ValueError("Reference mode must be style or lock.")
        chosen.append((path.strip(), mode))
    return chosen


def _plan_references(choices: list[tuple[str, str]]) -> list[dict]:
    planned = []
    seen: set[str] = set()
    counts = {"lock": 0, "style": 0}
    for relative, mode in choices:
        if relative in seen:
            continue
        seen.add(relative)
        source = _reference_source(relative)
        counts[mode] += 1
        prefix = "style" if mode == "style" else "ref"
        planned.append({
            "id": f"{prefix}-{counts[mode]:02d}",
            "name": source.name,
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


def _reference_note(references: list[dict] | None) -> str:
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
    return "\n".join(lines)


_SETTINGS = "run_settings.json"


def _save_settings(folder: Path, *, image_count: int | None, references: list[dict]) -> None:
    """What proceed() needs to continue the same run after a pause."""
    payload = {"image_count": image_count, "references": references}
    (folder / _SETTINGS).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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
        if runtime_seconds is not None:
            shot_budget(runtime_seconds, max_shots)
        locks = []
        if max_shots is not None:
            locks.append(f"The user allows at most {max_shots} shots.")
        if image_count is not None and max_shots is None:
            locks.append(
                f"The user allows at most {image_count} generated stills. That number is the still budget."
            )
        elif image_count is not None:
            locks.append(
                f"The user allows at most {image_count} generated stills. "
                f"The shot list is capped separately at {max_shots} shots."
            )
        reference_lock = _reference_note(planned)
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
            shot_budget(development.runtime_seconds, development.max_shots)
            folder = _project_dir(development.slug or development.title)
            folder.mkdir(parents=True, exist_ok=True)
            stored = _copy_references(folder, planned)
            _save_settings(folder, image_count=image_count, references=stored)
            (folder / "00_development.json").write_text(
                development.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            yield StudioEvent("developed",
                f"**{development.title}** · {development.job_type} · "
                f"{development.runtime_seconds}s · {development.aspect_ratio}\n\n"
                f"{development.logline}\n\n"
                f"Folder: `{folder.relative_to(WORKSPACE_ROOT)}`\n\n", folder=str(folder.relative_to(WORKSPACE_ROOT))
            )

            _raise_if_stopped(cancel)
            yield StudioEvent("writing", "**Screenwriter** is writing the pages.\n\n")
            screenplay = await self._write(development, previous=None, issues=None, references=stored)
            verdict = ScriptVerdict(passed=False, issues=["not yet read"])
            drafts = folder / "drafts"
            drafts.mkdir()
            for attempt in range(MAX_REWRITES + 1):
                (drafts / f"{attempt + 1:02d}_screenplay.json").write_text(screenplay.model_dump_json(indent=2) + "\n", encoding="utf-8")
                _raise_if_stopped(cancel)
                yield StudioEvent("reviewing", f"**Script editor** is reading draft {attempt + 1}.\n\n")
                verdict = await self._edit(development, screenplay)
                (drafts / f"{attempt + 1:02d}_verdict.json").write_text(verdict.model_dump_json(indent=2) + "\n", encoding="utf-8")
                if verdict.passed:
                    yield StudioEvent("approved", "The editor passed the draft.\n\n")
                    break
                listed = "\n".join(f"- {issue}" for issue in verdict.issues)
                yield StudioEvent("rejected", f"The editor sent it back:\n{listed}\n\n")
                if attempt == MAX_REWRITES:
                    yield StudioEvent("failed", "Rewrite limit reached. The last draft is the one on disk.\n\n")
                    break
                _raise_if_stopped(cancel)
                yield StudioEvent("rewriting", "**Screenwriter** is rewriting from the editor's notes.\n\n")
                screenplay = await self._write(development, previous=screenplay, issues=verdict.issues, references=stored)

            (folder / "01_screenplay.json").write_text(screenplay.model_dump_json(indent=2) + "\n", encoding="utf-8")
            (folder / "01_script.fountain").write_text(screenplay.fountain.rstrip() + "\n", encoding="utf-8")
            (folder / "02_shots.md").write_text(_shots_markdown(screenplay), encoding="utf-8")
            (folder / "verdict.json").write_text(verdict.model_dump_json(indent=2) + "\n", encoding="utf-8")
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
                    "The pages are waiting for your approval. Approve them to start the art director and stills.\n\n",
                )
                return

            async for event in self._produce(
                folder, development, screenplay, idea, dry_run=dry_run, inspection_mode=inspection_mode, cancel=cancel,
                image_count=image_count, references=stored,
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
        (folder / "greenlight.json").write_text(
            json.dumps(
                {
                    "proceeded": True,
                    "editor_passed": verdict.passed,
                    "note": "Continued with the saved draft.",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
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
        yield StudioEvent("art", "**Art director** is writing the still jobs.\n\n")
        package = await self._art(development, screenplay, references=references, image_count=image_count)
        (folder / "03_still_plan.json").write_text(package.model_dump_json(indent=2) + "\n", encoding="utf-8")
        reference_ids = [item["id"] for item in references] if references else []
        validate_still_package(
            package, development, screenplay, image_count=image_count, reference_ids=reference_ids,
        )
        images = folder / "images"
        _raise_if_stopped(cancel)
        yield StudioEvent("stills", "**Stills** is calling KIE.ai.\n\n")
        locked = [
            {"id": item["id"], "path": item["path"], "mode": item.get("mode", "lock")} for item in references
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
        result = await Runner.run(
            chat_agent("development", development_agent),
            f"{lock_block}Brief:\n{idea.strip()}",
            max_turns=3,
        )
        return result.final_output_as(Development)

    async def _write(
        self,
        development: Development,
        previous: Screenplay | None,
        issues: list[str] | None,
        references: list[dict] | None = None,
    ) -> Screenplay:
        result = await Runner.run(
            chat_agent("writer", writer_agent),
            writer_user_message(development, previous, issues, references),
            max_turns=4,
        )
        return result.final_output_as(Screenplay)

    async def _edit(self, development: Development, screenplay: Screenplay) -> ScriptVerdict:
        result = await Runner.run(
            chat_agent("editor", editor_agent),
            editor_user_message(development, screenplay),
            max_turns=3,
        )
        verdict = result.final_output_as(ScriptVerdict)
        extra = _script_issues(development, screenplay)
        if extra:
            verdict.issues = extra + verdict.issues
        if verdict.issues:
            verdict.passed = False
        return verdict

    async def _art(
        self,
        development: Development,
        screenplay: Screenplay,
        references: list[dict] | None = None,
        image_count: int | None = None,
    ) -> StillPackage:
        result = await Runner.run(
            chat_agent("art", art_agent),
            art_user_message(development, screenplay, references, image_count),
            max_turns=4,
        )
        return result.final_output_as(StillPackage)


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
    playbook = PLAYBOOKS.get(development.job_type, PLAYBOOKS["narrative"])
    budget = shot_budget(development.runtime_seconds, development.max_shots)
    if previous is not None and issues:
        lock = {
            "title": development.title,
            "job_type": development.job_type,
            "runtime_seconds": development.runtime_seconds,
            "aspect_ratio": development.aspect_ratio,
            "language": development.language,
            "logline": development.logline,
            "shot_budget": budget,
            "beats": [beat.model_dump() for beat in development.beats],
            "character_names": [character.name for character in development.characters],
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
                    "Vary the cameras on those shots. Do not add shots to get a new angle."
                ),
                "Playbook:\n" + playbook,
                CAMERA_COVERAGE,
            ]
        )
    note = _reference_note(references)
    if note:
        message += "\n\n" + note
    if _locked(references):
        message += (
            "\nKeep the locked faces, costumes, and places. This is a different plot, not a rewrite of the previous story."
        )
    if _styles(references):
        message += "\nStyle references are for the art director. Write the story the development lock describes."
    return message


def editor_user_message(development: Development, screenplay: Screenplay) -> str:
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
        "Write each keyframe's camera as a concrete angle, height, and distance. "
        "Vary those cameras across the supplied shots. Do not add keyframes.\n\n"
        + CAMERA_COVERAGE
        + "\n"
        + art_director_card()
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
            "Style ids are attached to every keyframe and location job by the stills step. "
            "Do not build character or location jobs from them and do not describe their people or places. "
            "Write the look they share into each prompt in words: grade, light, lens, texture."
        )
    if notes:
        body += "\n" + "\n\n".join(notes)
    return body + "\nLocked piece:\n" + json.dumps(payload, indent=2)


def _script_issues(development: Development, screenplay: Screenplay) -> list[str]:
    issues = []
    budget = shot_budget(development.runtime_seconds, development.max_shots)
    if len(screenplay.shots) > budget:
        issues.append(f"Shot list has {len(screenplay.shots)} shots; limit is {budget}. Consolidate coherently; preserve the story and runtime.")
    duration = sum(shot.duration_seconds for shot in screenplay.shots)
    if duration != development.runtime_seconds:
        issues.append(f"Shot durations total {duration}s; required runtime is {development.runtime_seconds}s. Rebalance durations.")
    numbers = [shot.number for shot in screenplay.shots]
    if numbers != list(range(1, len(numbers) + 1)):
        issues.append("Shot numbers must be unique and sequential, starting at 1.")
    return issues


def _shots_markdown(screenplay: Screenplay) -> str:
    rows = ["| # | Dur | Framing | Action | Audio | Notes |", "|---|---|---|---|---|---|"]
    for shot in screenplay.shots:
        cells = [
            str(shot.number),
            f"{shot.duration_seconds}s",
            shot.framing.replace("|", "/"),
            shot.action.replace("|", "/"),
            shot.audio.replace("|", "/"),
            shot.notes.replace("|", "/"),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    notes = "\n".join(f"- {note}" for note in screenplay.notes)
    return "\n".join(rows) + ("\n\n" + notes if notes else "") + "\n"
