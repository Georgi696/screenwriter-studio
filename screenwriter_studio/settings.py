"""Studio-wide model and quality choices, saved once and used by every stage.

The registry below is the list of models the studio can actually call, built from the
checked image and video catalogs plus the chat models in models.py. A model that is not
in a catalog has no verified request contract, so it cannot be selected here. To offer
another KIE model, add its card to the matching catalog and list it in this registry.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

from screenwriter_studio.images.catalog import (
    CARD_BY_ID, EDIT_CARD, GPT_CARD, NANO_CARD, NANO_PRO_CARD, RESOLUTION_VALUES, SEEDREAM_CARD,
)
from screenwriter_studio import pricing
from screenwriter_studio.paths import STATE_DIR
from screenwriter_studio.videos.catalog import CARDS as VIDEO_CARDS, KLING, SEEDANCE, clip_bounds

PATH = Path(os.environ.get("STUDIO_SETTINGS_PATH") or STATE_DIR / "studio_settings.json")

CHAT_MODELS = (
    {"id": "gpt-6-astra", "name": "GPT-6 Astra",
     "note": "Premium reasoning model, 272K context. Best for story, scripts and image prompts.",
     "sources": ("https://docs.kie.ai/market/chat/gpt-6-astra",)},
    {"id": "gpt-6-1-sol", "name": "GPT-6.1 Sol",
     "note": "Close to Astra, lighter and quicker. Accepts images and strict JSON. Suited to the script editor.",
     "sources": ("https://docs.kie.ai/market/chat/gpt-6-1-sol",)},
    {"id": "gpt-6-sol", "name": "GPT-6 Sol",
     "note": "Earlier Sol on the same endpoint. Accepts images and strict JSON.",
     "sources": ("https://docs.kie.ai/market/chat/gpt-6-sol",)},
    {"id": "gpt-6-luna", "name": "GPT-6 Luna",
     "note": "Faster, more affordable GPT-6 on the same endpoint. Accepts images and strict JSON.",
     "sources": ("https://docs.kie.ai/market/chat/gpt-6-luna",)},
)
CHAT_ROLES = (
    {"id": "development", "group": "story", "label": "Development", "help": "Locks the story, beats, cast and look."},
    {"id": "writer", "group": "story", "label": "Screenwriter", "help": "Writes the screenplay and the shot list."},
    {"id": "editor", "group": "story", "label": "Script editor", "help": "Passes or sends back each draft."},
    {"id": "art", "group": "images", "label": "Image prompts", "help": "Writes one image prompt per character, location and shot."},
    {"id": "inspection", "group": "images", "label": "Image inspector", "help": "Looks at each generated image. The model must accept image input."},
)
# Reasoning effort per role. "default" sends no reasoning field, so the model's own default
# applies. "low" is already used on this endpoint by the brief suggester; medium and high are
# the other values of the OpenAI Responses enum the SDK sends. KIE's chat pages did not
# publish the enum when this was written, so a rejected value surfaces as a run error.
EFFORTS = (
    {"id": "default", "name": "Effort · model default"},
    {"id": "low", "name": "Effort · low (fastest)"},
    {"id": "medium", "name": "Effort · medium"},
    {"id": "high", "name": "Effort · high (most thorough)"},
)
IMAGE_GROUPS = (
    {"id": "cinematic", "label": "Characters, locations and shot images",
     "help": "Every shot image needs reference images, so only a model that accepts them can be used.",
     "options": (NANO_CARD, NANO_PRO_CARD)},
    {"id": "text", "label": "Images with readable text",
     "help": "Posters, titles, logos and interface cards.",
     "options": (GPT_CARD, NANO_PRO_CARD, NANO_CARD)},
    {"id": "product", "label": "Product and packshot images",
     "help": "Photoreal objects with no cast reference.",
     "options": (SEEDREAM_CARD, GPT_CARD, NANO_PRO_CARD, NANO_CARD)},
)
IMAGE_RESOLUTIONS = ("auto", *RESOLUTION_VALUES)
VIDEO_SELECTIONS = ("auto", "kling", "seedance")
_VIDEO_BY_SELECTION = {"kling": KLING, "seedance": SEEDANCE}

DEFAULTS = {
    "chat": {"development": "gpt-6-astra", "writer": "gpt-6-astra", "editor": "gpt-6-1-sol",
             "art": "gpt-6-astra", "inspection": "gpt-6-astra"},
    "images": {"cinematic": NANO_CARD.model_id, "text": GPT_CARD.model_id,
               "product": SEEDREAM_CARD.model_id, "resolution": "auto", "parallel": 3},
    "video": {"selection": "auto", "mode": "pro", "sound": True, "parallel": 3},
    "effort": {"development": "default", "writer": "default", "editor": "default",
               "art": "default", "inspection": "default"},
}


def video_modes(selection: str) -> tuple[str, ...]:
    """Quality levels a model selection can deliver. Auto may use either model."""
    if selection in _VIDEO_BY_SELECTION:
        return VIDEO_CARDS[_VIDEO_BY_SELECTION[selection]].modes
    return tuple(dict.fromkeys(mode for card in VIDEO_CARDS.values() for mode in card.modes))


def schema() -> dict:
    """Everything the desk needs to draw the settings, so new models appear without UI changes."""
    return {
        "chat": {"roles": list(CHAT_ROLES), "models": [
            {"id": model["id"], "name": model["name"], "note": model["note"], "sources": list(model["sources"])}
            for model in CHAT_MODELS
        ], "efforts": [dict(effort) for effort in EFFORTS]},
        "images": {
            "groups": [{
                "id": group["id"], "label": group["label"], "help": group["help"],
                "options": [{
                    "id": card.model_id, "name": _image_name(card.model_id), "note": card.purpose + _image_price(card.model_id),
                    "resolutions": list(card.resolutions), "ratios": list(card.ratios),
                    "max_references": card.max_images, "sources": list(card.sources),
                } for card in group["options"]],
            } for group in IMAGE_GROUPS],
            "resolutions": list(IMAGE_RESOLUTIONS),
            "parallel": [1, 2, 3, 4],
        },
        "video": {
            "models": [{"id": "auto", "name": "Auto · choose for each shot",
                        "note": "Picks a compatible model per shot from timing, speech, subjects and camera.",
                        "duration_min": clip_bounds("auto")[0], "duration_max": clip_bounds("auto")[1],
                        "modes": list(video_modes("auto")), "sources": []}] + [{
                "id": key, "name": VIDEO_CARDS[model].name,
                "note": f"{VIDEO_CARDS[model].duration_min}–{VIDEO_CARDS[model].duration_max} s per clip · "
                        f"{', '.join(VIDEO_CARDS[model].ratios)} · prompts up to {VIDEO_CARDS[model].prompt_max} characters"
                        + _video_price(model),
                "duration_min": VIDEO_CARDS[model].duration_min,
                "duration_max": VIDEO_CARDS[model].duration_max,
                "modes": list(VIDEO_CARDS[model].modes), "sources": list(VIDEO_CARDS[model].sources),
            } for key, model in _VIDEO_BY_SELECTION.items()],
            "modes": {"std": "Standard · 720p", "pro": "Pro · 1080p", "4K": "4K"},
            "parallel": [1, 2, 3, 4],
        },
        "defaults": deepcopy(DEFAULTS),
        "pricing": pricing.schema(),
    }


def _image_price(model_id: str) -> str:
    rates = pricing.IMAGE_RATES.get(model_id)
    if not rates:
        return ""
    return " · " + ", ".join(f"{size} {credits:g}" for size, credits in rates.items()) + " credits per image"


def _video_price(model_id: str) -> str:
    rates = pricing.VIDEO_RATES.get(model_id, {})
    listed = {mode: (rates.get((mode, True)), rates.get((mode, False))) for mode in VIDEO_CARDS[model_id].modes}
    parts = [f"{mode} {sound:g}" if sound == silent else f"{mode} {sound:g} with sound, {silent:g} silent"
             for mode, (sound, silent) in listed.items() if sound is not None]
    return (" · credits per second: " + "; ".join(parts)) if parts else ""


def _image_name(model_id: str) -> str:
    return {NANO_CARD.model_id: "Nano Banana 2.1", NANO_PRO_CARD.model_id: "Nano Banana Pro",
            GPT_CARD.model_id: "GPT Image · Sunburst",
            SEEDREAM_CARD.model_id: "Seedream 5 Pro", EDIT_CARD.model_id: "GPT Image · Sunburst edit"}.get(model_id, model_id)


def validate(raw: object) -> dict:
    """A complete, legal settings object. Missing values take defaults; illegal ones are errors."""
    if not isinstance(raw, dict):
        raise ValueError("Settings must be an object.")
    result = deepcopy(DEFAULTS)
    chat = raw.get("chat", {})
    if not isinstance(chat, dict):
        raise ValueError("Chat settings must be an object.")
    known_chat = {model["id"] for model in CHAT_MODELS}
    for role in CHAT_ROLES:
        value = chat.get(role["id"], result["chat"][role["id"]])
        if value not in known_chat:
            raise ValueError(f"{role['label']}: choose one of the listed chat models.")
        result["chat"][role["id"]] = value
    effort = raw.get("effort", {})
    if not isinstance(effort, dict):
        raise ValueError("Reasoning effort settings must be an object.")
    known_effort = {item["id"] for item in EFFORTS}
    for role in CHAT_ROLES:
        value = effort.get(role["id"], "default")
        if value not in known_effort:
            raise ValueError(f"{role['label']}: reasoning effort must be default, low, medium or high.")
        result["effort"][role["id"]] = value
    images = raw.get("images", {})
    if not isinstance(images, dict):
        raise ValueError("Image settings must be an object.")
    for group in IMAGE_GROUPS:
        value = images.get(group["id"], result["images"][group["id"]])
        if value not in {card.model_id for card in group["options"]}:
            raise ValueError(f"{group['label']}: choose one of the listed image models.")
        result["images"][group["id"]] = value
    resolution = images.get("resolution", "auto")
    if resolution not in IMAGE_RESOLUTIONS:
        raise ValueError("Image resolution must be auto, 1K, 2K or 4K.")
    result["images"]["resolution"] = resolution
    image_parallel = images.get("parallel", 3)
    if type(image_parallel) is not int or not 1 <= image_parallel <= 4:
        raise ValueError("Images generated at once must be between 1 and 4.")
    result["images"]["parallel"] = image_parallel
    video = raw.get("video", {})
    if not isinstance(video, dict):
        raise ValueError("Video settings must be an object.")
    selection = video.get("selection", "auto")
    mode = video.get("mode", "pro")
    sound = video.get("sound", True)
    parallel = video.get("parallel", 3)
    if selection not in VIDEO_SELECTIONS:
        raise ValueError("Choose auto, kling, or seedance for the video model.")
    if mode not in video_modes(selection):
        raise ValueError(f"{VIDEO_CARDS[_VIDEO_BY_SELECTION[selection]].name if selection in _VIDEO_BY_SELECTION else 'Video'} "
                         f"does not offer {mode} output.")
    if type(sound) is not bool:
        raise ValueError("Sound must be on or off.")
    if type(parallel) is not int or not 1 <= parallel <= 4:
        raise ValueError("Clips generated at once must be between 1 and 4.")
    result["video"] = {"selection": selection, "mode": mode, "sound": sound, "parallel": parallel}
    return result


def load() -> dict:
    """Saved settings, or the defaults when nothing valid is saved."""
    try:
        return validate(json.loads(PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return deepcopy(DEFAULTS)


def save(raw: object) -> dict:
    values = validate(raw)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    temporary.replace(PATH)
    return values


def chat_agent(role: str, agent):
    """The crew agent on the chosen chat model and reasoning effort. Unchanged when both are defaults."""
    from screenwriter_studio.models import kie_chat_model
    values = load()
    chosen, effort = values["chat"][role], values["effort"][role]
    changes = {}
    if chosen != DEFAULTS["chat"][role]:
        changes["model"] = kie_chat_model(chosen)
    if effort != "default":
        from agents import ModelSettings
        from openai.types.shared import Reasoning
        changes["model_settings"] = agent.model_settings.resolve(ModelSettings(reasoning=Reasoning(effort=effort)))
    return agent.clone(**changes) if changes else agent


def reasoning_field(effort: str | None) -> dict:
    """The Responses `reasoning` argument for a direct client call; empty for the model default."""
    return {"reasoning": {"effort": effort}} if effort and effort != "default" else {}


def apply_images(jobs: list[dict], resolution: str, values: dict | None = None) -> tuple[list[dict], str]:
    """Route kinds to the chosen models and apply a forced resolution.

    The model goes on each job so a resumed batch keeps the choice it was started with.
    The generator still falls back to Nano Banana when a ratio or resolution is illegal.
    """
    values = values or load()
    chosen = values["images"]
    routed = {}
    for group in IMAGE_GROUPS:
        for kind in _GROUP_KINDS[group["id"]]:
            routed[kind] = chosen[group["id"]]
    updated = []
    for job in jobs:
        job = dict(job)
        model = routed.get(str(job.get("kind") or ""))
        if model and not job.get("model") and model != _DEFAULT_ROUTE.get(job.get("kind")):
            job["model"] = model
        updated.append(job)
    forced = chosen["resolution"]
    return updated, resolution if forced == "auto" else forced


_GROUP_KINDS = {
    "cinematic": ("character", "location", "keyframe", "scene"),
    "text": ("poster", "title", "logo", "ui", "text"),
    "product": ("product", "packshot"),
}
_DEFAULT_ROUTE = {kind: DEFAULTS["images"][group] for group, kinds in _GROUP_KINDS.items() for kind in kinds}
assert all(model in CARD_BY_ID for model in _DEFAULT_ROUTE.values())
