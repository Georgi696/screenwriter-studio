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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agents import Runner, set_tracing_disabled
from dotenv import load_dotenv

from art_agent import art_agent
from budget import CLIP_SECONDS, sample_indexes, shot_budget
from image_model_card import art_director_card
from development_agent import development_agent
from editor_agent import editor_agent
from playbooks import PLAYBOOKS
from models import load_kie_api_key
from schemas import Development, Screenplay, ScriptVerdict, StillPackage
from stills import limit_still_package, render_stills
from writer_agent import writer_agent

load_dotenv(override=True)
set_tracing_disabled(True)

MAX_REWRITES = 2
REPO_ROOT = Path(__file__).resolve().parents[1]


def _still_line(record: dict) -> str:
    kind = record.get("kind") or "still"
    if record.get("phase") == "start":
        return f"**Still** {record['id']} ({kind}) started on {record.get('model')}.\n\n"
    if record.get("error"):
        return f"- {record['id']} ({kind}) failed: {record['error']}\n\n"
    return f"- {record['id']} ({kind}) — {record.get('model')} — `{record.get('path')}`\n\n"


async def _stream_stills(package: StillPackage, images: Path, *, dry_run: bool):
    """Yield each still as it starts and again when the file is ready."""
    events: queue.Queue[dict] = queue.Queue()

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
        )
    )
    try:
        while not task.done():
            while True:
                try:
                    record = events.get_nowait()
                except queue.Empty:
                    break
                yield _still_line(record)
            await asyncio.sleep(0.2)
        while True:
            try:
                record = events.get_nowait()
            except queue.Empty:
                break
            yield _still_line(record)
        task.result()
    except (RuntimeError, ValueError, SystemExit) as exc:
        yield f"**Stills stopped:** {exc}\n\n"
        return

    label = "Dry run: models chosen, no images downloaded." if dry_run else "Stills are in `images/`."
    yield label + "\n\n"


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
    ):
        locks = []
        if aspect_ratio:
            locks.append(f"Aspect ratio is locked at {aspect_ratio}.")
        if runtime_seconds:
            locks.append(f"Runtime is locked at {runtime_seconds} seconds.")
        lock_block = ("\n".join(locks) + "\n") if locks else ""

        if not load_kie_api_key():
            yield (
                "KIE_API_KEY is not set. Add it to `.env` in the repo root. "
                "Keys are created at https://kie.ai/api-key\n"
            )
            return

        yield "**Development** is locking the logline and the beats.\n\n"
        development = await self._develop(idea, lock_block)
        folder = _project_dir(development.slug or development.title)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "00_development.json").write_text(
            development.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        yield (
            f"**{development.title}** · {development.job_type} · "
            f"{development.runtime_seconds}s · {development.aspect_ratio}\n\n"
            f"{development.logline}\n\n"
            f"Folder: `{folder.relative_to(REPO_ROOT)}`\n\n"
        )

        yield "**Screenwriter** is writing the pages.\n\n"
        screenplay = await self._write(development, previous=None, issues=None)
        verdict = ScriptVerdict(passed=False, issues=["not yet read"])
        for attempt in range(MAX_REWRITES + 1):
            yield f"**Script editor** is reading draft {attempt + 1}.\n\n"
            verdict = await self._edit(development, screenplay)
            if verdict.passed:
                yield "The editor passed the draft.\n\n"
                break
            listed = "\n".join(f"- {issue}" for issue in verdict.issues)
            yield f"The editor sent it back:\n{listed}\n\n"
            if attempt == MAX_REWRITES:
                yield "Rewrite limit reached. The last draft is the one on disk.\n\n"
                break
            yield "**Screenwriter** is rewriting from the editor's notes.\n\n"
            screenplay = await self._write(development, previous=screenplay, issues=verdict.issues)

        other_issues = [issue for issue in verdict.issues if not _is_shot_count_issue(issue)]
        if _budget_issues(development, screenplay) and not other_issues:
            screenplay, clipped = _clip_shots(development, screenplay)
            verdict.issues = []
            verdict.passed = True
            yield (
                f"Shot list was {clipped} shots. A {development.runtime_seconds}s clip "
                f"holds {shot_budget(development.runtime_seconds)} keyframes "
                f"(one per {CLIP_SECONDS}s). Extra shots were not sent to the image agent.\n\n"
            )

        (folder / "01_script.fountain").write_text(screenplay.fountain.rstrip() + "\n", encoding="utf-8")
        (folder / "02_shots.md").write_text(_shots_markdown(screenplay), encoding="utf-8")
        (folder / "verdict.json").write_text(verdict.model_dump_json(indent=2) + "\n", encoding="utf-8")
        yield f"Pages: `{(folder / '01_script.fountain').relative_to(REPO_ROOT)}`\n\n"

        if not verdict.passed:
            yield "The editor did not pass the draft. Stills were not generated.\n\n"
            return

        if pages_only:
            yield "Pages only. Stills were not requested.\n\n"
            return

        yield "**Art director** is writing the still jobs.\n\n"
        package = await self._art(development, screenplay)
        package, dropped = _cap_stills(development, package)
        if dropped:
            yield (
                f"Image budget is {shot_budget(development.runtime_seconds)} keyframes "
                f"for {development.runtime_seconds}s. Dropped {dropped} extra still "
                f"{'job' if dropped == 1 else 'jobs'} before calling KIE.ai.\n\n"
            )
        images = folder / "images"
        yield "**Stills** is calling KIE.ai.\n\n"
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
        extra = _budget_issues(development, screenplay)
        if extra:
            verdict.issues = extra + [
                issue for issue in verdict.issues if not _asks_for_another_shot(issue)
            ]
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
    budget = shot_budget(development.runtime_seconds)
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
                "Previous Fountain:\n" + previous.fountain,
            ]
        )
    return "\n\n".join(
        [
            "Locked development. Do not renegotiate it.",
            development.model_dump_json(indent=2),
            (
                f"Shot budget: {budget} shots maximum for {development.runtime_seconds} seconds "
                f"(one shot per {CLIP_SECONDS} seconds). Each shot becomes one generated image. "
                f"Do not write more than {budget} shots."
            ),
            "Playbook:\n" + playbook,
        ]
    )


