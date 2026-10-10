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
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agents import Runner, set_tracing_disabled
from dotenv import load_dotenv

from art_agent import art_agent
from budget import shot_budget
from events import StudioEvent
from image_model_card import art_director_card
from development_agent import development_agent
from editor_agent import editor_agent
from playbooks import PLAYBOOKS
from models import load_kie_api_key
from schemas import Development, Screenplay, ScriptVerdict, StillPackage
from stills import validate_still_package, render_stills
from writer_agent import writer_agent

load_dotenv(override=True)
set_tracing_disabled(True)

MAX_REWRITES = 2
REPO_ROOT = Path(__file__).resolve().parents[1]


def _still_line(record: dict) -> str:
    kind = record.get("kind") or "still"
    if record.get("phase") == "start":
        return f"**Still** {record['id']} ({kind}) started on {record.get('model')}.\n\n"
    if record.get("dry_run"):
        return f"- {record['id']} ({kind}) planned on {record.get('model')}; no image generated.\n\n"
    if record.get("error"):
        return f"- {record['id']} ({kind}) failed: {record['error']}\n\n"
    return f"- {record['id']} ({kind}) — {record.get('model')} — `{record.get('path')}`\n\n"


async def _stream_stills(package: StillPackage, images: Path, *, dry_run: bool):
    """Yield each still as it starts and again when the file is ready."""
    events: queue.Queue[dict] = queue.Queue()
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
            on_record=on_record,
            on_start=on_start,
            cancel=cancel,
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
    except asyncio.CancelledError:
        cancel.set()
        task.cancel()
        raise
    except (RuntimeError, ValueError, SystemExit) as exc:
        yield StudioEvent("failed", f"**Stills stopped:** {exc}\n\n")
        return

    failed = sum(bool(record.get("error")) for record in manifest["images"])
    if failed:
        yield StudioEvent("failed", f"{failed} still job(s) failed. Resume the saved jobs to retry.\n\n")
        return
    label = "Dry run: models chosen, no images downloaded." if dry_run else "Stills are in `images/`."
    yield StudioEvent("complete", label + "\n\n")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (cleaned or "untitled")[:48]


