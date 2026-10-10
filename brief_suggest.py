"""Write one short-film brief with the development chat model.

The desk calls this when someone asks for a story. It posts to the same KIE
Responses endpoint as the crew (`POST /codex/v1/responses`, development model).
KIE answers that POST with `text/event-stream` even when the request does not
ask to stream, so the body is parsed as server-sent events. It does not start
a production and it does not call an image model.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets

from budget import CLIP_SECONDS, shot_budget
from models import DEVELOPMENT_MODEL, kie_responses_client, load_kie_api_key

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
    try:
        text = _text_from_client_response(response, key=key)
    except BriefSuggestError as exc:
        raise BriefSuggestError(_safe_error(exc, key)) from exc
    brief = parse_brief_text(text)
    if not brief:
        raise BriefSuggestError("The model returned an empty brief.")
    return brief