def editor_user_message(development: Development, screenplay: Screenplay) -> str:
    """Checklist payload. Compact JSON: this whole draft is resent on every pass."""
    budget = shot_budget(development.runtime_seconds)
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
    budget = shot_budget(development.runtime_seconds)
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


def _budget_issues(development: Development, screenplay: Screenplay) -> list[str]:
    budget = shot_budget(development.runtime_seconds)
    count = len(screenplay.shots)
    if count <= budget:
        return []
    return [
        (
            f"Shot list has {count} shots. A {development.runtime_seconds}s clip holds at most "
            f"{budget} (one shot per {CLIP_SECONDS} seconds), because each shot becomes a generated image. "
            f"Merge down to {budget} or fewer. Cut secondary actions into audio or notes. Do not add shots."
        )
    ]


def _is_shot_count_issue(issue: str) -> bool:
    """True when the note is about how many shots the runtime can hold."""
    if issue.startswith("Shot list has "):
        return True
    text = issue.lower()
    return "shot" in text and ("budget" in text or "8 second" in text or "too many" in text)


def _asks_for_another_shot(issue: str) -> bool:
    text = issue.lower()
    return any(
        phrase in text
        for phrase in ("separate shot", "another shot", "new shot", "adjacent shot", "split")
    )


def _clip_shots(development: Development, screenplay: Screenplay) -> tuple[Screenplay, int]:
    """Last resort after rewrites: thin an over-budget list before any image call."""
    budget = shot_budget(development.runtime_seconds)
    count = len(screenplay.shots)
    if count <= budget:
        return screenplay, 0
    kept = [screenplay.shots[index] for index in sample_indexes(count, budget)]
    note = (
        f"Shot list clipped from {count} to {len(kept)} so a {development.runtime_seconds}s clip "
        f"does not generate one still per fragment."
    )
    notes = [note, *[item for item in screenplay.notes if item != note]][:2]
    return screenplay.model_copy(update={"shots": kept, "notes": notes}), count


def _cap_stills(development: Development, package: StillPackage) -> tuple[StillPackage, int]:
    limited = limit_still_package(
        package,
        runtime_seconds=development.runtime_seconds,
        character_count=len(development.characters),
        location_count=len(development.locations),
    )
    return limited, len(package.jobs) - len(limited.jobs)


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
