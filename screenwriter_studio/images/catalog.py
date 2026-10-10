"""Checked constraints for the image models this studio calls.

The stills step does not fetch these pages at runtime. Facts below were read
from the listed sources on CHECKED. Request fields are the KIE createTask
inputs. Prompt rules are only what those pages, or the vendor pages, state.
"""

from __future__ import annotations

from dataclasses import dataclass

CHECKED = "2026-10-09"

CREATE_TASK = "POST https://api.kie.ai/api/v1/jobs/createTask"

# KIE enum order, including auto. Google's Nano Banana 2.1 table lists the
# same named ratios and does not list auto.
NANO_RATIO_VALUES = (
    "1:1", "2:3", "3:2", "1:4", "4:1", "3:4", "4:3", "4:5", "5:4",
    "1:8", "8:1", "9:16", "16:9", "21:9", "auto",
)
# KIE enum. The image-to-image x-apidog labels also name 5:4, 4:5, 2:1, 1:2,
# 3:1, 1:3, and 9:21; those values are not in the enum, so they are not used.
SUNBURST_RATIO_VALUES = (
    "auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9",
    "27:16", "16:27", "9:8", "8:9",
)
SUNBURST_1K_ONLY_VALUES = ("27:16", "16:27", "9:8", "8:9")
SEEDREAM_RATIO_VALUES = ("1:1", "4:3", "3:4", "16:9", "9:16", "2:3", "3:2", "21:9")
RESOLUTION_VALUES = ("1K", "2K", "4K")


@dataclass(frozen=True)
class ImageModel:
    model_id: str
    kinds: tuple[str, ...]
    purpose: str
    sources: tuple[str, ...]
    prompt_min: int
    prompt_max: int
    ratios: tuple[str, ...]
    resolutions: tuple[str, ...]
    resolution_default: str
    documented_fields: tuple[str, ...]
    field_prompt: str
    field_ratio: str
    field_resolution: str
    field_images: str
    max_images: int
    image_required: bool
    field_output_format: str
    output_format: str
    field_quality: str
    one_k_only: tuple[str, ...]
    prompt_rules: str

    def quality_for(self, resolution: str) -> str:
        """KIE: basic outputs 1K, high outputs 2K. There is no 4K value."""
        return "high" if resolution == "2K" else "basic"


NANO_CARD = ImageModel(
    model_id="nano-banana-2-1",
    kinds=("character", "location", "keyframe", "scene"),
    purpose="Cinematic stills that may take reference images.",
    sources=(
        "https://docs.kie.ai/market/google/nanobanana-2-1",
        "https://ai.google.dev/gemini-api/docs/models/gemini-nano-banana-2.1",
        "https://ai.google.dev/gemini-api/docs/image-generation",
    ),
    prompt_min=0,
    prompt_max=20000,
    ratios=NANO_RATIO_VALUES,
    resolutions=RESOLUTION_VALUES,
    resolution_default="1K",
    documented_fields=("prompt", "image_input", "aspect_ratio", "resolution", "output_format"),
    field_prompt="prompt",
    field_ratio="aspect_ratio",
    field_resolution="resolution",
    field_images="image_input",
    max_images=10,
    image_required=False,
    field_output_format="output_format",
    output_format="png",
    field_quality="",
    one_k_only=(),
    prompt_rules=(
        "Write a specific prose description: subject, purpose, and camera. "
        "Google's templates also name the aspect ratio in the prompt; the aspect_ratio field sets the frame. "
        "There is no negative_prompt field: describe the scene that should appear. "
        "A complex scene can be ordered in steps. "
        "Google documents character consistency for up to 4 characters and object fidelity for up to 10 objects. "
        "The Gemini API allows a mix of up to 14 reference images; KIE image_input allows 10, so do not attach more than 10. "
        "Accepted files are jpeg, png, or webp, each up to 30MB."
    ),
)

