"""Structured workflow updates, independent of the displayed activity text."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StudioEvent:
    kind: Literal[
        "development", "developed", "writing", "reviewing", "approved",
        "rejected", "rewriting", "greenlit", "pages", "art", "stills", "image",
        "complete", "failed",
    ]
    text: str
    folder: str = ""
    image: dict | None = None
