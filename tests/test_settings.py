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
        pro = settings.validate({"images": {"cinematic": "nano-banana-pro"}})
        routed, _ = settings.apply_images([{"id": "s01", "kind": "keyframe"}], "1K", pro)
        self.assertEqual(routed[0]["model"], "nano-banana-pro")
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
        durations = {model["id"]: (model["duration_min"], model["duration_max"]) for model in schema["video"]["models"]}
        self.assertEqual(durations, {"auto": (3, 30), "kling": (3, 15), "seedance": (4, 30)})
        self.assertEqual([m["id"] for m in schema["images"]["groups"][0]["options"]], ["nano-banana-2-1", "nano-banana-pro"])
        self.assertEqual([role["group"] for role in schema["chat"]["roles"]], ["story", "story", "story", "images", "images"])
        self.assertEqual([model["id"] for model in schema["chat"]["models"]], ["gpt-6-astra", "gpt-6-1-sol", "gpt-6-sol", "gpt-6-luna"])

    def test_characters_are_portrait_and_a_job_ratio_wins(self):
        jobs = portrait_characters([{"id": "a", "kind": "character"}, {"id": "s01", "kind": "keyframe"},
                                    {"id": "b", "kind": "character", "aspect_ratio": "1:1"}])
        self.assertEqual([job.get("aspect_ratio") for job in jobs], ["3:4", None, "1:1"])
        self.assertEqual(job_aspect(jobs[0], "16:9"), "3:4")
        self.assertEqual(job_aspect(jobs[1], "16:9"), "16:9")
        self.assertEqual(choose(jobs[0], "16:9", "1K")[0], "nano-banana-2-1")


if __name__ == "__main__":
    unittest.main()


class ReasoningEffortTest(unittest.TestCase):
    def tearDown(self):
        settings.PATH.unlink(missing_ok=True)

    def test_effort_is_validated_and_defaults_send_nothing(self):
        self.assertEqual(set(settings.validate({})["effort"].values()), {"default"})
        with self.assertRaises(ValueError):
            settings.validate({"effort": {"writer": "max"}})
        self.assertEqual(settings.reasoning_field("default"), {})
        self.assertEqual(settings.reasoning_field(None), {})
        self.assertEqual(settings.reasoning_field("low"), {"reasoning": {"effort": "low"}})
        self.assertEqual([e["id"] for e in settings.schema()["chat"]["efforts"]], ["default", "low", "medium", "high"])

    def test_chat_agent_applies_model_and_effort_only_when_chosen(self):
        from screenwriter_studio.crew.writer import writer_agent
        self.assertIs(settings.chat_agent("writer", writer_agent), writer_agent)
        settings.save({"effort": {"writer": "high"}})
        tuned = settings.chat_agent("writer", writer_agent)
        self.assertIsNot(tuned, writer_agent)
        self.assertEqual(tuned.model_settings.reasoning.effort, "high")
        self.assertIs(tuned.model, writer_agent.model)
        self.assertEqual(tuned.instructions, writer_agent.instructions)
        self.assertIsNone(writer_agent.model_settings.reasoning)


class ImageHardeningTest(unittest.TestCase):
    def test_repair_keeps_portrait_and_references_route_to_a_model_that_takes_them(self):
        from pathlib import Path
        from screenwriter_studio.images.review import repair_job
        job = {"id": "lena", "kind": "character", "aspect_ratio": "3:4", "prompt": "Lena", "references": []}
        record = {"inspection": {"changes": ["Fix the scar."], "preserve": ["Face."]}, "url": "https://host/lena.png"}
        repaired = repair_job(job, record, Path("/tmp/out"))
        self.assertEqual(repaired["aspect_ratio"], "3:4")
        self.assertEqual(job_aspect(repaired, "16:9"), "3:4")
        self.assertEqual(choose({"id": "t", "kind": "title", "references": ["loc"]}, "16:9", "1K")[0], "nano-banana-2-1")
        self.assertEqual(choose({"id": "t", "kind": "title"}, "16:9", "1K")[0], "gpt-image-2-5-sunburst-text-to-image")

    def test_downloads_must_be_images_and_uploads_use_the_real_type(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from screenwriter_studio.images import generator
        jpeg = b"\xff\xd8\xff\xe0" + b"0" * 64
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / "s01.png"
            with mock.patch.object(generator, "_within", side_effect=lambda seconds, fn: b"<html>expired</html>"):
                with self.assertRaisesRegex(RuntimeError, "not an image"):
                    generator.download("https://host/x", dest)
            self.assertFalse(dest.exists())
            dest.write_bytes(jpeg)
            self.assertTrue(generator.reference_data_url(dest).startswith("data:image/jpeg;base64,"))

    def test_inspection_reply_is_tidied_not_discarded(self):
        from screenwriter_studio.images.inspection import Inspection, normalize_review
        raw = {"verdict": "accept", "summary": "Fine", "findings": [f"f{n}" for n in range(20)],
               "changes": [], "preserve": [], "uncertainty": "", "confidence": 0.9}
        review = Inspection.model_validate(normalize_review(raw))
        self.assertEqual(len(review.findings), 12)
        self.assertEqual(review.verdict, "accept")
