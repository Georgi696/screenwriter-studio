"""Studio-wide model choices: validation, routing onto jobs, and portrait character sheets."""

import unittest

from screenwriter_studio import settings
from screenwriter_studio.images.generator import choose, job_aspect
from screenwriter_studio.images.service import portrait_characters


class SettingsTest(unittest.TestCase):
    def test_defaults_are_valid_and_complete(self):
        self.assertEqual(settings.validate({}), settings.DEFAULTS)
        self.assertEqual(settings.validate(settings.DEFAULTS), settings.DEFAULTS)

    def test_unknown_models_and_impossible_quality_are_rejected(self):
        for bad in ({"chat": {"writer": "nope"}}, {"images": {"text": "nope"}}, {"images": {"resolution": "8K"}},
                    {"video": {"selection": "veo"}}, {"video": {"selection": "seedance", "mode": "4K"}},
                    {"video": {"sound": "yes"}}, {"video": {"parallel": 9}}, {"chat": []}):
            with self.assertRaises(ValueError, msg=bad):
                settings.validate(bad)
        self.assertEqual(settings.validate({"video": {"selection": "kling", "mode": "4K"}})["video"]["mode"], "4K")

    def test_chosen_image_model_is_written_on_the_job_and_resolution_forced(self):
        values = settings.validate({"images": {"product": "nano-banana-2-1", "resolution": "2K"}})
        jobs, resolution = settings.apply_images([
            {"id": "mug", "kind": "packshot"}, {"id": "s01", "kind": "keyframe"}, {"id": "t", "kind": "title"}], "1K", values)
        self.assertEqual(jobs[0]["model"], "nano-banana-2-1")
        self.assertNotIn("model", jobs[1])
        self.assertNotIn("model", jobs[2])
        self.assertEqual(resolution, "2K")
        self.assertEqual(settings.apply_images([{"id": "m", "kind": "packshot"}], "1K", settings.DEFAULTS)[1], "1K")

    def test_save_and_load_round_trip(self):
        saved = settings.save({"video": {"parallel": 2}})
        self.assertEqual(settings.load(), saved)
        settings.PATH.unlink()
        self.assertEqual(settings.load(), settings.DEFAULTS)

    def test_schema_lists_only_models_with_verified_contracts(self):
        schema = settings.schema()
        self.assertEqual([m["id"] for m in schema["video"]["models"]], ["auto", "kling", "seedance"])
        self.assertEqual([m["id"] for m in schema["images"]["groups"][0]["options"]], ["nano-banana-2-1"])

    def test_characters_are_portrait_and_a_job_ratio_wins(self):
        jobs = portrait_characters([{"id": "a", "kind": "character"}, {"id": "s01", "kind": "keyframe"},
                                    {"id": "b", "kind": "character", "aspect_ratio": "1:1"}])
        self.assertEqual([job.get("aspect_ratio") for job in jobs], ["3:4", None, "1:1"])
        self.assertEqual(job_aspect(jobs[0], "16:9"), "3:4")
        self.assertEqual(job_aspect(jobs[1], "16:9"), "16:9")
        self.assertEqual(choose(jobs[0], "16:9", "1K")[0], "nano-banana-2-1")


if __name__ == "__main__":
    unittest.main()
