"""KIE credit prices for the models the studio calls, and estimates before each paid step.

Prices were read from KIE's public price list on CHECKED. KIE bills what that page says on
the day, so an estimate is a guide, not a quote. Chat calls (development, writing, image
prompts and inspections) are billed per token and are not estimated here.
"""

from __future__ import annotations

from screenwriter_studio.images.catalog import EDIT_CARD, GPT_CARD, NANO_CARD, NANO_PRO_CARD, SEEDREAM_CARD
from screenwriter_studio.videos.catalog import CARDS as VIDEO_CARDS, KLING, SEEDANCE, choose_model

CHECKED = "2026-10-14"
SOURCE = "https://kie.ai/pricing"
USD_PER_CREDIT = 0.005  # KIE lists every price in credits and in dollars at 200 credits per dollar.

# Credits per second of video. Kling 3.0: the "Kling 3.0, video" rows at 720P (std), 1080P (pro) and 4K.
# Seedance 2.5: the rows without a reference video (std is 720p, pro is 1080p); KIE lists no separate audio price.
VIDEO_RATES = {
    KLING: {("std", True): 20, ("std", False): 14, ("pro", True): 27, ("pro", False): 18,
            ("4K", True): 67, ("4K", False): 67},
    SEEDANCE: {("std", True): 63, ("std", False): 63, ("pro", True): 158, ("pro", False): 158},
}
# Credits per image by output size. Seedream 5 Pro sends basic quality for 1K and high for 2K.
IMAGE_RATES = {
    NANO_CARD.model_id: {"1K": 8, "2K": 12, "4K": 18},
    NANO_PRO_CARD.model_id: {"1K": 18, "2K": 18, "4K": 24},
    GPT_CARD.model_id: {"1K": 6, "2K": 10, "4K": 16},
    EDIT_CARD.model_id: {"1K": 6, "2K": 10, "4K": 16},
    SEEDREAM_CARD.model_id: {"1K": 7, "2K": 14},
}
ASSUMPTIONS = {
    NANO_CARD.model_id: "KIE's price list names Nano Banana 2, not 2.1; that price is used.",
}
EXTRAS = "Chat calls, image inspections and repairs are extra."


def usd(credits: float) -> float:
    return round(credits * USD_PER_CREDIT, 2)


def describe(credits: float) -> str:
    return f"{credits:,.0f} credits (about ${usd(credits):,.2f})"


def video_rate(model_id: str, mode: str, sound: bool) -> float | None:
    return VIDEO_RATES.get(model_id, {}).get((mode, bool(sound)))


def image_rate(model_id: str, resolution: str) -> float | None:
    rates = IMAGE_RATES.get(model_id, {})
    return rates.get(resolution if resolution in rates else "1K")


def planned_resolution(choice: str) -> str:
    """Auto leaves the size to the art director, who writes 1K unless the brief asks for finals."""
    return choice if choice in {"1K", "2K", "4K"} else "1K"


def video_estimate(development, screenplay, *, mode: str, sound: bool, selection: str) -> dict:
    """One clip per shot, on the model the router would choose today."""
    shots, unpriced, total = [], [], 0.0
    for shot in screenplay.shots:
        shot_id = f"s{shot.number:02d}"
        try:
            card, _ = choose_model(development, shot, mode=mode, sound=sound, selection=selection)
        except ValueError:
            unpriced.append(shot_id)
            continue
        rate = video_rate(card.id, mode, sound)
        if rate is None:
            unpriced.append(shot_id)
            continue
        credits = rate * shot.duration_seconds
        total += credits
        shots.append({"id": shot_id, "model": card.id, "seconds": shot.duration_seconds, "credits": credits})
    return {"credits": total, "usd": usd(total), "shots": shots, "unpriced": unpriced}


