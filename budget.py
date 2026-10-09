"""How many shots — and therefore keyframe stills — a runtime may have.

A clip lands around 8 seconds, the long end of the 5–8s range. Kling can
run as short as 3s, and a writer who uses that floor turns a minute into
fifteen generated images. The cap is one shot per 8 seconds: a 60s piece
is 7 shots, a 30s spot is 3.
"""

from __future__ import annotations

CLIP_SECONDS = 8


def shot_budget(runtime_seconds: int) -> int:
    """Upper bound on shots, and on the keyframe stills those shots become."""
    runtime = max(0, int(runtime_seconds))
    return max(1, runtime // CLIP_SECONDS)


KEYFRAME_KINDS = {"keyframe", "scene"}
TEXT_KINDS = {"poster", "title", "logo", "ui", "text"}
PRODUCT_KINDS = {"product", "packshot"}


def still_caps(*, runtime_seconds: int, character_count: int, location_count: int) -> dict[str, int]:
    return {
        "character": max(0, character_count),
        "location": max(0, location_count),
        "keyframe": shot_budget(runtime_seconds),
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
) -> list[tuple[str, str]]:
    """Keep the stills a runtime budgeted. `jobs` are `(id, kind)`, in generation order."""
    caps = still_caps(
        runtime_seconds=runtime_seconds,
        character_count=character_count,
        location_count=location_count,
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
        seen.add(job_id)
        counts[bucket] += 1
        kept.append((job_id, kind))
    return kept


def sample_indexes(count: int, budget: int) -> list[int]:
    """Evenly spaced indexes, always including the first and the last.

    Used only when a draft is still over budget after rewrites, so the
    opening and the button survive and the middle is thinned.
    """
    if count <= 0 or budget <= 0:
        return []
    if budget >= count:
        return list(range(count))
    if budget == 1:
        return [0]
    raw = [round(i * (count - 1) / (budget - 1)) for i in range(budget)]
    used: set[int] = set()
    indexes: list[int] = []
    for index in raw:
        while index in used and index + 1 < count:
            index += 1
        if index not in used:
            used.add(index)
            indexes.append(index)
    return indexes
