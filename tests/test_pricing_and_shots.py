"""Credit estimates before paid steps, and the structured shot fields (cast, place, camera move, dialogue)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures import _piece
from screenwriter_studio import pricing, settings as studio_settings
from screenwriter_studio.images.catalog import GPT_CARD, NANO_CARD
from screenwriter_studio.images.service import attach_cast
from screenwriter_studio.schemas import CharacterLock, KieOutput, Line, Screenplay, Shot
from screenwriter_studio.studio import _script_issues, _shot_issues, _shots_markdown
from screenwriter_studio.videos.catalog import CARDS, KLING, SEEDANCE, choose_model, has_speech
from screenwriter_studio.videos.plan import digest, screenplay_digest, write_json
from screenwriter_studio.videos.prompts import make_prompt
from screenwriter_studio.web import server

PRO_SOUND = {"video": {"selection": "auto", "mode": "pro", "sound": True, "parallel": 3},
             "images": {"cinematic": NANO_CARD.model_id, "text": GPT_CARD.model_id,
                        "product": GPT_CARD.model_id, "resolution": "auto"}}


def _two_shot_piece():
    development, screenplay = _piece()
    development.runtime_seconds = 20
    development.characters.append(CharacterLock(name="Leo", description="A boy of 9 in a yellow raincoat."))
    shot = screenplay.shots[0]
    screenplay.shots = [shot, shot.model_copy(update={"number": 2, "duration_seconds": 12})]
    return development, screenplay


class PricingTest(unittest.TestCase):
    def test_video_estimate_uses_the_routed_model_and_its_rate(self):
        development, screenplay = _two_shot_piece()
        estimate = pricing.video_estimate(development, screenplay, mode="pro", sound=True, selection="auto")
        by_shot = {shot["id"]: shot for shot in estimate["shots"]}
        self.assertEqual(by_shot["s01"]["model"], KLING)
        self.assertEqual(by_shot["s01"]["credits"], 27 * 8)
        self.assertEqual(by_shot["s02"]["model"], SEEDANCE)  # 12 s favours Seedance
        self.assertEqual(by_shot["s02"]["credits"], 158 * 12)
        self.assertEqual(estimate["credits"], 27 * 8 + 158 * 12)
        self.assertEqual(estimate["usd"], round((27 * 8 + 158 * 12) * 0.005, 2))
        silent = pricing.video_estimate(development, screenplay, mode="std", sound=False, selection="kling")
        self.assertEqual(silent["credits"], 14 * 20)

    def test_a_shot_no_model_can_make_is_reported_not_priced(self):
        development, screenplay = _piece()
        screenplay.shots = [screenplay.shots[0].model_copy(update={"duration_seconds": 20})]
        estimate = pricing.video_estimate(development, screenplay, mode="4K", sound=True, selection="auto")
        self.assertEqual(estimate["unpriced"], ["s01"])
        self.assertEqual(estimate["credits"], 0)

    def test_image_estimate_counts_sheets_and_respects_the_image_limit(self):
        development, screenplay = _two_shot_piece()
        estimate = pricing.image_estimate(development, screenplay, values=PRO_SOUND)
        self.assertEqual(estimate["count"], 2 + 2 + 1)  # shots, characters, locations
        self.assertEqual(estimate["resolution"], "1K")
        self.assertEqual(estimate["credits"], 5 * 8)
        self.assertTrue(estimate["assumption"])
        self.assertEqual(pricing.image_estimate(development, screenplay, values=PRO_SOUND, image_count=3)["count"], 3)
        self.assertEqual(pricing.image_estimate(development, screenplay, values=PRO_SOUND, locked_count=2)["count"], 3)
        four_k = {**PRO_SOUND, "images": {**PRO_SOUND["images"], "resolution": "4K"}}
        self.assertEqual(pricing.image_estimate(development, screenplay, values=four_k)["per_image"], 18)

    def test_plan_estimate_separates_finished_clips(self):
        plan = {"mode": "pro", "sound": False, "shots": [
            {"id": "s01", "model": KLING, "duration_seconds": 5},
            {"id": "s02", "model": SEEDANCE, "duration_seconds": 10}]}
        estimate = pricing.plan_estimate(plan, [{"id": "s01", "state": "done"}, {"id": "s02", "state": "failed"}])
        self.assertEqual(estimate["shots"], {"s01": 90, "s02": 1580})
        self.assertEqual(estimate["credits"], 1670)
        self.assertEqual(estimate["remaining"], 1580)

    def test_remaining_images_price_each_job_on_its_routed_model(self):
        spec = {"aspect_ratio": "16:9", "resolution": "2K", "jobs": [
            {"id": "maya", "kind": "character"}, {"id": "s01", "kind": "keyframe"},
            {"id": "title", "kind": "title"}]}
        manifest = {"images": [{"id": "maya", "state": "done"}]}
        left = pricing.remaining_images(spec, manifest)
        self.assertEqual(left["count"], 2)
        self.assertEqual(left["credits"], 12 + 10)  # Nano 2K, then GPT Image 2K for the title

    def test_production_estimate_states_its_source_and_extras(self):
        development, screenplay = _piece()
        development.runtime_seconds = 8
        estimate = pricing.production_estimate(development, screenplay, values=PRO_SOUND)
        self.assertEqual(estimate["credits"], 3 * 8 + 27 * 8)
        self.assertIn("credits", estimate["text"])
        self.assertIn(pricing.SOURCE, estimate["text"])
        self.assertIn("extra", estimate["text"])

    def test_every_selectable_model_has_a_price(self):
        schema = studio_settings.schema()
        for group in schema["images"]["groups"]:
            for option in group["options"]:
                self.assertIn(option["id"], pricing.IMAGE_RATES)
                self.assertIn("credits per image", option["note"])
        for model, card in CARDS.items():
            for mode in card.modes:
                for sound in (True, False):
                    self.assertIsNotNone(pricing.video_rate(model, mode, sound), (model, mode, sound))
        self.assertIn("credits per second", schema["video"]["models"][1]["note"])
        self.assertEqual(schema["pricing"]["video"]["kling"]["pro/sound"], 27)

    def test_desk_status_carries_the_estimate_before_approval(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "film"
            folder.mkdir()
            development, screenplay = _piece()
            development.runtime_seconds = 8
            write_json(folder / "00_development.json", development.model_dump())
            write_json(folder / "01_screenplay.json", screenplay.model_dump())
            with mock.patch.object(studio_settings, "load", return_value=studio_settings.validate(PRO_SOUND)):
                view = server.cost_view(folder, True)
            self.assertEqual(view["estimate"]["videos"]["credits"], 27 * 8)
            self.assertIsNone(view["images_estimate"])
            self.assertIsNone(server.cost_view(folder, False)["estimate"])


class StructuredShotTest(unittest.TestCase):
    def test_strict_schema_requires_the_fields_and_old_screenplays_still_load(self):
        schema = KieOutput(Screenplay).json_schema()
        shot = schema["properties"]["shots"]["items"]
        self.assertTrue({"characters", "location", "camera_move", "dialogue"} <= set(shot["required"]))
        self.assertNotIn('"default"', json.dumps(schema))
        self.assertNotIn("$ref", json.dumps(schema))
        legacy = {"number": 1, "duration_seconds": 8, "framing": "wide", "action": "Maya waits.", "audio": "", "notes": ""}
        loaded = Screenplay.model_validate({"fountain": "x", "notes": [], "shots": [legacy]})
        self.assertEqual(loaded.shots[0].dialogue, [])
        # A video plan saved before these fields existed keeps its screenplay fingerprint.
        self.assertEqual(screenplay_digest(loaded), digest({"fountain": "x", "notes": [], "shots": [legacy]}))
        filled = loaded.model_copy(update={"shots": [loaded.shots[0].model_copy(update={"characters": ["Maya"]})]})
        self.assertNotEqual(screenplay_digest(filled), screenplay_digest(loaded))

    def test_dialogue_drives_speech_and_attributed_prompt_lines(self):
        development, screenplay = _piece()
        shot = screenplay.shots[0].model_copy(update={
            "audio": "Rain on glass.", "camera_move": "slow push in", "characters": ["Maya"],
            "dialogue": [Line(speaker="Maya", text="Open the door.")]})
        self.assertTrue(has_speech(shot))
        self.assertFalse(has_speech(screenplay.shots[0].model_copy(update={"audio": "Rain on glass."})))
        prompt = make_prompt(CARDS[KLING], development, shot, sound=True)
        self.assertIn('[Maya: A woman of 40 with short black hair.]: "Open the door."', prompt)
        self.assertIn("Camera movement: slow push in", prompt)
        self.assertIn("Spoken language: English", prompt)
        self.assertNotIn("Open the door", make_prompt(CARDS[KLING], development, shot, sound=False))

    def test_listed_cast_routes_multi_subject_shots(self):
        development, screenplay = _two_shot_piece()
        shot = screenplay.shots[0].model_copy(update={"duration_seconds": 6, "characters": ["Maya", "Leo"]})
        card, reason = choose_model(development, shot, mode="pro", sound=True)
        self.assertEqual(card.id, SEEDANCE)
        self.assertIn("Several named subjects", reason)

    def test_editor_notes_check_names_places_and_speakers_without_blocking_work(self):
        development, screenplay = _piece()
        development.runtime_seconds = 8
        clean = screenplay.shots[0].model_copy(update={
            "characters": ["maya"], "location": "Hall", "dialogue": [Line(speaker="MAYA", text="Wait.")]})
        screenplay.shots = [clean]
        self.assertEqual(_shot_issues(development, screenplay), [])
        screenplay.shots = [clean.model_copy(update={
            "characters": ["Mia"], "location": "Garden",
            "dialogue": [Line(speaker="Maya", text="Wait."), Line(speaker="Bob", text="No.")]})]
        issues = " ".join(_shot_issues(development, screenplay))
        for expected in ("Mia", "Garden", "2 speakers", "bob"):
            self.assertIn(expected, issues)
        self.assertEqual(_script_issues(development, screenplay), [])
        narrator = clean.model_copy(update={"dialogue": [Line(speaker="Narrator", text="Dawn came late.")]})
        screenplay.shots = [narrator]
        self.assertEqual(_shot_issues(development, screenplay), [])

    def test_shot_images_get_the_sheets_of_the_listed_cast(self):
        jobs = [{"id": "maya", "kind": "character"}, {"id": "leo-sheet", "kind": "character"},
                {"id": "hall", "kind": "location"},
                {"id": "s01", "kind": "keyframe", "references": ["hall"]},
                {"id": "s02", "kind": "keyframe", "references": ["maya"]},
                {"id": "title", "kind": "title"}]
        shots = [{"number": 1, "characters": ["Maya", "Leo"]}, {"number": 2, "characters": ["Maya", "Nobody"]},
                 {"number": 3, "characters": ["Maya"]}]
        attached = {job["id"]: job for job in attach_cast(jobs, shots)}
        self.assertEqual(attached["s01"]["references"], ["hall", "maya", "leo-sheet"])
        self.assertEqual(attached["s02"]["references"], ["maya"])
        self.assertNotIn("references", attached["title"])
        self.assertEqual(attach_cast(jobs, []), jobs)
        twins = [{"id": "maya-young", "kind": "character"}, {"id": "maya-old", "kind": "character"},
                 {"id": "s01", "kind": "keyframe"}]
        self.assertEqual(attach_cast(twins, shots)[-1]["references"], [])

    def test_shot_list_and_desk_show_the_new_fields(self):
        development, screenplay = _piece()
        screenplay.shots = [screenplay.shots[0].model_copy(update={
            "characters": ["Maya"], "location": "hall", "camera_move": "static",
            "dialogue": [Line(speaker="Maya", text="Wait | here.")]})]
        table = _shots_markdown(screenplay)
        self.assertIn("| Move | Cast | Location |", table)
        self.assertIn('Maya: "Wait / here."', table)
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            write_json(folder / "00_development.json", development.model_dump())
            write_json(folder / "01_screenplay.json", screenplay.model_dump())
            shot = server.shot_plan(folder)["shots"][0]
        self.assertEqual(shot["characters"], ["Maya"])
        self.assertEqual(shot["dialogue"], [{"speaker": "Maya", "text": "Wait | here."}])
        self.assertEqual(shot["camera_move"], "static")


if __name__ == "__main__":
    unittest.main()
