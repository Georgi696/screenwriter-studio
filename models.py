"""One KIE.ai key, one model per job.

Chat agents use KIE's OpenAI Responses endpoint
POST https://api.kie.ai/codex/v1/responses
with Authorization: Bearer $KIE_API_KEY.
The body model field selects the model. Gemini chat models on KIE are
chat-only and do not return this SDK's json_schema, so the crew uses
Responses models that document text.format json_schema.

Image models are chosen per still by kind. The stills generator reads the same key.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents import OpenAIResponsesModel
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI

from image_model_card import EDIT_MODEL, GPT_IMAGE_MODEL, NANO_BANANA_MODEL, SEEDREAM_MODEL

KIE_API_ROOT = "https://api.kie.ai"
KIE_RESPONSES_BASE = f"{KIE_API_ROOT}/codex/v1"

# Chat. docs.kie.ai/market/chat. Both accept text.format json_schema.
# gpt-6-astra is the premium Responses model (reasoning through max, 272K).
# gpt-6-1-sol is the current agent model on the same endpoint, for the checklist.
DEVELOPMENT_MODEL = "gpt-6-astra"
WRITER_MODEL = "gpt-6-astra"
ART_MODEL = "gpt-6-astra"
EDITOR_MODEL = "gpt-6-1-sol"

# Stills. Kind -> model. Ratios, reference limits, and request fields are in
# image_model_card.py, checked against the KIE and vendor docs on 2026-10-09.
IMAGE_MODEL_BY_KIND = {
    "character": NANO_BANANA_MODEL,
    "location": NANO_BANANA_MODEL,
    "keyframe": NANO_BANANA_MODEL,
    "scene": NANO_BANANA_MODEL,
    "poster": GPT_IMAGE_MODEL,
    "title": GPT_IMAGE_MODEL,
    "logo": GPT_IMAGE_MODEL,
    "ui": GPT_IMAGE_MODEL,
    "text": GPT_IMAGE_MODEL,
    "product": SEEDREAM_MODEL,
    "packshot": SEEDREAM_MODEL,
    "edit": EDIT_MODEL,
}


def load_kie_api_key() -> str:
    """Read KIE_API_KEY from the environment, then from a .env file. Never prints it."""
    load_dotenv(override=False)
    key = os.environ.get("KIE_API_KEY", "").strip()
    if key:
        return key

    starts = [Path.cwd(), Path(__file__).resolve().parent]
    seen: set[Path] = set()
    for start in starts:
        for directory in [start, *start.parents]:
            if directory in seen:
                continue
            seen.add(directory)
            for candidate in (
                directory / ".env",
                directory / "screenwriter_studio" / ".env",
                directory / "biology_brand" / ".env",
            ):
                found = _key_in_env_file(candidate)
                if found:
                    os.environ["KIE_API_KEY"] = found
                    return found
            if (directory / ".git").is_dir():
                break
    return ""


def _key_in_env_file(path: Path) -> str:
    if not path.is_file():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "KIE_API_KEY":
            return value.strip().strip('"').strip("'")
    return ""


def kie_chat_model(model_id: str) -> OpenAIResponsesModel:
    """A Responses model on KIE. One base URL; the model id goes in the request body."""
    client = AsyncOpenAI(
        api_key=load_kie_api_key() or "missing",
        base_url=KIE_RESPONSES_BASE,
    )
    return OpenAIResponsesModel(model=model_id, openai_client=client)


def kie_responses_client(*, api_key: str | None = None, http_client=None) -> OpenAI:
    """Sync client for the same Responses endpoint the crew uses.

    `kie_chat_model` wraps an async client for the Agents SDK. A one-shot brief
    on the desk uses this sync client: same key, same base URL, model id in the body.
    """
    key = load_kie_api_key() if api_key is None else api_key
    return OpenAI(
        api_key=key or "missing",
        base_url=KIE_RESPONSES_BASE,
        http_client=http_client,
        max_retries=0,
        timeout=60.0,
    )


development_chat = kie_chat_model(DEVELOPMENT_MODEL)
writer_chat = kie_chat_model(WRITER_MODEL)
art_chat = kie_chat_model(ART_MODEL)
editor_chat = kie_chat_model(EDITOR_MODEL)
