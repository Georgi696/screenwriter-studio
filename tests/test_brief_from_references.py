"""Suggest a story from the selected reference images. The model call is a mock. No KIE credits."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from screenwriter_studio.web import brief
from screenwriter_studio.web.brief import BriefSuggestError, build_request, spark_for, suggest_brief

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20


class BriefFromReferencesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        images = self.root / "productions" / "frankfurt-core" / "images"
        images.mkdir(parents=True)
        for name in ("lena.png", "avenue.png", "crater.png"):
            (images / name).write_bytes(PNG)
        patcher = mock.patch("screenwriter_studio.studio.WORKSPACE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        key = mock.patch("screenwriter_studio.web.brief.load_kie_api_key", return_value="test-key")
        key.start()
        self.addCleanup(key.stop)
        self.chosen = [
            {"path": "frankfurt-core/images/lena.png", "mode": "character", "name": "Lena", "face": True, "wardrobe": True},
            {"path": "frankfurt-core/images/avenue.png", "mode": "location"},
            {"path": "frankfurt-core/images/crater.png", "mode": "style", "instruction": "Keep the ash haze."},
        ]

    def test_the_request_is_text_only_with_each_role_and_prompt(self):
        (self.root / "productions/frankfurt-core/images/jobs.json").write_text(json.dumps({"jobs": [
            {"id": "lena", "kind": "character", "prompt": "Lena, 32, olive skin, green fatigues."}]}))
        body = build_request("", salt="alpha", references=brief.resolve_references(self.chosen))
        text = body["input"]
        self.assertIsInstance(text, str)  # no image blocks, no data URLs
        self.assertNotIn("data:image", text)
        self.assertIn("Build the brief around them", text)
        self.assertNotIn(spark_for("alpha"), text)  # the references lead, not a random spark
        self.assertIn("Reference 1 “Lena” (lena.png) · character", text)
        self.assertIn("generated from: “Lena, 32, olive skin, green fatigues.”", text)
        self.assertIn("Keep the face", text)
        self.assertIn("Reference 2 (avenue.png) · location", text)
        self.assertIn("No generation prompt is saved", text)  # avenue has no saved plan in this fixture
        self.assertIn("Keep the ash haze.", text)

    def test_style_lends_its_look_and_angles_vary_the_story(self):
        (self.root / "productions/frankfurt-core/images/jobs.json").write_text(json.dumps({"jobs": [
            {"id": "crater", "kind": "location", "prompt": "Medium: photoreal still. Subject: a war crater in Frankfurt. "
             "Light: smoky daylight. Palette: soot black, fire orange. Constraints: military realism."}]}))
        refs = brief.resolve_references(self.chosen[2:])
        text = build_request("", salt="alpha", references=refs)["input"]
        self.assertIn("Light: smoky daylight.", text)
        self.assertIn("Palette: soot black, fire orange.", text)
        self.assertNotIn("Frankfurt", text)
        self.assertNotIn("military realism", text)
        self.assertIn(brief.angle_for("alpha"), text)
        angles = {brief.angle_for(salt) for salt in ("a", "b", "c", "d", "e", "f", "g", "h")}
        self.assertGreater(len(angles), 3)
        self.assertIn("Do not default to whoever the setting suggests first.", text)

    def test_earlier_suggestions_are_avoided_only_for_a_new_story(self):
        refs = brief.resolve_references(self.chosen[1:2])
        earlier = ["German soldier Lukas wants to reach the station.", "", "Medic Ana crosses the crater."]
        text = build_request("", salt="alpha", references=refs, avoid=earlier)["input"]
        self.assertIn("Write a different film", text)
        self.assertIn("- German soldier Lukas wants to reach the station.", text)
        self.assertIn("- Medic Ana crosses the crater.", text)
        kept = build_request("a medic crosses the avenue", salt="alpha", references=refs, avoid=earlier)["input"]
        self.assertNotIn("Write a different film", kept)

    def test_a_note_in_the_box_is_kept(self):
        text = build_request("a medic crosses the avenue", salt="alpha",
                             references=brief.resolve_references(self.chosen[1:2]))["input"]
        self.assertIn("a medic crosses the avenue", text)
        self.assertIn("No character is selected", text)

    def test_suggest_sends_no_pictures_and_refuses_paths_outside_productions(self):
        client = mock.Mock()
        client.responses.create.return_value = {"status": "completed", "output_text": "A 30-second 16:9 war short. Lena crosses the avenue."}
        self.assertIn("Lena", suggest_brief("", client=client, references=self.chosen))
        sent = client.responses.create.call_args.kwargs["input"]
        self.assertIsInstance(sent, str)
        self.assertIn("Lena", sent)
        for bad in ([{"path": "../secret.png", "mode": "style"}], [{"path": "frankfurt-core/images/lena.png", "mode": "hero"}]):
            with self.assertRaises(BriefSuggestError):
                suggest_brief("", client=client, references=bad)
        self.assertEqual(client.responses.create.call_count, 1)

    def test_reference_count_is_capped(self):
        many = []
        for number in range(10):
            name = f"frame{number}.png"
            (self.root / "productions/frankfurt-core/images" / name).write_bytes(PNG)
            many.append({"path": f"frankfurt-core/images/{name}", "mode": "location"})
        text = build_request("", salt="a", references=brief.resolve_references(many))["input"]
        self.assertIn(f"Reference {brief.MAX_REFERENCES} ", text)
        self.assertNotIn(f"Reference {brief.MAX_REFERENCES + 1} ", text)


class ImagePromptTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "productions"
        self.images = self.root / "core" / "images"
        self.images.mkdir(parents=True)
        for name in ("lena.png", "lena.repair-1.png", "s01.png", "s01.take-2.png", "upload.png"):
            (self.images / name).write_bytes(PNG)
        (self.images / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "lena", "kind": "character", "prompt": "Lena, 32, olive skin, green fatigues."},
            {"id": "s01", "kind": "keyframe", "prompt": "Wide shot, Lena crawls past a slab."}]}))
        (self.images / "manifest.json").write_text(json.dumps({"images": [
            {"id": "lena", "path": str(self.images / "lena.repair-1.png"), "history": [{"path": str(self.images / "lena.png")}]},
            {"id": "s01", "path": str(self.images / "s01.take-2.png"), "job_override": {"prompt": "Lena sprints through smoke."},
             "history": [{"path": str(self.images / "s01.png")}]}]}))

    def test_each_version_maps_to_the_prompt_it_was_made_from(self):
        self.assertEqual(brief.image_prompt(self.images / "lena.png"), "Lena, 32, olive skin, green fatigues.")
        self.assertEqual(brief.image_prompt(self.images / "lena.repair-1.png"), "Lena, 32, olive skin, green fatigues.")
        self.assertEqual(brief.image_prompt(self.images / "s01.take-2.png"), "Lena sprints through smoke.")
        self.assertEqual(brief.image_prompt(self.images / "s01.png"), "Wide shot, Lena crawls past a slab.")
        self.assertEqual(brief.image_prompt(self.images / "upload.png"), "")

    def test_a_copied_reference_is_followed_to_its_source(self):
        other = self.root / "next" / "references"
        other.mkdir(parents=True)
        (other / "character-01.png").write_bytes(PNG)
        (other.parent / "run_settings.json").write_text(json.dumps({"references": [
            {"id": "character-01", "path": str(other / "character-01.png"), "source": "core/images/lena.repair-1.png"}]}))
        self.assertEqual(brief.image_prompt(other / "character-01.png"), "Lena, 32, olive skin, green fatigues.")
        self.assertEqual(brief.image_prompt(other / "missing.png"), "")

    def test_the_description_carries_the_edited_prompt(self):
        item = {"source_path": self.images / "s01.take-2.png", "mode": "composition", "name": ""}
        self.assertIn("generated from: “Lena sprints through smoke.”", brief.reference_text([item]))


class ProviderOutageTest(unittest.TestCase):
    def setUp(self):
        for target, value in (("screenwriter_studio.web.brief.load_kie_api_key", "test-key"),
                              ("screenwriter_studio.web.brief.time.sleep", None)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def gateway(code=502):
        error = RuntimeError(f"Error code: {code} - {{'type': 'https://developers.cloudflare.com/support/troubleshooting/"
                             f"http-status-codes/cloudflare-5xx-errors/error-{code}/', 'title': 'Error {code}: Bad gateway'}}")
        error.status_code = code
        return error

    def test_a_gateway_error_is_retried_then_succeeds(self):
        client = mock.Mock()
        client.responses.create.side_effect = [self.gateway(), self.gateway(503), {"status": "completed", "output_text": "A 30-second brief."}]
        self.assertEqual(suggest_brief("", client=client), "A 30-second brief.")
        self.assertEqual(client.responses.create.call_count, 3)

    def test_a_lasting_outage_reads_as_one_plain_sentence(self):
        client = mock.Mock()
        client.responses.create.side_effect = self.gateway()
        with self.assertRaises(BriefSuggestError) as caught:
            suggest_brief("", client=client)
        message = str(caught.exception)
        self.assertIn("KIE's server did not answer (502)", message)
        self.assertNotIn("cloudflare", message.lower())
        self.assertEqual(client.responses.create.call_count, 3)

    def test_a_rejected_request_is_not_retried(self):
        client = mock.Mock()
        error = RuntimeError("Error code: 400 - model is not available")
        error.status_code = 400
        client.responses.create.side_effect = error
        with self.assertRaisesRegex(BriefSuggestError, "not available"):
            suggest_brief("", client=client)
        self.assertEqual(client.responses.create.call_count, 1)

    def test_crew_errors_use_the_same_sentence(self):
        from screenwriter_studio.errors import friendly
        self.assertIn("KIE's server did not answer (502)", friendly(self.gateway()))
        self.assertIn("rate-limiting", friendly(RuntimeError("Error code: 429 - too many requests")))
        self.assertNotIn("KIE's server", friendly(ValueError("Shot 3 is 40s")))


class SuggestRouteTest(unittest.TestCase):
    def test_the_desk_passes_references_through(self):
        from screenwriter_studio.web.server import DeskHandler
        handler = DeskHandler.__new__(DeskHandler)
        handler._json = mock.Mock()
        refs = [{"path": "frankfurt-core/images/lena.png", "mode": "character"}]
        with mock.patch("screenwriter_studio.web.server.suggest_brief", return_value="A brief.") as call:
            handler._suggest("", None, refs)
        call.assert_called_once_with("", references=refs)
        handler._json.assert_called_once_with(200, {"brief": "A brief."})


if __name__ == "__main__":
    unittest.main()