GPT_CARD = ImageModel(
    model_id="gpt-image-2-5-sunburst-text-to-image",
    kinds=("poster", "title", "logo", "ui", "text"),
    purpose="Images whose words must be readable.",
    sources=(
        "https://docs.kie.ai/market/gpt/gpt-image-2-5-sunburst-text-to-image",
        "https://developers.openai.com/api/docs/guides/image-prompting",
        "https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst",
    ),
    prompt_min=1,
    prompt_max=20000,
    ratios=SUNBURST_RATIO_VALUES,
    resolutions=RESOLUTION_VALUES,
    resolution_default="",
    documented_fields=("prompt", "aspect_ratio", "resolution", "background"),
    field_prompt="prompt",
    field_ratio="aspect_ratio",
    field_resolution="resolution",
    field_images="",
    max_images=0,
    image_required=False,
    field_output_format="",
    output_format="",
    field_quality="",
    one_k_only=SUNBURST_1K_ONLY_VALUES,
    prompt_rules=(
        "Name the subject, the use (poster, title, logo, or interface), the composition, and placement. "
        "Put wording that must be readable in quotes and state its position and typography. "
        "KIE takes aspect_ratio and resolution. OpenAI's own API takes size as WIDTHxHEIGHT and a quality setting; "
        "those are not fields on this KIE model, so do not invent them. "
        "The optional background field (transparent, opaque, or auto) is not sent. "
        "Do not ask for a transparent or checkerboard background."
    ),
)

SEEDREAM_CARD = ImageModel(
    model_id="seedream/5-pro-text-to-image",
    kinds=("product", "packshot"),
    purpose="Photoreal product or packshot with no cast reference.",
    sources=(
        "https://docs.kie.ai/market/seedream/5-pro-text-to-image",
        "https://seed.bytedance.com/en/seedream5_0_pro",
    ),
    prompt_min=4,
    prompt_max=5000,
    ratios=SEEDREAM_RATIO_VALUES,
    resolutions=(),
    resolution_default="",
    documented_fields=("prompt", "aspect_ratio", "quality", "output_format", "nsfw_checker"),
    field_prompt="prompt",
    field_ratio="aspect_ratio",
    field_resolution="",
    field_images="",
    max_images=0,
    image_required=False,
    field_output_format="output_format",
    output_format="png",
    field_quality="quality",
    one_k_only=(),
    prompt_rules=(
        "Write a descriptive prompt. ByteDance's product page shows photographic prompts and states an aspect ratio in the prompt; "
        "it does not publish this request schema. Use only the ratios listed here. "
        "Some ByteDance examples use 4:5, which this KIE model does not accept. "
        "Optional nsfw_checker is not sent. The documented default is false."
    ),
)

EDIT_CARD = ImageModel(
    model_id="gpt-image-2-5-sunburst-image-to-image",
    kinds=("edit",),
    purpose="An edit that takes input images.",
    sources=(
        "https://docs.kie.ai/market/gpt/gpt-image-2-5-sunburst-image-to-image",
        "https://developers.openai.com/api/docs/guides/image-prompting",
    ),
    prompt_min=0,
    prompt_max=20000,
    ratios=SUNBURST_RATIO_VALUES,
    resolutions=RESOLUTION_VALUES,
    resolution_default="",
    documented_fields=("prompt", "input_urls", "aspect_ratio", "resolution", "background"),
    field_prompt="prompt",
    field_ratio="aspect_ratio",
    field_resolution="resolution",
    field_images="input_urls",
    max_images=16,
    image_required=True,
    field_output_format="",
    output_format="",
    field_quality="",
    one_k_only=SUNBURST_1K_ONLY_VALUES,
    prompt_rules=(
        "Say what changes and list what must stay the same: identity, geometry, layout, lighting, or labels. "
        "Identify each image by its order and what to take from it. "
        "The ratios 27:16, 16:27, 9:8, and 8:9 support 1K only. "
        "The optional background field is not sent. Do not ask for a transparent or checkerboard background."
    ),
)

