"""Short beat patterns injected into the writer. The full playbooks live in the skill."""

PLAYBOOKS: dict[str, str] = {
    "narrative": (
        "Narrative short. One dramatic question. Opening image, want visible in behavior, "
        "inciting incident by 25%, two raises that cost more each time, a crisis choice, "
        "a consequence, a closing image that rhymes with the opening. Three locations max. "
        "No flashbacks under 10 minutes. Resolve the character, not every plot thread."
    ),
    "sketch": (
        "Sketch. One game. Base reality, one unusual thing played straight, the game gets "
        "named, then heighten three times. Do not add a second joke. Cut on the biggest laugh."
    ),
    "ad": (
        "Ad. One message, one call to action, stated once in the final seconds. "
        "Write for the exact runtime. A trimmed 30 is not a 15. The product is in the picture, "
        "not explained."
    ),
    "vertical": (
        "Vertical social. Face or motion in frame one. The hook is the first second. "
        "No establishing shot. One speaker. Pattern interrupt, then one turn, then a button."
    ),
    "explainer": (
        "Explainer. One claim. Show the problem in behavior, demonstrate the mechanism, "
        "land one proof, then a single call to action. Voiceover may carry the exposition."
    ),
    "trailer": (
        "Trailer. Sell the question, not the plot. Three images that raise the stakes, "
        "one line of dialogue that turns, a final image that withholds the ending. "
        "No scene that exists only to explain."
    ),
    "rewrite": (
        "Rewrite pass. Keep the want, the obstacle, and the turn. Cut anything that does not "
        "serve the turn. Do not add a subplot."
    ),
}

AI_VIDEO_RULES = """\
AI video constraints:
- One shot is one continuous action inside one clip. Kling clips are 3-15s, Seedance 4-30s. Most beats land at 5-8s.
- One speaker per clip, at most about 20 words. Default the dialogue to American English.
- Kling accepts only 16:9, 9:16, and 1:1. Seedance also accepts 4:3, 3:4, and 21:9.
- No readable text in frame. No precise handwork (typing, pouring, handing objects over).
- Restate wardrobe, light, and location in every shot. The last frame of a shot can be the first of the next; say so in the notes.
"""
