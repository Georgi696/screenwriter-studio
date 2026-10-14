"""Original templates derived from official guides, not copied sample prompts.

Kling's guide uses cinematic prose and attributed dialogue; it does not require
seven uppercase fields or a fixed word budget. Seedance recommends a summary,
shot order/timeline, material roles when applicable, and continuity instructions.
"""

from screenwriter_studio.videos.catalog import KLING, SEEDANCE, has_speech


def make_prompt(card, development, shot, *, sound: bool) -> str:
    # The supplied first frame already defines cast, setting, look and composition.
    # Do not invent subjects or pretend that unattached reference assets exist.
    action = shot.action.strip()
    camera = shot.framing.strip()
    continuity = "Keep the first frame's identities, wardrobe, props, lighting, and colour grade consistent."
    if shot.notes.strip():
        continuity += " " + shot.notes.strip()
    if not sound:
        audio = "Silent video; no generated audio."
    elif shot.audio.strip() or has_speech(shot):
        audio = "Audio direction: " + (shot.audio.strip() or "Use only the speech specified in the action.")
        if has_speech(shot):
            audio += f" Spoken language: {development.language}. Keep the supplied speaker-to-line assignments and exact dialogue; do not translate or add lines."
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
