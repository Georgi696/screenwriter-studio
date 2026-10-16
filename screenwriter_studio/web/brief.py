"""Write one short-film brief with the development chat model.

The desk calls this when someone asks for a story. It posts to the same KIE
Responses endpoint as the crew (`POST /codex/v1/responses`, development model).
KIE answers that POST with `text/event-stream` even when the request does not
ask to stream, so the body is parsed as server-sent events. It does not start
a production and it does not call an image model.

Selected reference images are described in text only: each one's role, name, direction
and the prompt it was generated from. No picture is sent; KIE's gateway answered requests
carrying image data with 502 errors.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

from screenwriter_studio.budget import CLIP_SECONDS, shot_budget
from screenwriter_studio.models import DEVELOPMENT_MODEL, kie_responses_client, load_kie_api_key

MISSING_KEY = (
    "API key is not set. Add KIE_API_KEY to `.env` in the repo root. "
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

# With references the pictures fix the places and look; an angle varies the story told in them.
ANGLES = (
    "a quiet, personal moment rather than action",
    "a rescue that goes wrong in a small way",
    "a reunion between two people who thought the other was lost",
    "something precious has gone missing and must be found",
    "a race against a clock the audience can see",
    "an act of mercy toward someone who does not deserve it",
    "a child sees what the adults miss",
    "a stranger who does not belong in this place",
    "a promise that has to be kept before the light changes",
    "a dark comedy: one stubborn person versus the situation",
    "the aftermath, years later, when someone returns",
    "a message that has to reach one person",
)
_LOOK_LABELS = {"medium", "light", "lighting", "palette", "colour", "color", "grade", "texture", "look", "style", "lens", "mood"}
_LABELED = re.compile(r"(?:^|(?<=[.;]\s))([A-Z][A-Za-z ]{1,20}):\s*")
MAX_AVOID = 3

_FENCE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*([\s\S]*?)\s*```")
_LABEL = re.compile(r"^(?:brief|story|logline)\s*:\s*", re.IGNORECASE)
_JSON_STRING = re.compile(
    r'"(?:brief|idea|text|paragraph|story)"\s*:\s*"((?:\\.|[^"\\])*)"',
)
_MAX_BRIEF = 900
MAX_REFERENCES = 8
_MAX_REFERENCE_PROMPT = 1500  # characters of one image's generation prompt
RETRY_WAITS = (2.0, 5.0)

ROLE_USE = {
    "character": "a character in the story: give them a first name, describe them by what is visible, keep their face and wardrobe",
    "location": "where the story happens: describe the place as shown",
    "object": "the key object or product the story turns on",
    "style": "the look and mood only: let it set the tone and genre, not the people or places",
    "composition": "a signature shot: one moment should be framed like this",
    "lock": "people and places to keep exactly as shown",
}


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
        "This is a short, not a feature. These are default generation limits; a production can choose a different shot limit."
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


def angle_for(salt: str) -> str:
    digest = hashlib.sha256(("angle:" + salt).encode("utf-8")).digest()
    return ANGLES[int.from_bytes(digest[:4], "big") % len(ANGLES)]


def look_only(prompt: str) -> str:
    """The light, palette and medium parts of a structured image prompt; its people and places stay out."""
    marks = list(_LABELED.finditer(prompt or ""))
    kept = []
    for at, mark in enumerate(marks):
        end = marks[at + 1].start() if at + 1 < len(marks) else len(prompt)
        if mark.group(1).strip().lower() in _LOOK_LABELS:
            kept.append(prompt[mark.start():end].strip())
    return " ".join(kept)


def avoid_line(avoid: list[str] | None) -> str:
    """Earlier suggestions from this desk, so a new click does not hand back the same film."""
    earlier = [" ".join(str(text).split())[:600] for text in (avoid or [])[:MAX_AVOID] if str(text).strip()]
    if not earlier:
        return ""
    return ("\n\nThese briefs were already suggested. Write a different film: do not reuse their characters, names, "
            "premise, obstacle or visual hook, even if the references are the same.\n"
            + "\n".join(f"- {text}" for text in earlier))


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


def film_line(film: dict | None) -> str:
    """What the user already locked in the Film section; the brief must not contradict it."""
    film = film or {}
    parts = []
    if film.get("runtime_seconds"):
        parts.append(f"the film is exactly {int(film['runtime_seconds'])} seconds long")
    if film.get("aspect_ratio"):
        parts.append(f"the aspect ratio is {film['aspect_ratio']}")
    if film.get("max_shots"):
        parts.append(f"it has at most {int(film['max_shots'])} shots")
    if not parts:
        return ""
    return "\n\nThe user has already locked: " + "; ".join(parts) + ". State these exactly in the brief and do not choose different values."


def reference_line(references: list[dict]) -> str:
    """How to use the selected references. Their descriptions follow, one per reference."""
    return (
        "\n\nThe user selected reference images, described below with a role each and, where saved, the prompt "
        "each image was generated from. Build the brief around them: every Character reference is in the cast, "
        "the story happens in the Location references, an Object reference is the thing the story turns on, "
        "Style sets tone and genre, Composition is one moment's framing. "
        "Treat a generation prompt as the exact description of its image: reuse its people, wardrobe, places, "
        "props and light, not its camera directions or technical wording. "
        "Use the names given; name an unnamed character yourself."
        + ("" if any(item["mode"] in {"character", "lock"} for item in references)
           else " No character is selected; invent one. The places do not decide who it is: a resident, a child, "
                "a medic, a musician, a thief or a stranger can be here as well as the most obvious person. "
                "Do not default to whoever the setting suggests first.")
    )


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def image_prompt(path: Path, _depth: int = 0) -> str:
    """The prompt a library image was generated from, read from its production's saved plan.

    An edited take uses its edited prompt; a repair uses the prompt of the image it repaired.
    A reference copied into another production is followed back to its source once.
    Uploaded pictures have no prompt, so this returns an empty string for them.
    """
    path = Path(path)
    if path.parent.name == "references" and _depth < 2:
        settings = _read(path.parent.parent / "run_settings.json") or {}
        for item in settings.get("references") or []:
            if isinstance(item, dict) and Path(str(item.get("path") or "")).name == path.name and item.get("source"):
                source = (path.parent.parent.parent / str(item["source"])).resolve()
                if source.is_relative_to(path.parent.parent.parent.resolve()):
                    return image_prompt(source, _depth + 1)
        return ""
    if path.parent.name != "images":
        return ""
    jobs = [job for job in ((_read(path.parent / "jobs.json") or {}).get("jobs") or []) if isinstance(job, dict)]
    by_id = {job.get("id"): job for job in jobs}
    records = (_read(path.parent / "manifest.json") or {}).get("images") or []
    for record in records:
        if not isinstance(record, dict):
            continue
        for entry in [record, *(record.get("history") or [])]:
            if isinstance(entry, dict) and Path(str(entry.get("path") or "")).name == path.name:
                override = entry.get("job_override") if isinstance(entry.get("job_override"), dict) else {}
                prompt = override.get("prompt") or (by_id.get(record.get("id")) or {}).get("prompt")
                if prompt:
                    return str(prompt)
    stem = re.sub(r"\.(?:repair|take)-\d+$", "", path.stem)
    for job in jobs:
        if job.get("id") == stem or job.get("filename") == path.name:
            return str(job.get("prompt") or "")
    return ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def reference_text(references: list[dict]) -> str:
    """One paragraph per reference: its role, name, direction and the prompt its image was made from."""
    from screenwriter_studio.references import reference_direction

    lines = []
    for number, item in enumerate(references[:MAX_REFERENCES], start=1):
        path = item["source_path"]
        name = f" \u201c{item['name']}\u201d" if item.get("name") and item["name"] != path.name else ""
        label = (f"Reference {number}{name} ({path.name}) · {item['mode']}: use it as "
                 f"{ROLE_USE.get(item['mode'], ROLE_USE['lock'])}. " + reference_direction(item))
        prompt = image_prompt(path)
        if prompt and item["mode"] == "style":
            # A style picture lends its look. Its subject (a city, a war, a person) must not choose the story.
            look = look_only(prompt)
            label += (f"\nThe look of this image, from its generation prompt: \u201c{_clip(look, _MAX_REFERENCE_PROMPT)}\u201d" if look
                      else "\nIts generation prompt is left out so its people and places do not enter the story.")
        else:
            label += (f"\nThe prompt this image was generated from: \u201c{_clip(prompt, _MAX_REFERENCE_PROMPT)}\u201d" if prompt
                      else "\nNo generation prompt is saved for this image (an upload); go by its role, name and file name.")
        lines.append(label)
    return "\n\n".join(lines)


def resolve_references(references) -> list[dict]:
    """Checked library images for the brief, or a clear error. Paths stay inside productions/."""
    from screenwriter_studio.references import reference_choices
    from screenwriter_studio.studio import _plan_references

    try:
        return _plan_references(reference_choices(references))
    except ValueError as exc:
        raise BriefSuggestError(str(exc)) from exc


def build_request(idea: str, *, salt: str | None = None, film: dict | None = None,
                  references: list[dict] | None = None, avoid: list[str] | None = None) -> dict:
    """Body fields for `responses.create` on the KIE chat client. References must already be resolved.

    The input is always plain text; references are described, never attached as pictures.
    """
    from screenwriter_studio import settings as studio_settings
    salt = salt or secrets.token_hex(4)
    text = build_input(idea, salt=salt) + film_line(film)
    if references:
        if not " ".join(idea.split()):
            # The references lead; the random spark would only pull the story somewhere else.
            text = text.replace(f"Let this spark steer the invention so two clicks do not match: {spark_for(salt)}. ",
                                f"Tell this kind of story in the referenced places: {angle_for(salt)}. ")
        text += reference_line(references) + "\n\n" + reference_text(references)
    if not " ".join(idea.split()):
        text += avoid_line(avoid)
    return {
        "model": studio_settings.load()["chat"]["development"] or DEVELOPMENT_MODEL,
        "instructions": instructions(),
        "input": text,
        # Reasoning tokens count against this cap. A medium effort pass can
        # spend the whole budget before any visible paragraph exists.
        "max_output_tokens": 4096,
        "reasoning": {"effort": "low"},
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


def _error_message(payload: object) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    if isinstance(error, dict):
        for field in ("message", "code"):
            value = error.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return ""


def _failure_reason(payload: dict) -> str:
    """Why a payload has no assistant text. Empty when the payload is not a failure."""
    incomplete = payload.get("incomplete_details")
    if isinstance(incomplete, dict) and incomplete.get("reason") == "max_output_tokens":
        return "The model ran out of room before it wrote the brief."
    message = _error_message(payload)
    if message:
        return message
    if payload.get("status") in {"failed", "incomplete"}:
        return "The model did not answer."
    return ""


def _sse_payloads(raw: str):
    """JSON objects from one event-stream body. Comments and blank lines are ignored."""
    data_lines: list[str] = []

    def flush():
        nonlocal data_lines
        blob = "\n".join(data_lines).strip()
        data_lines = []
        if not blob:
            return None
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return None

    for line in raw.splitlines():
        if line == "":
            payload = flush()
            if payload is not None:
                yield payload
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    payload = flush()
    if payload is not None:
        yield payload


def _looks_like_sse(raw: str) -> bool:
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith(("event:", "data:", ":"))
    return False


def text_from_sse(raw: str) -> tuple[str, str]:
    """Assistant prose and a failure reason from a KIE `text/event-stream` body.

    The visible brief is `response.output_text.delta` or the completed message.
    Reasoning items, encrypted reasoning, and `: keep-alive` comments are not it.
    """
    deltas: list[str] = []
    done = ""
    completed = ""
    failure = ""
    for payload in _sse_payloads(raw):
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "response.output_text.delta" and isinstance(payload.get("delta"), str):
            deltas.append(payload["delta"])
            continue
        if kind == "response.output_text.done" and isinstance(payload.get("text"), str):
            done = payload["text"]
            continue
        if kind == "response.output_item.done":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "message":
                piece = text_from_response({"output": [item]})
                if piece.strip():
                    completed = piece
            continue
        response = payload.get("response")
        if kind in {"response.completed", "response.incomplete", "response.failed"} and isinstance(response, dict):
            piece = text_from_response(response)
            if piece.strip():
                completed = piece
            reason = _failure_reason(response)
            if reason:
                failure = reason
            continue
        if kind in {"error", "response.error"}:
            reason = _error_message(payload)
            if reason:
                failure = reason
    text = "".join(deltas).strip() or done.strip() or completed.strip()
    return text, "" if text else failure


def text_from_body(raw: str) -> tuple[str, str]:
    """Assistant text and a failure reason from JSON, an event stream, or plain prose."""
    text = (raw or "").strip()
    if not text:
        return "", ""
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            extracted = text_from_response(data)
            nested = data.get("response")
            if not extracted and isinstance(nested, dict):
                extracted = text_from_response(nested)
            if extracted.strip():
                return extracted, ""
            reason = _failure_reason(data)
            if not reason and isinstance(nested, dict):
                reason = _failure_reason(nested)
            return "", reason
        if isinstance(data, str) and data.strip():
            return data.strip(), ""
    if _looks_like_sse(text):
        return text_from_sse(text)
    lowered = text[:80].lower()
    if lowered.startswith("<!doctype") or lowered.startswith("<html"):
        return "", "The model did not answer."
    return text, ""


def brief_from_body(raw: str) -> str:
    """Plain prose from a KIE body. A failure raises instead of returning that body."""
    text, failure = text_from_body(raw)
    brief = parse_brief_text(text)
    if brief:
        return brief
    if failure:
        raise BriefSuggestError(failure)
    raise BriefSuggestError("The model returned an empty brief.")


def _safe_error(exc: Exception, key: str) -> str:
    from screenwriter_studio.errors import provider_down, provider_message
    if provider_down(exc):
        return provider_message(exc)
    message = getattr(exc, "message", None) or str(exc)
    if key:
        message = message.replace(key, "[key]")
    message = " ".join(message.split())
    if len(message) > 240:
        message = message[:240].rstrip() + "…"
    return message or "The model did not answer."


def _text_from_client_response(response, *, key: str) -> str:
    """Text from an SDK object, a dict, or the raw event-stream string KIE returns."""
    output_text = getattr(response, "output_text", None)
    if not isinstance(response, str) and isinstance(output_text, str) and output_text.strip():
        return output_text
    if isinstance(response, str):
        text, failure = text_from_body(response)
    else:
        payload = response if isinstance(response, dict) else None
        if payload is None and hasattr(response, "model_dump"):
            payload = response.model_dump()
        if not isinstance(payload, dict):
            return ""
        text = text_from_response(payload)
        failure = "" if text.strip() else _failure_reason(payload)
    if failure and not str(text).strip():
        raise BriefSuggestError(_safe_error(RuntimeError(failure), key))
    return text


def _create_with_retries(chat, request: dict, key: str):
    """A gateway error, overload or dropped connection is usually brief on KIE's side: try twice more."""
    from screenwriter_studio.errors import provider_down
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            return chat.responses.create(**request)
        except Exception as exc:
            transient = provider_down(exc) or type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}
            if not transient or attempt == len(RETRY_WAITS):
                raise BriefSuggestError(_safe_error(exc, key)) from exc
            time.sleep(RETRY_WAITS[attempt])


def suggest_brief(idea: str, *, client=None, salt: str | None = None, film: dict | None = None,
                  references: list | None = None, avoid: list | None = None) -> str:
    """Return one paragraph. Raises BriefSuggestError when the key or the model fails."""
    key = load_kie_api_key()
    if not key:
        raise BriefSuggestError(MISSING_KEY)
    resolved = resolve_references(references) if references else None
    request = build_request(idea if isinstance(idea, str) else "", salt=salt, film=film, references=resolved,
                            avoid=[text for text in avoid or [] if isinstance(text, str)])
    chat = client or kie_responses_client(api_key=key)
    response = _create_with_retries(chat, request, key)
    try:
        text = _text_from_client_response(response, key=key)
    except BriefSuggestError as exc:
        raise BriefSuggestError(_safe_error(exc, key)) from exc
    brief = parse_brief_text(text)
    if not brief:
        raise BriefSuggestError("The model returned an empty brief.")
    return brief