def plan_estimate(plan: dict, clips: list[dict] | None = None) -> dict:
    """A saved or rebuilt clip plan: the price of every clip, and of the ones not finished yet."""
    done = {clip.get("id") for clip in clips or [] if clip.get("state") == "done"}
    shots, remaining, total = {}, 0.0, 0.0
    for shot in plan.get("shots", []):
        rate = video_rate(shot.get("model", plan.get("model")), plan.get("mode", "pro"), plan.get("sound", True))
        if rate is None:
            continue
        credits = rate * int(shot.get("duration_seconds") or 0)
        shots[shot["id"]] = credits
        total += credits
        if shot["id"] not in done:
            remaining += credits
    return {"credits": total, "usd": usd(total), "remaining": remaining, "remaining_usd": usd(remaining), "shots": shots}


def image_estimate(development, screenplay, *, values: dict, image_count: int | None = None, locked_count: int = 0) -> dict:
    """Before the art director runs: one image per shot plus a sheet per character and location."""
    count = len(screenplay.shots) + max(0, len(development.characters) + len(development.locations) - locked_count)
    if image_count is not None:
        count = min(count, image_count)
    model = values["images"]["cinematic"]
    resolution = planned_resolution(values["images"]["resolution"])
    rate = image_rate(model, resolution) or 0
    return {"count": count, "model": model, "resolution": resolution, "per_image": rate,
            "credits": count * rate, "usd": usd(count * rate), "assumption": ASSUMPTIONS.get(model, "")}


def remaining_images(spec: dict, manifest: dict) -> dict:
    """Images in a saved plan that are not finished yet, on the model each job will actually use."""
    from screenwriter_studio.images.generator import choose

    done = {record.get("id") for record in manifest.get("images", []) if record.get("state") in {"done", "review"}}
    aspect, resolution = spec.get("aspect_ratio", "16:9"), spec.get("resolution", "1K")
    count, total = 0, 0.0
    for job in spec.get("jobs", []):
        if job.get("id") in done:
            continue
        model, _ = choose(job, aspect, resolution)
        total += image_rate(model, resolution) or 0
        count += 1
    return {"count": count, "credits": total, "usd": usd(total)}


def production_estimate(development, screenplay, *, values: dict | None = None,
                        image_count: int | None = None, locked_count: int = 0) -> dict:
    """What approving the pages leads to: the images now, then one clip per shot."""
    if values is None:
        from screenwriter_studio.settings import load
        values = load()
    video = values["video"]
    images = image_estimate(development, screenplay, values=values, image_count=image_count, locked_count=locked_count)
    clips = video_estimate(development, screenplay, mode=video["mode"], sound=video["sound"], selection=video["selection"])
    total = images["credits"] + clips["credits"]
    text = (f"Estimated cost: {describe(total)}. Images: about {images['count']} at {images['resolution']}, "
            f"{describe(images['credits'])}. Video clips: {sum(s['seconds'] for s in clips['shots'])}s, "
            f"{describe(clips['credits'])}.")
    if clips["unpriced"]:
        text += f" Not priced: {', '.join(clips['unpriced'])}."
    text += f" {EXTRAS} Prices from {SOURCE}, checked {CHECKED}."
    return {"credits": total, "usd": usd(total), "images": images, "videos": clips, "text": text,
            "checked": CHECKED, "source": SOURCE}


def schema() -> dict:
    """Rates the desk uses for its rough estimate on the Idea tab and in model notes."""
    return {
        "checked": CHECKED, "source": SOURCE, "usd_per_credit": USD_PER_CREDIT, "extras": EXTRAS,
        "video": {key: {f"{mode}/{'sound' if sound else 'silent'}": rate for (mode, sound), rate in VIDEO_RATES[model].items()}
                  for key, model in (("kling", KLING), ("seedance", SEEDANCE))},
        "video_names": {"kling": VIDEO_CARDS[KLING].name, "seedance": VIDEO_CARDS[SEEDANCE].name},
        "images": {model: dict(rates) for model, rates in IMAGE_RATES.items()},
        "assumptions": dict(ASSUMPTIONS),
    }
