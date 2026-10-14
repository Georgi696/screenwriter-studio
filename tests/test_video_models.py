"""Offline contracts for documented model selection and prompt adapters."""

import tempfile
import unittest
from unittest import mock

from tests.fixtures import _piece
from tests.test_video import production
from screenwriter_studio.schemas import CharacterLock
from screenwriter_studio.videos.catalog import CARDS, KLING, SEEDANCE, choose_model
from screenwriter_studio.videos.plan import build_plan, payload, read_json, write_json
from screenwriter_studio.videos.prompts import make_prompt
from screenwriter_studio.videos.web import video_status


class VideoModelsTest(unittest.TestCase):
    def setUp(self):
        self.development, screenplay = _piece()
        self.shot = screenplay.shots[0]

    def choose(self, **settings):
        return choose_model(self.development, self.shot, **{"mode": "pro", "sound": True, **settings})[0].id

    def test_short_dialogue_extended_action_and_complex_camera(self):
        self.assertEqual(self.choose(), KLING)
        self.shot.duration_seconds = 12
        self.assertEqual(self.choose(), SEEDANCE)
        self.shot.audio = 'Maya (softly): "Stay here."'
        self.assertEqual(self.choose(), KLING)
        self.shot.audio = "room tone"
        self.shot.duration_seconds = 8
        self.shot.framing = "The camera tracks forward, then tilts up toward the roof."
        self.assertEqual(self.choose(), SEEDANCE)

    def test_native_resolution_and_exact_duration_win_over_preferences(self):
        self.shot.duration_seconds = 12
        self.assertEqual(self.choose(mode="4K"), KLING)
        self.shot.duration_seconds = 3
        self.assertEqual(self.choose(), KLING)
        with self.assertRaisesRegex(ValueError, "4–30"):
            self.choose(selection="seedance")
        self.shot.duration_seconds = 8
        with self.assertRaisesRegex(ValueError, "4K"):
            self.choose(selection="seedance", mode="4K")

    def test_named_subject_staging_and_explicit_choice(self):
        self.development.characters.append(CharacterLock(name="Leo", description="A man in a red coat."))
        self.shot.action = "Maya hands Leo the key while he opens the door."
        self.assertEqual(self.choose(), SEEDANCE)
        self.assertEqual(self.choose(selection="kling"), KLING)
        self.shot.action = "Maya waits."
        self.assertEqual(self.choose(selection="seedance"), SEEDANCE)

    def test_speech_language_filters_candidates_without_silent_translation(self):
        self.shot.audio = 'Maya says: "Bom dia."'
        self.development.language = "Portuguese"
        self.assertEqual(self.choose(), SEEDANCE)
        with self.assertRaisesRegex(ValueError, "no documented model"):
            self.choose(mode="4K")
        self.development.language = "English and German"
        with self.assertRaisesRegex(ValueError, "no documented model"):
            self.choose()
        # The language does not constrain ambient-only/silent footage.
        self.assertEqual(self.choose(sound=False), KLING)
        self.shot.audio = "Room tone, no dialogue."
        self.assertEqual(self.choose(), KLING)

    def test_provider_adapters_use_distinct_fields_types_and_first_frame_mode(self):
        for model in (KLING, SEEDANCE):
            plan = {"mode": "pro", "sound": True, "aspect_ratio": "9:16"}
            shot = {"id": "s01", "model": model, "duration_seconds": 8, "prompt": "A woman turns toward the camera."}
            request = payload(plan, shot, "https://host/keyframe.png")
            self.assertEqual(request["model"], model)
            fields = request["input"]
            if model == KLING:
                self.assertEqual(fields["duration"], "8")
                self.assertEqual(fields["image_urls"], ["https://host/keyframe.png"])
                self.assertEqual(fields["mode"], "pro")
                self.assertNotIn("generate_audio", fields)
            else:
                self.assertEqual(fields["duration"], 8)
                self.assertEqual(fields["first_frame_url"], "https://host/keyframe.png")
                self.assertEqual(fields["resolution"], "1080p")
                self.assertEqual(fields["aspect_ratio"], "adaptive")
                self.assertTrue(fields["generate_audio"])
                self.assertEqual(fields["output_format"], "mp4")
                for forbidden in ("reference_image_urls", "image_urls", "sound", "mode", "multi_shots"):
                    self.assertNotIn(forbidden, fields)

    def test_prompt_formats_preserve_action_dialogue_notes_without_fake_references(self):
        self.shot.audio = 'Maya (quietly): "Do not leave." Rain on the roof.'
        self.shot.notes = "Keep the key in her left hand."
        for model in (KLING, SEEDANCE):
            prompt = make_prompt(CARDS[model], self.development, self.shot, sound=True)
            for fragment in (self.shot.action, self.shot.audio, self.shot.notes, "English"):
                self.assertIn(fragment, prompt)
            self.assertNotIn("@Image", prompt)
            self.assertNotIn("SUBJECT:", prompt)
            self.assertEqual("Timeline 0–8s:" in prompt, model == SEEDANCE)
            muted = make_prompt(CARDS[model], self.development, self.shot, sound=False)
            self.assertNotIn(self.shot.audio, muted)
            self.assertIn("Silent video", muted)

    def test_model_prompt_limits_are_enforced_before_submission(self):
        for model in (KLING, SEEDANCE):
            card = CARDS[model]
            self.shot.action = "a" * (card.prompt_max + 1)
            with self.assertRaisesRegex(ValueError, str(card.prompt_max)):
                make_prompt(card, self.development, self.shot, sound=True)
            with self.assertRaisesRegex(ValueError, "prompt"):
                payload({"mode": "pro", "sound": True, "aspect_ratio": "16:9"},
                        {"id": "s01", "model": model, "duration_seconds": 8, "prompt": self.shot.action}, "https://host/frame.png")


class VideoRoutingPlanTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = production(temp.name)

    def test_auto_falls_back_for_required_long_prompt_instead_of_truncating(self):
        path = self.folder / "01_screenplay.json"
        screenplay = read_json(path)
        screenplay["shots"][0]["notes"] = "Continuity detail. " * 180
        write_json(path, screenplay)
        plan = build_plan(self.folder)
        self.assertEqual(plan["shots"][0]["model"], SEEDANCE)
        self.assertIn(screenplay["shots"][0]["notes"].strip(), plan["shots"][0]["prompt"])

    def test_saved_choices_and_prompts_are_frozen_across_router_updates(self):
        original = build_plan(self.folder, selection="seedance")
        with mock.patch("screenwriter_studio.videos.plan.choose_model", side_effect=AssertionError("must not reroute")), mock.patch("screenwriter_studio.videos.plan.make_prompt", side_effect=AssertionError("must not rewrite")):
            resumed = build_plan(self.folder, selection="seedance", frozen_plan=original)
        self.assertEqual(resumed, original)

    def test_original_kling_plan_remains_compatible(self):
        # Version 1 has no per-shot model, selection or source metadata.
        original = build_plan(self.folder, selection="kling", frozen_plan={"version": 1, "shots": []})
        self.assertEqual(original["version"], 1)
        self.assertEqual(original["model"], KLING)
        self.assertNotIn("selection", original)
        self.assertNotIn("model", original["shots"][0])
        self.assertIn("SUBJECT:", original["shots"][0]["prompt"])
        self.assertEqual(original, build_plan(self.folder, selection="kling", frozen_plan=original))
        self.assertEqual(payload(original, original["shots"][0], "https://host/frame.png")["model"], KLING)

    def test_preview_shows_selected_model_reasons_and_official_sources(self):
        with mock.patch("screenwriter_studio.videos.web.require_tools"):
            status = video_status(self.folder, options={"selection": "seedance", "mode": "std", "sound": False})
        self.assertTrue(status["can_plan"])
        self.assertEqual(status["selection"], "seedance")
        shot = status["shots"][0]
        self.assertEqual(shot["model"], SEEDANCE)
        self.assertIn("Selected explicitly", shot["selection_reason"])
        self.assertIn("https://docs.kie.ai/market/bytedance/seedance-2-5", shot["sources"])
        self.assertNotIn("image", shot)
        bad = video_status(self.folder, options={"selection": "seedance", "mode": "4K"})
        self.assertFalse(bad["ready"])
        self.assertIn("4K", bad["message"])
