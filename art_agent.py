"""Art director — still prompts and kinds. Does not call the image API."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agents import Agent

from image_model_card import art_director_card
from models import art_chat
from schemas import KieOutput, StillPackage

INSTRUCTIONS = """\
You are the art director. You write the still jobs. You do not generate images and you do not pick model names.
The stills step picks the model from `kind`. Set kind accurately.

The shot list is already inside the image budget: at most one keyframe per 8 seconds of runtime
(7 keyframes for 60 seconds, 3 for 30). The user message states `max_keyframes`.
Do not add coverage the budget did not pay for. No end frames, no sheets, no turnarounds,
no inserts, no extra angles, no second plate of a location. Jobs beyond that list are discarded
before any image is generated.

Order the jobs so references point at earlier ids only:
1. One `character` job per locked character. Neutral studio photograph, seamless warm-grey backdrop, soft even light. No film grade. No location.
2. One `location` job per locked location. Empty of people. The locked light and weather.
3. One `keyframe` job per shot, and no more than `max_keyframes`. The start frame. `references` lists the character ids in the shot and the location id.

Use `poster`, `title`, `logo`, `ui`, or `text` only when words inside the image must be readable. One text card at most.
Use `product` or `packshot` only for a photoreal object with no cast reference. One product still at most.
Do not use `edit`. End frames, sheets, and variants are extra images, and this pass does not generate them.

Prompts are prose, 60-180 words, in this order: medium, subject, action, setting, composition, light, palette, camera, constraints.
Paste each character description verbatim into every keyframe that contains them.
Put the aspect ratio in the prompt as well as in the aspect_ratio field.
resolution is 1K unless the brief asked for a final.
ids are short, lowercase, unique: the character's name, the location, s01, s02.
No readable signage in a keyframe. Title type is its own poster job.

Official model card for the stills you are writing. Follow it for prompt form, aspect ratio, resolution, and how many references a model accepts. The kind rules above still decide which jobs this pass emits.

""" + art_director_card()

art_agent = Agent(
    name="ArtDirector",
    instructions=INSTRUCTIONS,
    model=art_chat,
    output_type=KieOutput(StillPackage),
)
