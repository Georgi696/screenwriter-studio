"""Original templates derived from official guides, not copied sample prompts.

Kling's guide uses cinematic prose and attributed dialogue; it does not require
seven uppercase fields or a fixed word budget. Seedance recommends a summary,
shot order/timeline, material roles when applicable, and continuity instructions.
"""

import re

from screenwriter_studio.videos.catalog import KLING, SEEDANCE, has_speech


def _speaker(development, name: str) -> str:
    """A name and a visible cue, so the model can tell which person in the frame is talking."""
    for character in development.characters:
        if character.name.strip().lower() == name.strip().lower():
            cue = " ".join(character.description.split())
            if len(cue) > 90:
                cue = cue[:90].rsplit(" ", 1)[0].rstrip(",;")
            return f"{character.name}: {cue}" if cue else character.name
    if name.strip().upper() in {"NARRATOR", "V.O.", "VOICE-OVER", "VOICEOVER"}:
        return "Narrator, voice-over, not on screen"
    return name.strip()


def dialogue_lines(development, shot) -> list[str]:
    return [f'[{_speaker(development, line.speaker)}]: "{line.text.strip()}"'
            for line in getattr(shot, "dialogue", None) or [] if line.text.strip()]


def make_prompt(card, development, shot, *, sound: bool) -> str:
    # The supplied first frame already defines cast, setting, look and composition.
    # Do not invent subjects or pretend that unattached reference assets exist.
    action = shot.action.strip()
    camera = shot.framing.strip()
    move = getattr(shot, "camera_move", "").strip()
    if move:
        camera += f". Camera movement: {move}"
    continuity = "Keep the first frame's identities, wardrobe, props, lighting, and colour grade consistent."
    if shot.notes.strip():
        continuity += " " + shot.notes.strip()
    lines = dialogue_lines(development, shot)
    if not sound:
        audio = "Silent video; no generated audio."
    elif shot.audio.strip() or has_speech(shot):
        audio = "Audio direction: " + (shot.audio.strip() or ("Only the dialogue below." if lines else "Use only the speech specified in the action."))
        if lines:
            audio += "\nDialogue, in this order:\n" + "\n".join(lines) + "\n"
        if has_speech(shot):
            audio += ("" if lines else " ") + f"Spoken language: {development.language}. Keep the supplied speaker-to-line assignments and exact dialogue; do not translate or add lines."
        else:
            audio += " Do not invent dialogue."
    else:
        audio = "No added dialogue or music."
    if card.id == KLING:
        prompt = (f"Continue from the supplied first frame in one uninterrupted {shot.duration_seconds}-second shot. "
                  f"{action}\nCamera and composition: {camera}\n{audio}\n{continuity}")
    elif card.id == SEEDANCE:
        # First-frame mode is exclusive of reference mode. @Image bindings would
        # falsely describe uploaded reference assets here, so name the first frame.
        prompt = ("Input: the supplied first frame fixes the opening composition, subjects, and setting.\n"
                  f"Summary: one continuous {shot.duration_seconds}-second shot continuing that scene.\n"
                  f"Timeline 0–{shot.duration_seconds}s:\n"
                  f"Visual action: {action}\nCamera and composition: {camera}\n{audio}\n"
                  f"Throughout: {continuity}\nKeep this as a single take, with no inserted cuts or added subtitles.")
    else:
        raise ValueError("No documented prompt template for this video model.")
    if len(prompt) > card.prompt_max:
        raise ValueError(f"s{shot.number:02d}: {card.name} permits at most {card.prompt_max} prompt characters; shorten the shot notes without dropping required dialogue.")
    return prompt
