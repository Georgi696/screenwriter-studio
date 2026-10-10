"""Pydantic handoffs for the screenwriter studio.

Model ids live in models.py. Every agent uses KIE_API_KEY.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from agents import AgentOutputSchema
from pydantic import BaseModel, Field

StillKind = Literal[
    "character",
    "location",
    "keyframe",
    "scene",
    "poster",
    "title",
    "logo",
    "ui",
    "text",
    "product",
    "packshot",
    "edit",
]


class CharacterLock(BaseModel):
    name: str = Field(description="First name only, the name used in the script")
    description: str = Field(
        description="Physical lock pasted verbatim into every later prompt: age, skin, hair, clothes, one identifying detail"
    )


class Beat(BaseModel):
    name: str = Field(description="Spine beat: Hook, Setup, Turn, Escalation, Crisis, Button, or CTA")
    timecode: str = Field(description="Start time, e.g. 0:00")
    action: str = Field(description="What the camera sees in this beat. One continuous action")


class Development(BaseModel):
    title: str
    slug: str = Field(description="Lowercase hyphenated folder name, no spaces")
    job_type: str = Field(description="narrative, sketch, ad, vertical, explainer, or trailer")
    runtime_seconds: int = Field(ge=6, le=720)
    max_shots: int | None = Field(default=None, ge=1, le=240)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = Field(description="Production aspect ratio")
    language: str
    tone: str
    audience: str
    logline: str = Field(description="One sentence: when X, a flawed character must Y before Z")
    beats: list[Beat] = Field(min_length=1, max_length=120)
    look: str
    palette: str
    lighting: str
    characters: list[CharacterLock]
    locations: list[str]
    invented: list[str] = Field(description="Defaults you chose because the brief did not say. Two items at most")


class Shot(BaseModel):
    number: int = Field(ge=1)
    duration_seconds: int = Field(ge=3, le=15)
    framing: str
    action: str = Field(description="One continuous action. No cuts inside the shot")
    audio: str
    notes: str = Field(description="Continuity, lip-sync, or prop note. Empty string if none")


class Screenplay(BaseModel):
    fountain: str = Field(description="Complete Fountain screenplay, including title page lines")
    shots: list[Shot] = Field(min_length=1, max_length=240)
    notes: list[str] = Field(description="At most two notes: an invented choice, or one alternative")


class ScriptVerdict(BaseModel):
    passed: bool
    issues: list[str] = Field(
        description="Concrete fixes. Empty when passed. Each issue names the line or beat and the change"
    )


class StillJob(BaseModel):
    id: str = Field(description="Short lowercase id, used as the filename")
    kind: StillKind
    prompt: str
    references: list[str] = Field(
        default_factory=list,
        description="Ids of earlier stills this image must match, or empty",
    )


class StillPackage(BaseModel):
    aspect_ratio: str
    resolution: str = Field(description="1K unless the brief asked for finals")
    jobs: list[StillJob]


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy nested ``$defs`` into place and drop the references.

    KIE rejects ``$ref`` inside array items, including ``#/$defs/Beat``,
    even when that definition sits at the root of the schema.
    """
    schema = copy.deepcopy(schema)
    pool: dict[str, Any] = {}
    for key in ("definitions", "$defs"):
        found = schema.get(key)
        if isinstance(found, dict):
            pool.update(found)

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            parts = ref[2:].split("/")
            if len(parts) == 2 and parts[0] in {"$defs", "definitions"} and parts[1] in pool:
                merged = walk(copy.deepcopy(pool[parts[1]]))
                extras = {key: walk(value) for key, value in node.items() if key != "$ref"}
                merged.update(extras)
                return merged
        return {
            key: walk(value)
            for key, value in node.items()
            if key not in {"$defs", "definitions"}
        }

    return walk(schema)


class KieOutput(AgentOutputSchema):
    """Strict output schema with nested models inlined for KIE."""

    def __init__(self, output_type: type[Any]) -> None:
        super().__init__(output_type)
        self._output_schema = _inline_refs(self._output_schema)
