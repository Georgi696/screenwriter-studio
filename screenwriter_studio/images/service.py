"""Stills desk — writes jobs.json and calls KIE.ai. No model choice here; the generator routes."""

import json
from collections.abc import Callable
from pathlib import Path

from screenwriter_studio.budget import select_stills
from screenwriter_studio.images.catalog import card_for, prompt_length_error
from screenwriter_studio.images.generator import _ref_list, choose, run_batch, waves
from screenwriter_studio.models import ART_MODEL
from screenwriter_studio.schemas import Development, Screenplay, StillPackage


def validate_still_package(
    package: StillPackage,
    development: Development,
    screenplay: Screenplay,
    *,
    image_count: int | None = None,
    reference_ids: list[str] | None = None,
) -> None:
    """Reject incomplete or over-budget plans without silently dropping references."""
    if package.aspect_ratio != development.aspect_ratio:
        raise ValueError("Still aspect ratio differs from the locked production.")
    jobs = [(job.id, job.kind) for job in package.jobs]
    if image_count is not None and len(package.jobs) > image_count:
        raise ValueError(
            f"Still plan has {len(package.jobs)} images; the limit is {image_count}. No images were submitted."
        )
    chosen = select_stills(
        jobs, runtime_seconds=development.runtime_seconds,
        character_count=len(development.characters), location_count=len(development.locations),
        max_shots=development.max_shots,
        max_images=image_count,
    )
    if chosen != jobs:
        raise ValueError("Still plan exceeds its limits, contains duplicate IDs, or uses unsupported kinds. No images were submitted.")
    expected = {f"s{shot.number:02d}" for shot in screenplay.shots}
    known = set(reference_ids or ())
    if known & {job_id for job_id, _kind in jobs}:
        raise ValueError("Locked reference ids must not reuse still job ids. No images were submitted.")
    actual = {job.id for job in package.jobs if job.kind in {"keyframe", "scene"}}
    if image_count is not None and image_count < len(expected):
        if not actual <= expected:
            raise ValueError(
                "Still plan keyframes must use shot ids and stay within the image limit. No images were submitted."
            )
    elif actual != expected:
        raise ValueError("Still plan must contain exactly one keyframe per shot, named s01, s02, etc.")
    waves([job.model_dump() for job in package.jobs], known)


STYLE_KINDS = {"keyframe", "scene", "location"}


def style_note(count: int) -> str:
    images = "reference image sets" if count == 1 else f"{count} reference images set"
    return (
        f"\n\nStyle reference: the last {images} the look only. Match the colour grade, lighting, "
        "lens character, and texture. Do not copy their people, wardrobe, places, objects, or composition."
    )


def apply_style_references(jobs: list[dict], style_ids: list[str], aspect: str, resolution: str) -> list[dict]:
    """Style images go last on every keyframe and location, and nowhere else.

    A shot keeps its subject references first; style images fill what the model's
    reference limit leaves. The prompt says the trailing images are look only.
    """
    if not style_ids:
        return jobs
    styled = []
    for job in jobs:
        job = dict(job)
        subject = [ref for ref in _ref_list(job) if ref not in style_ids]
        if job.get("kind") not in STYLE_KINDS:
            job["references"] = subject
            styled.append(job)
            continue
        model, _ = choose({**job, "references": subject}, aspect, resolution)
        room = max(0, card_for(model).max_images - len(subject))
        extra = style_ids[:room]
        prompt = str(job.get("prompt") or "").rstrip()
        if extra and not prompt_length_error(model, prompt + style_note(len(extra))):
            job["references"] = subject + extra
            job["prompt"] = prompt + style_note(len(extra))
        else:
            job["references"] = subject
        styled.append(job)
    return styled


def render_stills(
    package: StillPackage,
    out: Path,
    *,
    dry_run: bool = False,
    on_record: Callable[[dict], None] | None = None,
    on_start: Callable[[dict], None] | None = None,
    cancel=None,
    inspection_mode: str = "manual",
    inspection_context: dict | None = None,
    locked_references: list[dict] | None = None,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    style_ids = [item["id"] for item in locked_references or [] if item.get("mode") == "style"]
    spec = {
        "inspection": {"mode": inspection_mode, "model": ART_MODEL},
        "inspection_context": inspection_context or {},
        "aspect_ratio": package.aspect_ratio,
        "resolution": package.resolution,
        "jobs": apply_style_references(
            [job.model_dump() for job in package.jobs], style_ids, package.aspect_ratio, package.resolution,
        ),
    }
    if locked_references:
        spec["locked_references"] = locked_references
    (out / "jobs.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return run_batch(spec, out, dry_run=dry_run, on_record=on_record, on_start=on_start, cancel=cancel)
