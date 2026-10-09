"""Art-director context and still request fields. No network."""

import unittest
from pathlib import Path

from art_agent import art_agent
from generate_stills import build_payload, choose, run_job
from image_model_card import (
    CHECKED,
    EDIT_CARD,
    GPT_CARD,
    NANO_CARD,
    SEEDREAM_CARD,
    art_director_card,
)
from schemas import Beat, CharacterLock, Development, Screenplay, Shot
from studio import art_user_message


def _piece() -> tuple[Development, Screenplay]:
    development = Development(
        title="Hall",
        slug="hall",
        job_type="narrative",
        runtime_seconds=30,
        aspect_ratio="16:9",
        language="English",
        tone="quiet",
        audience="adults",
        logline="When the light fails, Maya must cross the hall before dawn.",
        beats=[
            Beat(name="Hook", timecode="0:00", action="Maya waits at the door."),
            Beat(name="Turn", timecode="0:10", action="The light goes out."),
            Beat(name="Button", timecode="0:20", action="She reaches the far door."),
        ],
        look="natural",
        palette="grey",
        lighting="one window",
        characters=[CharacterLock(name="Maya", description="A woman of 40 with short black hair.")],
        locations=["hall"],
        invented=[],
    )
    screenplay = Screenplay(
        fountain="Title: Hall\n\nINT. HALL - NIGHT\n\nMaya waits.\n",
        shots=[
            Shot(
                number=1,
                duration_seconds=8,
                framing="wide",
                action="Maya waits at the door.",
                audio="room tone",
                notes="",
            )
        ],
        notes=[],
    )
    return development, screenplay


class ArtDirectorCardTest(unittest.TestCase):
    def test_context_includes_official_constraints(self):
        development, screenplay = _piece()
        message = art_user_message(development, screenplay)
        card = art_director_card()
        self.assertIn(card, art_agent.instructions)
        self.assertIn(card, message)
        self.assertIn(CHECKED, message)
        for model in (NANO_CARD, GPT_CARD, SEEDREAM_CARD, EDIT_CARD):
            self.assertIn(model.model_id, message)
            for source in model.sources:
                self.assertIn(source, message)
        self.assertIn("image_input", message)
        self.assertIn("at most 10 image URLs", message)
        self.assertIn("up to 4 characters", message)
        self.assertIn("input_urls", message)
        self.assertIn("at most 16 image URLs", message)
        self.assertIn("basic outputs 1K", message)
        self.assertIn("high outputs 2K", message)
        self.assertIn("4 to 5000 characters", message)
        self.assertIn("27:16", message)
        self.assertIn("1K only", message)
        self.assertIn("16:9", message)

    def test_locked_piece_is_still_in_the_message(self):
        development, screenplay = _piece()
        message = art_user_message(development, screenplay)
        self.assertIn('"max_keyframes": 3', message)
        self.assertIn("Maya", message)


class RequestFieldTest(unittest.TestCase):
    def test_payload_uses_documented_field_names(self):
        nano = build_payload(NANO_CARD.model_id, "A still.", "16:9", "1K", ["https://example.com/a.png"])
        self.assertEqual(nano["model"], NANO_CARD.model_id)
        self.assertEqual(
            set(nano["input"]),
            {"prompt", "aspect_ratio", "resolution", "output_format", "image_input"},
        )
        self.assertEqual(nano["input"]["image_input"], ["https://example.com/a.png"])
        self.assertEqual(nano["input"]["output_format"], "png")
        self.assertTrue(set(nano["input"]).issubset(NANO_CARD.documented_fields))

        bare = build_payload(NANO_CARD.model_id, "A still.", "16:9", "1K", [])
        self.assertNotIn("image_input", bare["input"])

        many = [f"https://example.com/{index}.png" for index in range(12)]
        clipped = build_payload(NANO_CARD.model_id, "A still.", "16:9", "1K", many)
        self.assertEqual(len(clipped["input"]["image_input"]), NANO_CARD.max_images)

        poster = build_payload(GPT_CARD.model_id, "A poster.", "16:9", "2K", ["https://example.com/a.png"])
        self.assertEqual(set(poster["input"]), {"prompt", "aspect_ratio", "resolution"})
        self.assertNotIn("background", poster["input"])
        self.assertNotIn("size", poster["input"])
        self.assertTrue(set(poster["input"]).issubset(GPT_CARD.documented_fields))

        product = build_payload(SEEDREAM_CARD.model_id, "A red mug.", "1:1", "1K", [])
        self.assertEqual(set(product["input"]), {"prompt", "aspect_ratio", "quality", "output_format"})
        self.assertEqual(product["input"]["quality"], "basic")
        self.assertNotIn("resolution", product["input"])
        self.assertNotIn("image_input", product["input"])
        sharp = build_payload(SEEDREAM_CARD.model_id, "A red mug.", "16:9", "2K", [])
        self.assertEqual(sharp["input"]["quality"], "high")
        self.assertEqual(sharp["input"]["output_format"], "png")
        self.assertTrue(set(product["input"]).issubset(SEEDREAM_CARD.documented_fields))

        edit = build_payload(EDIT_CARD.model_id, "Change the cup.", "3:4", "1K", ["https://example.com/a.png"])
        self.assertEqual(edit["input"]["input_urls"], ["https://example.com/a.png"])
        self.assertEqual(set(edit["input"]), {"prompt", "input_urls", "aspect_ratio", "resolution"})
        self.assertTrue(set(edit["input"]).issubset(EDIT_CARD.documented_fields))
        urls = [f"https://example.com/{index}.png" for index in range(20)]
        wide = build_payload(EDIT_CARD.model_id, "Change the cup.", "1:1", "1K", urls)
        self.assertEqual(len(wide["input"]["input_urls"]), EDIT_CARD.max_images)

    def test_ratio_and_resolution_follow_the_card(self):
        self.assertEqual(choose({"kind": "poster"}, "16:9", "2K")[0], GPT_CARD.model_id)
        self.assertEqual(choose({"kind": "poster"}, "27:16", "1K")[0], GPT_CARD.model_id)
        self.assertEqual(choose({"kind": "poster"}, "27:16", "2K")[0], NANO_CARD.model_id)
        self.assertEqual(choose({"kind": "product"}, "16:9", "2K")[0], SEEDREAM_CARD.model_id)
        self.assertEqual(choose({"kind": "product"}, "16:9", "4K")[0], NANO_CARD.model_id)
        self.assertEqual(choose({"kind": "product"}, "4:5", "1K")[0], NANO_CARD.model_id)
        self.assertEqual(
            choose({"kind": "edit", "references": ["s01"]}, "16:9", "1K")[0],
            EDIT_CARD.model_id,
        )

    def test_prompt_length_is_the_documented_limit(self):
        short = run_job(
            "not-a-key",
            {"id": "mug", "kind": "product", "prompt": "hi"},
            "1:1",
            "1K",
            Path("/tmp/unused-stills"),
            {},
            False,
        )
        self.assertIn("at least 4", short["error"])

        long = run_job(
            "not-a-key",
            {"id": "maya", "kind": "character", "prompt": "a" * 20001},
            "16:9",
            "1K",
            Path("/tmp/unused-stills"),
            {},
            False,
        )
        self.assertIn("20000", long["error"])
        self.assertIn(NANO_CARD.model_id, long["error"])


if __name__ == "__main__":
    unittest.main()