def _project_dir(slug: str) -> Path:
    base = REPO_ROOT / "productions" / _slug(slug)
    occupied = base.exists() and any(base.iterdir())
    if not occupied:
        return base
    number = 2
    while True:
        candidate = REPO_ROOT / "productions" / f"{_slug(slug)}-{number}"
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
    ):
        if runtime_seconds is not None and not 6 <= runtime_seconds <= 720:
            raise ValueError("Runtime must be between 6 and 720 seconds.")
        if aspect_ratio is not None and aspect_ratio not in {"16:9", "9:16", "1:1"}:
            raise ValueError("Aspect ratio must be 16:9, 9:16, or 1:1.")
        if max_shots is not None and not 1 <= max_shots <= 240:
            raise ValueError("Shot limit must be between 1 and 240.")
        if runtime_seconds is not None:
            shot_budget(runtime_seconds, max_shots)
        locks = []
        if max_shots is not None:
            locks.append(f"The user allows at most {max_shots} shots.")
        if aspect_ratio:
            locks.append(f"Aspect ratio is locked at {aspect_ratio}.")
        if runtime_seconds:
            locks.append(f"Runtime is locked at {runtime_seconds} seconds.")
        lock_block = ("\n".join(locks) + "\n") if locks else ""

        if not load_kie_api_key():
            yield StudioEvent("failed",
                "KIE_API_KEY is not set. Add it to `.env` in the repo root. "
                "Keys are created at https://kie.ai/api-key\n"
            )
            return

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
        (folder / "00_development.json").write_text(
            development.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        yield StudioEvent("developed",
            f"**{development.title}** · {development.job_type} · "
            f"{development.runtime_seconds}s · {development.aspect_ratio}\n\n"
            f"{development.logline}\n\n"
            f"Folder: `{folder.relative_to(REPO_ROOT)}`\n\n", folder=str(folder.relative_to(REPO_ROOT))
        )

        yield StudioEvent("writing", "**Screenwriter** is writing the pages.\n\n")
        screenplay = await self._write(development, previous=None, issues=None)
        verdict = ScriptVerdict(passed=False, issues=["not yet read"])
        drafts = folder / "drafts"
        drafts.mkdir()
        for attempt in range(MAX_REWRITES + 1):
            (drafts / f"{attempt + 1:02d}_screenplay.json").write_text(screenplay.model_dump_json(indent=2) + "\n", encoding="utf-8")
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
            yield StudioEvent("rewriting", "**Screenwriter** is rewriting from the editor's notes.\n\n")
            screenplay = await self._write(development, previous=screenplay, issues=verdict.issues)

        (folder / "01_screenplay.json").write_text(screenplay.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (folder / "01_script.fountain").write_text(screenplay.fountain.rstrip() + "\n", encoding="utf-8")
        (folder / "02_shots.md").write_text(_shots_markdown(screenplay), encoding="utf-8")
        (folder / "verdict.json").write_text(verdict.model_dump_json(indent=2) + "\n", encoding="utf-8")
        yield StudioEvent("pages", f"Pages: `{(folder / '01_script.fountain').relative_to(REPO_ROOT)}`\n\n")

        if not verdict.passed:
            yield StudioEvent("failed", "The editor did not pass the draft. Stills were not generated.\n\n")
            return

        if pages_only:
            yield StudioEvent("complete", "Pages only. Stills were not requested.\n\n")
            return

        yield StudioEvent("art", "**Art director** is writing the still jobs.\n\n")
        package = await self._art(development, screenplay)
        (folder / "03_still_plan.json").write_text(package.model_dump_json(indent=2) + "\n", encoding="utf-8")
        validate_still_package(package, development, screenplay)
        images = folder / "images"
        yield StudioEvent("stills", "**Stills** is calling KIE.ai.\n\n")
        async for line in _stream_stills(package, images, dry_run=dry_run):
            yield line

    async def _develop(self, idea: str, lock_block: str) -> Development:
        result = await Runner.run(
            development_agent,
            f"{lock_block}Brief:\n{idea.strip()}",
            max_turns=3,
        )
        return result.final_output_as(Development)

    async def _write(
        self,
        development: Development,
        previous: Screenplay | None,
        issues: list[str] | None,
    ) -> Screenplay:
        result = await Runner.run(
            writer_agent,
            writer_user_message(development, previous, issues),
            max_turns=4,
        )
        return result.final_output_as(Screenplay)

    async def _edit(self, development: Development, screenplay: Screenplay) -> ScriptVerdict:
        result = await Runner.run(
            editor_agent,
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

    async def _art(self, development: Development, screenplay: Screenplay) -> StillPackage:
        result = await Runner.run(art_agent, art_user_message(development, screenplay), max_turns=4)
        return result.final_output_as(StillPackage)


def writer_user_message(
    development: Development,
    previous: Screenplay | None,
    issues: list[str] | None,
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
        return "\n\n".join(
            [
                "The editor rejected the previous draft. Rewrite it. Do not renegotiate the lock.",
                json.dumps(lock, separators=(",", ":")),
                "Issues:\n" + "\n".join(f"- {issue}" for issue in issues),
                "Previous screenplay and shots:\n" + previous.model_dump_json(),
            ]
        )
    return "\n\n".join(
        [
            "Locked development. Do not renegotiate it.",
            development.model_dump_json(indent=2),
            (
                f"Shot budget: {budget} shots maximum for {development.runtime_seconds} seconds "
                f"(a configurable still-generation limit). Each shot becomes one generated image. "
                f"Do not write more than {budget} shots."
            ),
            "Playbook:\n" + playbook,
        ]
    )


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


def art_user_message(development: Development, screenplay: Screenplay) -> str:
    """What the art director reads: the checked model card, then the locked piece."""
    budget = shot_budget(development.runtime_seconds, development.max_shots)
    payload = {
        "aspect_ratio": development.aspect_ratio,
        "runtime_seconds": development.runtime_seconds,
        "max_keyframes": budget,
        "look": development.look,
        "palette": development.palette,
        "lighting": development.lighting,
        "characters": [character.model_dump() for character in development.characters],
        "locations": development.locations,
        "shots": [shot.model_dump() for shot in screenplay.shots],
    }
    return (
        "Write the still jobs for this locked piece.\n\n"
        + art_director_card()
        + "\nLocked piece:\n"
        + json.dumps(payload, indent=2)
    )


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
