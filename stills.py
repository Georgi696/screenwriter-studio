"""Stills desk — writes jobs.json and calls KIE.ai. No model choice here; the generator routes."""

import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from budget import select_stills
from generate_stills import run_batch
from schemas import Development, Screenplay, StillPackage
from generate_stills import waves


def validate_still_package(package: StillPackage, development: Development, screenplay: Screenplay) -> None:
    """Reject incomplete or over-budget plans without silently dropping references."""
    if package.aspect_ratio != development.aspect_ratio:
        raise ValueError("Still aspect ratio differs from the locked production.")
    jobs = [(job.id, job.kind) for job in package.jobs]
    chosen = select_stills(
        jobs, runtime_seconds=development.runtime_seconds,
        character_count=len(development.characters), location_count=len(development.locations),
        max_shots=development.max_shots,
    )
    if chosen != jobs:
        raise ValueError("Still plan exceeds its limits, contains duplicate IDs, or uses unsupported kinds. No images were submitted.")
    expected = {f"s{shot.number:02d}" for shot in screenplay.shots}
    actual = {job.id for job in package.jobs if job.kind in {"keyframe", "scene"}}
    if actual != expected:
        raise ValueError("Still plan must contain exactly one keyframe per shot, named s01, s02, etc.")
    waves([job.model_dump() for job in package.jobs])


def render_stills(
    package: StillPackage,
    out: Path,
    *,
    dry_run: bool = False,
    on_record: Callable[[dict], None] | None = None,
    on_start: Callable[[dict], None] | None = None,
    cancel=None,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    spec = {
        "aspect_ratio": package.aspect_ratio,
        "resolution": package.resolution,
        "jobs": [job.model_dump() for job in package.jobs],
    }
    (out / "jobs.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return run_batch(spec, out, dry_run=dry_run, on_record=on_record, on_start=on_start, cancel=cancel)
