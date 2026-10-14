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

SHOT_TAKE_RULE = """\
What one shot may hold (the writer and the script editor use this same rule):
- A shot is one unbroken take from one camera setup, as long as the clip range in the user message allows.
- Inside it, a chain of up to three closely linked beats is fine (she crawls, checks the tracker, ducks), as long as it happens in one place, in continuous time, with no cut.
- A shot needs trimming or splitting only when it requires a cut, jumps in time or place, has a second speaker, or chains more than three beats.
- Trim by keeping the beats that carry the story and moving the rest to audio or notes. A change of camera height, angle, or distance is framing, not an extra beat.
- Spoken lines fit the clip: about 2.5 words per second of shot, one speaker, so lip sync is not rushed or cut off.
"""

CAMERA_COVERAGE = """\
Camera coverage, on the shots that already exist:
- Consecutive shots must not share the same camera height, angle, and subject distance.
- Use a real mix: wide, medium, and close; high, eye, and low; and, when the scene has more than three shots, at least one shot from a different axis (side, rear, overhead, or ground).
- Action should feel like the camera moved: a low angle into a charge, a high angle on a collapse, a tight insert, an over-shoulder. Not a row of front-facing masters.
- Name a concrete angle, height, and distance. Cinematic or dynamic alone is not a camera.
- Vary the cameras on the shots that already exist. Do not add shots. Do not exceed the shot budget.
- Camera variety is framing, not extra action inside the shot. A shot stays one unbroken take.
"""
