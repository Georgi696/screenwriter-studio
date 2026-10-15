"""Stills desk — writes jobs.json and calls KIE.ai. No model choice here; the generator routes."""

import json
import re
from collections.abc import Callable
from pathlib import Path

from screenwriter_studio.budget import select_stills
from screenwriter_studio.images.catalog import NANO_BANANA_MODEL, card_for, prompt_length_error
from screenwriter_studio.images.generator import _ref_list, choose, run_batch, waves
from screenwriter_studio import settings as studio_settings
from screenwriter_studio.models import ART_MODEL
from screenwriter_studio.schemas import Development, Screenplay, StillPackage
from screenwriter_studio.references import reference_direction, validate_reference_shots


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
        # Keep one slot free: a repair sends the candidate image first, then these references.
        room = max(0, card_for(model).max_images - len(subject) - 1)
        extra = style_ids[:room]
        prompt = str(job.get("prompt") or "").rstrip()
        if extra and not prompt_length_error(model, prompt + style_note(len(extra))):
            job["references"] = subject + extra
            job["prompt"] = prompt + style_note(len(extra))
        else:
            job["references"] = subject
        styled.append(job)
    return styled


def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", str(text).lower()) if token]


def apply_reference_settings(jobs: list[dict], references: list[dict], shots: list[dict], aspect: str, resolution: str) -> list[dict]:
    """Enforce explicit shot scope and carry each attached image's role into the prompt."""
    if not references:
        return jobs
    validate_reference_shots(references, shots)
    by_shot = {f"s{shot['number']:02d}": shot for shot in shots}
    supplied = {ref["id"] for ref in references}
    generated_shots = {job["id"] for job in jobs if job.get("kind") in {"keyframe", "scene"}}
    for ref in references:
        if any(f"s{n:02d}" not in generated_shots for n in ref.get("shots", [])):
            raise ValueError(f"Reference {ref.get('name') or ref['id']} selects a shot without a planned image. Increase the image limit.")
    result = []
    for original in jobs:
        job = dict(original)
        requested = _ref_list(job)
        attached = list(dict.fromkeys(ref for ref in requested if ref not in supplied))
        shot = by_shot.get(job["id"]) if job.get("kind") in {"keyframe", "scene"} else None
        selected = []
        for ref in references:
            mode = ref.get("mode", "lock")
            use = ref["id"] in requested
            if ref.get("shots"):
                use = bool(shot and shot["number"] in ref["shots"])
            elif mode == "style":
                use = job.get("kind") in STYLE_KINDS
            elif mode == "composition" and not shot:
                use = False
            elif mode in {"character", "location"} and shot:
                name = str(ref.get("name", "")).strip().casefold()
                # Exact screenplay names are authoritative. Unnamed image files
                # still rely on the art director's explicit attachment decisions.
                names = {str(n).strip().casefold() for s in shots for n in
                         (s.get("characters", []) if mode == "character" else [s.get("location", "")])}
                if name and name in names:
                    visible = shot.get("characters", []) if mode == "character" else [shot.get("location", "")]
                    use = name in {str(n).strip().casefold() for n in visible}
            if use:
                selected.append(ref)
        # Keep style references last, as in existing productions.
        selected.sort(key=lambda ref: ref.get("mode") == "style")
        attached.extend(ref["id"] for ref in selected)
        job["references"] = attached
        if selected:
            directions = [f"Reference image {attached.index(ref['id']) + 1} ({ref.get('name') or ref['id']}): {reference_direction(ref)}"
                          for ref in selected]
            job["prompt"] = str(job.get("prompt") or "").rstrip() + "\n\nReference directions:\n" + "\n".join(directions)
        model, _ = choose(job, aspect, resolution)
        # A repair needs one additional slot for the candidate image.
        if len(attached) > max(0, card_for(model).max_images - 1):
            raise ValueError(f"{job['id']}: too many references for {model} with a repair slot. Reduce references or narrow their shot scope. No images were submitted.")
        error = prompt_length_error(model, job.get("prompt", ""))
        if error:
            raise ValueError(f"{job['id']}: {error} Shorten reference instructions. No images were submitted.")
        result.append(job)
    return result


def attach_cast(jobs: list[dict], shots: list[dict], *, reference_limit: int | None = None) -> list[dict]:
    """Each shot image gets the sheet of every character its shot lists, even when the prompt writer forgot one.

    A sheet is matched by the character's first name appearing in the sheet id ("maya", "maya-sheet").
    An ambiguous name attaches nothing. One reference slot stays free for a repair.
    reference_limit is the chosen cinematic model's image cap; the default is Nano Banana 2.1.
    """
    limit = card_for(NANO_BANANA_MODEL).max_images if reference_limit is None else reference_limit
    sheets: dict[str, list[str]] = {}
    for job in jobs:
        if job.get("kind") == "character":
            for token in set(_tokens(job.get("id", ""))):
                sheets.setdefault(token, []).append(job["id"])
    cast = {}
    for shot in shots or []:
        if isinstance(shot, dict) and isinstance(shot.get("number"), int) and isinstance(shot.get("characters"), list):
            cast[f"s{shot['number']:02d}"] = [str(name) for name in shot["characters"]]
    attached = []
    for job in jobs:
        names = cast.get(job.get("id")) if job.get("kind") in {"keyframe", "scene"} else None
        if names:
            references = _ref_list(job)
            for name in names:
                first = (_tokens(name) or [""])[0]
                found = sheets.get(first, [])
                if len(found) == 1 and found[0] not in references and len(references) < limit - 1:
                    references.append(found[0])
            job = {**job, "references": references}
        attached.append(job)
    return attached


def portrait_characters(jobs: list[dict]) -> list[dict]:
    """A full-body reference in a wide frame wastes most of its pixels on backdrop.

    Characters are framed 3:4 so the face, hair and distinguishing marks stay readable
    when the sheet is attached to later shots. Other jobs keep the film's ratio.
    """
    return [{**job, "aspect_ratio": "3:4"} if job.get("kind") == "character" and not job.get("aspect_ratio") else job
            for job in jobs]


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
    chosen = studio_settings.load()
    planned = package.resolution if package.resolution in {"1K", "2K", "4K"} else "1K"  # the art director writes free text here
    jobs = attach_cast(
        portrait_characters([job.model_dump() for job in package.jobs]),
        (inspection_context or {}).get("shots", []),
        reference_limit=card_for(chosen["images"]["cinematic"]).max_images,
    )
    jobs, resolution = studio_settings.apply_images(jobs, planned, chosen)
    spec = {
        "inspection": {"mode": inspection_mode, "model": chosen["chat"]["inspection"],
                       "effort": chosen["effort"]["inspection"]},
        "inspection_context": inspection_context or {},
        "aspect_ratio": package.aspect_ratio,
        "resolution": resolution,
        "jobs": apply_reference_settings(jobs, locked_references or [], (inspection_context or {}).get("shots", []), package.aspect_ratio, resolution),
    }
    if locked_references:
        spec["locked_references"] = locked_references
    (out / "jobs.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return run_batch(spec, out, dry_run=dry_run, on_record=on_record, on_start=on_start, cancel=cancel)
