"""Checked provider contracts and an explicit, offline model-selection policy.

Capabilities come from the official sources below. Routing preferences are studio
heuristics, not vendor benchmarks. Do not silently relax quality, timing or language.
"""

from dataclasses import dataclass
import re

CHECKED = "2026-10-14"
KLING = "kling-3.0/video"
SEEDANCE = "bytedance/seedance-2-5"
SELECTIONS = ("auto", "kling", "seedance")
KLING_API = "https://docs.kie.ai/market/kling/kling-3-0"
KLING_GUIDE = "https://kling.ai/quickstart/klingai-video-3-model-user-guide"
SEEDANCE_API = "https://docs.kie.ai/market/bytedance/seedance-2-5"
SEEDANCE_GUIDE = "https://docs.volcengine.com/docs/ark/seedance-2-5-prompt-guide?lang=en"
SEEDANCE_LANGUAGES = "https://docs.volcengine.com/docs/ark/seedance-2-5?lang=en"


@dataclass(frozen=True)
class VideoModel:
    id: str
    name: str
    duration_min: int
    duration_max: int
    modes: tuple[str, ...]
    ratios: tuple[str, ...]
    prompt_max: int
    languages: frozenset[str]
    sources: tuple[str, ...]


CARDS = {
    KLING: VideoModel(KLING, "Kling 3.0", 3, 15, ("std", "pro", "4K"),
                      ("16:9", "9:16", "1:1"), 2500,
                      frozenset({"en", "zh", "ja", "ko", "es"}), (KLING_API, KLING_GUIDE)),
    SEEDANCE: VideoModel(SEEDANCE, "Seedance 2.5", 4, 30, ("std", "pro"),
                         ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"), 20480,
                         frozenset({"en", "zh", "ja", "ko", "es", "id", "ms", "th", "ar", "pt", "vi"}),
                         (SEEDANCE_API, SEEDANCE_GUIDE, SEEDANCE_LANGUAGES)),
}

_LANGUAGE_NAMES = {
    "en": ("english", "英语"), "zh": ("chinese", "mandarin", "cantonese", "中文", "普通话"),
    "ja": ("japanese", "日本語"), "ko": ("korean", "한국어"), "es": ("spanish", "español"),
    "id": ("indonesian", "bahasa indonesia"), "ms": ("malay", "bahasa melayu"),
    "th": ("thai", "ภาษาไทย"), "ar": ("arabic", "العربية"), "pt": ("portuguese", "português"),
    "vi": ("vietnamese", "tiếng việt"), "fr": ("french", "français"),
    "de": ("german", "deutsch"), "ru": ("russian", "русский"), "bg": ("bulgarian", "български"),
    "hi": ("hindi",), "it": ("italian",), "pl": ("polish",),
}


def clip_bounds(selection: str = "auto") -> tuple[int, int]:
    """Shortest and longest clip this video choice can generate.

    Auto may use either model, so a clip can be as short as Kling and as long as Seedance.
    """
    if selection == "kling":
        card = CARDS[KLING]
        return card.duration_min, card.duration_max
    if selection == "seedance":
        card = CARDS[SEEDANCE]
        return card.duration_min, card.duration_max
    if selection == "auto":
        return (min(card.duration_min for card in CARDS.values()),
                max(card.duration_max for card in CARDS.values()))
    raise ValueError("Choose auto, kling, or seedance model selection.")


def language_codes(language: str) -> set[str]:
    text = language.lower().strip()
    matches = {code for code, names in _LANGUAGE_NAMES.items()
               if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text)
                      for name in (code, *names))}
    return matches or {text}


def has_speech(shot) -> bool:
    text = f"{shot.audio} {shot.action} {shot.notes}".lower()
    text = re.sub(r"\b(?:no|without)\s+(?:spoken\s+)?(?:dialogue|speech|narration|voiceover|voices)\b", "", text)
    return bool(re.search(r"\b(?:says?|speaks?|speaking|whispers?|shouts?|dialogue|narrat\w*|voice[ -]?over|lip[ -]?sync|sings?|singing)\b", text)
                or re.search(r'["“「{]', shot.audio))


def capability_error(card: VideoModel, *, duration: int, mode: str, aspect: str,
                     languages: set[str] | None = None) -> str:
    if not card.duration_min <= duration <= card.duration_max:
        return f"requires {card.duration_min}–{card.duration_max} seconds per shot"
    if mode not in card.modes:
        return f"does not offer {mode} output on this KIE endpoint"
    if aspect not in card.ratios:
        return f"does not support {aspect}"
    if languages and not languages <= card.languages:
        return "does not document native speech in " + ", ".join(sorted(languages - card.languages))
    return ""


def choose_model(development, shot, *, mode: str, sound: bool, selection: str = "auto") -> tuple[VideoModel, str]:
    if selection not in SELECTIONS:
        raise ValueError("Choose auto, kling, or seedance model selection.")
    speech = sound and has_speech(shot)
    languages = language_codes(development.language) if speech else None
    errors = {key: capability_error(card, duration=shot.duration_seconds, mode=mode,
                                   aspect=development.aspect_ratio, languages=languages)
              for key, card in CARDS.items()}
    if selection != "auto":
        chosen = {"kling": KLING, "seedance": SEEDANCE}[selection]
        if errors[chosen]:
            raise ValueError(f"s{shot.number:02d}: {CARDS[chosen].name} {errors[chosen]}. Change the model or production settings.")
        return CARDS[chosen], "Selected explicitly; checked against the shot's timing, quality, format, and speech requirements."
    eligible = [key for key, error in errors.items() if not error]
    if not eligible:
        raise ValueError(f"s{shot.number:02d}: no documented model matches this shot. " +
                         "; ".join(f"{CARDS[key].name} {error}" for key, error in errors.items()) +
                         ". Adjust quality, duration, or disable generated speech and dub separately.")
    if len(eligible) == 1:
        chosen = eligible[0]
        other = next(key for key in CARDS if key != chosen)
        return CARDS[chosen], f"Matches the locked requirements; {CARDS[other].name} {errors[other]}."
    if speech:
        return CARDS[KLING], "Dialogue shot in a documented Kling language; prefer its speaker attribution and native lip sync."
    # Preference thresholds below are editorial choices, not provider limits.
    if shot.duration_seconds >= 10:
        return CARDS[SEEDANCE], "Extended continuous action; prefer Seedance's timeline prompting and longer-form motion continuity."
    visible_cast = [c.name for c in development.characters
                    if re.search(r"(?<!\w)" + re.escape(c.name) + r"(?!\w)", shot.action, re.I)]
    if len(visible_cast) >= 2:
        return CARDS[SEEDANCE], "Several named subjects share the action; prefer Seedance's multi-subject staging."
    moves = re.findall(r"\b(?:pan|tilt|orbit|track|dolly|zoom|rack.focus)\w*\b", shot.framing, re.I)
    if len(set(move.lower() for move in moves)) >= 2:
        return CARDS[SEEDANCE], "Compound camera movement; prefer Seedance's structured motion and camera instructions."
    return CARDS[KLING], "Short continuous shot from one approved keyframe; prefer Kling's direct image-to-video camera and action control."
