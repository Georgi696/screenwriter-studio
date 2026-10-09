"""Stills desk — writes jobs.json and calls KIE.ai. No model choice here; the generator routes."""

import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from generate_stills import run_batch
from schemas import StillPackage


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
