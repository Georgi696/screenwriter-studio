"""Stills desk — writes jobs.json and calls KIE.ai. No model choice here; the generator routes."""

import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from budget import select_stills
from generate_stills import run_batch
from schemas import StillPackage


def limit_still_package(
    package: StillPackage,
    *,
    runtime_seconds: int,
    character_count: int,
    location_count: int,
) -> StillPackage:
    """Drop stills a runtime did not budget, before any image call."""
    chosen = select_stills(
        [(job.id, job.kind) for job in package.jobs],
        runtime_seconds=runtime_seconds,
        character_count=character_count,
        location_count=location_count,
    )
    if len(chosen) == len(package.jobs):
        return package
    by_id = {job.id: job for job in package.jobs}
    return package.model_copy(update={"jobs": [by_id[job_id] for job_id, _kind in chosen]})


def render_stills(
    package: StillPackage,
    out: Path,
    *,
    dry_run: bool = False,
    on_record: Callable[[dict], None] | None = None,
    on_start: Callable[[dict], None] | None = None,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    spec = {
        "aspect_ratio": package.aspect_ratio,
        "resolution": package.resolution,
        "jobs": [job.model_dump() for job in package.jobs],
    }
    (out / "jobs.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return run_batch(spec, out, dry_run=dry_run, on_record=on_record, on_start=on_start)