CARDS = (NANO_CARD, GPT_CARD, SEEDREAM_CARD, EDIT_CARD)
CARD_BY_ID = {card.model_id: card for card in CARDS}

NANO_BANANA_MODEL = NANO_CARD.model_id
GPT_IMAGE_MODEL = GPT_CARD.model_id
SEEDREAM_MODEL = SEEDREAM_CARD.model_id
EDIT_MODEL = EDIT_CARD.model_id
NANO_RATIOS = set(NANO_CARD.ratios)
NANO_MAX_REFS = NANO_CARD.max_images
SUNBURST_RATIOS = set(GPT_CARD.ratios)
SUNBURST_1K_ONLY = set(GPT_CARD.one_k_only)
SEEDREAM_RATIOS = set(SEEDREAM_CARD.ratios)


def card_for(model_id: str) -> ImageModel:
    """The card for a known model. An unknown id uses Nano Banana 2.1, matching the request fallback."""
    return CARD_BY_ID.get(model_id, NANO_CARD)


def ratio_allowed(model_id: str, aspect: str, resolution: str) -> bool:
    """True when this KIE model documents the ratio at that resolution. Unknown models are left alone."""
    card = CARD_BY_ID.get(model_id)
    if card is None:
        return True
    if aspect not in card.ratios:
        return False
    if resolution in {"2K", "4K"} and aspect in card.one_k_only:
        return False
    if resolution == "4K" and card.field_quality:
        return False
    return True


def prompt_length_error(model_id: str, prompt: str) -> str:
    """Empty when the prompt length is inside the documented min and max."""
    card = card_for(model_id)
    count = len(prompt)
    if card.prompt_min and count < card.prompt_min:
        return f"prompt is {count} characters; {card.model_id} requires at least {card.prompt_min}"
    if count > card.prompt_max:
        return f"prompt is {count} characters; {card.model_id} allows {card.prompt_max}"
    return ""


def art_director_card() -> str:
    """The constraint text shown to the art director. Built from this module, not from the network."""
    lines = [
        f"Official image model card. Checked {CHECKED}.",
        f"The stills step calls KIE {CREATE_TASK}.",
        "Follow this card for prompt form, aspect ratio, resolution, and reference images.",
        "Do not invent a field or a ratio that is not listed.",
        "The kind rules in your instructions still decide which jobs this pass emits.",
        "",
    ]
    for card in CARDS:
        lines.append(card.model_id)
        lines.append(f"Kinds: {', '.join(card.kinds)}. {card.purpose}")
        lines.append("Sources: " + " ".join(card.sources))
        lines.append("KIE input fields: " + ", ".join(card.documented_fields) + ".")
        if card.prompt_min:
            lines.append(f"Prompt length: {card.prompt_min} to {card.prompt_max} characters.")
        else:
            lines.append(f"Prompt length: at most {card.prompt_max} characters.")
        ratio_line = "aspect_ratio: " + ", ".join(card.ratios) + "."
        if "auto" in card.ratios and card.model_id != SEEDREAM_MODEL:
            ratio_line += " Default auto."
        lines.append(ratio_line)
        if card.resolutions:
            resolution_line = "resolution: " + ", ".join(card.resolutions) + "."
            if card.resolution_default:
                resolution_line += f" Default {card.resolution_default}."
            lines.append(resolution_line)
        if card.one_k_only:
            lines.append(
                "1K only: " + ", ".join(card.one_k_only) + ". 2K and 4K are not supported for those ratios."
            )
        if card.field_quality:
            lines.append("quality is required: basic outputs 1K, high outputs 2K. There is no 4K.")
        if card.field_images:
            requirement = "required" if card.image_required else "optional"
            lines.append(f"{card.field_images} is {requirement}, at most {card.max_images} image URLs.")
        else:
            lines.append("No reference-image field on this endpoint.")
        if card.output_format:
            lines.append(f"This request sends {card.field_output_format} {card.output_format}.")
        lines.append(card.prompt_rules)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
