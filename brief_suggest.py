"""Write one short-film brief with the development chat model.

The desk calls this when someone asks for a story. It posts to the same KIE
Responses endpoint as the crew (`POST /codex/v1/responses`, development model).
It does not start a production and it does not call an image model.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets

from budget import CLIP_SECONDS, shot_budget
from models import DEVELOPMENT_MODEL, kie_responses_client, load_kie_api_key

MISSING_KEY = (
    "KIE_API_KEY is not set. Add it to `.env` in the repo root. "
    "Keys are created at https://kie.ai/api-key"
)

# Steering lines for an empty box. A salt picks one so two clicks do not match.
SPARKS = (
    "a night-shift locksmith who wants one door to stay shut",
    "a ferry cook who wants the last meal to reach the captain",
    "a child who wants a red kite back from a train roof",
    "a wedding pianist who wants the blackout to last one more song",
    "a beekeeper who wants the swarm to choose the empty hive",
    "a subway cleaner who wants to return a violin before dawn",
    "a lighthouse keeper who wants the foghorn to stay silent",
    "a tailor who wants a torn coat finished before the rain",
    "a goalkeeper who wants the penalty saved without a crowd",
    "a florist who wants the wrong bouquet delivered on purpose",
    "a projectionist who wants the last reel to play through",
    "a librarian who wants a banned book left on the right desk",
    "a diver who wants a ring back from a dark pool",
    "a radio host who wants one caller to stay on the line",
    "a window washer who wants to warn the office before the crack spreads",
    "a pastry chef who wants the cake to reach the hospital intact",
)

_FENCE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*([\s\S]*?)\s*```")
_LABEL = re.compile(r"^(?:brief|story|logline)\s*:\s*", re.IGNORECASE)
_JSON_STRING = re.compile(
    r'"(?:brief|idea|text|paragraph|story)"\s*:\s*"((?:\\.|[^"\\])*)"',
)
_MAX_BRIEF = 900


class BriefSuggestError(Exception):
    """The desk can show this. It never includes the API key."""


def budget_line() -> str:
    """The studio cap: one shot per 8 seconds, so 60s is 7 shots and 30s is 3."""
    return (
        f"The crew shoots one shot per {CLIP_SECONDS} seconds. "
        f"A 60-second film is at most {shot_budget(60)} shots. "
        f"A 30-second film is at most {shot_budget(30)} shots. "
        f"A 15-second film is at most {shot_budget(15)} shots. "
        "Name a runtime of 15, 30, or 60 seconds and a story the crew can shoot inside that budget. "
        "This is a short, not a feature."
    )


def instructions() -> str:
    return (
        "You fill the brief box on a short-film desk for someone who is not a screenwriter. "
        "Return one paragraph of plain prose, under 90 words. "
        "No title, no markdown, no code fence, no JSON, no bullet list, no preamble.\n"
        + budget_line()
        + " One named character with a want. One obstacle. One visual hook the camera can see. "
        "Aspect ratio is 16:9, 9:16, or 1:1. Genre in a few words."
    )


def spark_for(salt: str) -> str:
    digest = hashlib.sha256(salt.encode("utf-8")).digest()
    return SPARKS[int.from_bytes(digest[:4], "big") % len(SPARKS)]


def build_input(idea: str, *, salt: str) -> str:
    note = " ".join(idea.split())
    if note:
        return (
            budget_line()
            + "\n\nRewrite the note below into one stronger brief. Keep this idea. "
            "Do not replace it with a different story. "
            "Keep their character, place, object, and want. "
            "Add a runtime, an aspect ratio, a genre, one character with a want, one obstacle, "
            "and a visual hook only where the note left them out.\n\n"
            f"Note:\n{note}"
        )
    return (
        budget_line()
        + "\n\nThe box is empty. Invent one complete, specific short-film brief. "
        "Include the runtime, the aspect ratio, the genre, one named character and what they want, "
        "one obstacle, and a visual hook. "
        f"Let this spark steer the invention so two clicks do not match: {spark_for(salt)}. "
        f"Variation {salt}."
    )


def build_request(idea: str, *, salt: str | None = None) -> dict:
    """Body fields for `responses.create` on the KIE chat client."""
    return {
        "model": DEVELOPMENT_MODEL,
        "instructions": instructions(),
        "input": build_input(idea, salt=salt or secrets.token_hex(4)),
        "max_output_tokens": 1200,
        "store": False,
    }


def _unwrap_fences(text: str) -> str:
    current = text.strip()
    for _ in range(3):
        match = re.fullmatch(r"```(?:[a-zA-Z0-9_-]+)?\s*([\s\S]*?)\s*```", current)
        if not match:
            break
        current = match.group(1).strip()
    return current


def _from_json(text: str) -> str | None:
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        for key in ("brief", "idea", "text", "paragraph", "story"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        strings = [value.strip() for value in data.values() if isinstance(value, str) and value.strip()]
        if strings:
            return " ".join(strings)
    match = _JSON_STRING.search(text)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"').strip()
    except json.JSONDecodeError:
        return match.group(1).replace("\\n", " ").strip()


def parse_brief_text(raw: str) -> str:
    """Plain prose for the textarea. Drops fences, JSON wrappers, and labels."""
    text = _unwrap_fences((raw or "").strip())
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    extracted = _from_json(text)
    if extracted:
        text = extracted
    text = _LABEL.sub("", text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if len(text) > _MAX_BRIEF:
        cut = text.rfind(".", 0, _MAX_BRIEF)
        text = text[: cut + 1] if cut > 200 else text[:_MAX_BRIEF].rstrip()
    return text


def text_from_response(payload: dict) -> str:
    """Pull assistant text out of a Responses payload, skipping reasoning items."""
    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {None, "message"}:
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    if block.get("type") in {None, "output_text", "text"}:
                        parts.append(block["text"])
    return "\n".join(parts)


def brief_from_response(payload: dict) -> str:
    return parse_brief_text(text_from_response(payload))


def _safe_error(exc: Exception, key: str) -> str:
    message = getattr(exc, "message", None) or str(exc)
    if key:
        message = message.replace(key, "[key]")
    message = " ".join(message.split())
    if len(message) > 240:
        message = message[:240].rstrip() + "…"
    return message or "The model did not answer."


def suggest_brief(idea: str, *, client=None, salt: str | None = None) -> str:
    """Return one paragraph. Raises BriefSuggestError when the key or the model fails."""
    key = load_kie_api_key()
    if not key:
        raise BriefSuggestError(MISSING_KEY)
    request = build_request(idea if isinstance(idea, str) else "", salt=salt)
    chat = client or kie_responses_client(api_key=key)
    try:
        response = chat.responses.create(**request)
    except Exception as exc:
        raise BriefSuggestError(_safe_error(exc, key)) from exc
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        text = output_text
    elif isinstance(response, dict):
        text = text_from_response(response)
    elif hasattr(response, "model_dump"):
        text = text_from_response(response.model_dump())
    else:
        text = ""
    brief = parse_brief_text(text)
    if not brief:
        raise BriefSuggestError("The model returned an empty brief.")
    return brief
