"""Configurable shot/keyframe count limits, separate from exact timeline duration.

The default remains one keyframe per eight seconds. This is a generation-count
policy, not a required shot length or a monetary spending limit.
"""

from __future__ import annotations

from screenwriter_studio.videos.catalog import clip_bounds

CLIP_SECONDS = 8


def shot_budget(runtime_seconds: int, max_shots: int | None = None, selection: str = "auto") -> int:
    """Upper bound on shots, and on the keyframe stills those shots become.

    The floor is one shot per longest clip the selected video model can make.
    Seedance 2.5 can hold 30 seconds, so a 30-second film can be one clip.
    """
    runtime = max(0, int(runtime_seconds))
    shortest, longest = clip_bounds(selection)
    minimum = max(1, (runtime + longest - 1) // longest) if runtime else 1
    maximum = min(240, max(1, runtime // shortest)) if runtime else 1
    if max_shots is not None:
        if not minimum <= max_shots <= maximum:
            raise ValueError(f"Shot limit must be between {minimum} and {maximum} for {runtime}s.")
        return max_shots
    return max(minimum, runtime // CLIP_SECONDS)


KEYFRAME_KINDS = {"keyframe", "scene"}
TEXT_KINDS = {"poster", "title", "logo", "ui", "text"}
PRODUCT_KINDS = {"product", "packshot"}


def still_caps(*, runtime_seconds: int, character_count: int, location_count: int, max_shots: int | None = None) -> dict[str, int]:
    return {
        "character": max(0, character_count),
        "location": max(0, location_count),
        "keyframe": shot_budget(runtime_seconds, max_shots),
        "text": 1,
        "product": 1,
    }


def still_bucket(kind: str) -> str | None:
    if kind in KEYFRAME_KINDS:
        return "keyframe"
    if kind in TEXT_KINDS:
        return "text"
    if kind in PRODUCT_KINDS:
        return "product"
    if kind in {"character", "location"}:
        return kind
    return None


def select_stills(
    jobs: list[tuple[str, str]],
    *,
    runtime_seconds: int,
    character_count: int,
    location_count: int,
    max_shots: int | None = None,
    max_images: int | None = None,
) -> list[tuple[str, str]]:
    """Keep the stills a runtime budgeted. `jobs` are `(id, kind)`, in generation order."""
    caps = still_caps(
        runtime_seconds=runtime_seconds,
        character_count=character_count,
        location_count=location_count,
        max_shots=max_shots,
    )
    counts = {bucket: 0 for bucket in caps}
    kept: list[tuple[str, str]] = []
    seen: set[str] = set()
    for job_id, kind in jobs:
        if job_id in seen:
            continue
        bucket = still_bucket(kind)
        if bucket is None or counts[bucket] >= caps[bucket]:
            continue
        if max_images is not None and len(kept) >= max_images:
            continue
        seen.add(job_id)
        counts[bucket] += 1
        kept.append((job_id, kind))
    return kept
