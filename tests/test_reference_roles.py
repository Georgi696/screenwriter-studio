"""Reference roles survive resume and reach only the intended image jobs. No network."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from screenwriter_studio import studio
from screenwriter_studio.images.service import apply_reference_settings, render_stills
from screenwriter_studio.images.review import repair_job
from screenwriter_studio.references import reference_choices, reference_direction
from screenwriter_studio.schemas import StillJob, StillPackage


SHOTS = [
    {"number": 1, "characters": ["Maya"], "location": "Station"},
    {"number": 2, "characters": ["Lee"], "location": "Garden"},
]


class ReferenceRolesTest(unittest.TestCase):
    def jobs(self):
        return [{"id": name, "kind": kind, "prompt": "A cinematic still.", "references": []}
                for name, kind in [("maya", "character"), ("station", "location"),
                                   ("s01", "keyframe"), ("s02", "keyframe")]]

    def apply(self, refs, jobs=None):
        return {job["id"]: job for job in apply_reference_settings(
            self.jobs() if jobs is None else jobs, refs, SHOTS, "16:9", "1K")}

    def test_legacy_paths_and_modes_stay_compatible(self):
        self.assertEqual(reference_choices(["a.png", {"path": "b.png", "mode": "style"}]),
                         [{"path": "a.png", "mode": "lock"}, {"path": "b.png", "mode": "style"}])

    def test_normalization_and_duplicate_paths(self):
        ref = {"path": " a.png ", "mode": "character", "name": " Maya ", "scope": "shots",
               "shots": [2, 1, 2], "face": False, "wardrobe": True, "instruction": " Red coat "}
        result = reference_choices([ref, {"path": "a.png", "mode": "style"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], {**ref, "path": "a.png", "name": "Maya", "shots": [1, 2], "instruction": "Red coat"})

    def test_invalid_settings_are_rejected(self):
        for extra in [
            {"mode": "copy"}, {"scope": "all"}, {"scope": "shots"}, {"scope": "auto", "shots": [1]},
            {"shots": [True]}, {"shots": [0]}, {"shots": [241]}, {"shots": "1,2"},
            {"instruction": "x" * 1001}, {"name": 123},
            {"mode": "character", "face": "false"},
            {"mode": "character", "face": False, "wardrobe": False},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                reference_choices([{"path": "a.png", **extra}])

    def test_settings_survive_copy_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, "WORKSPACE_ROOT", Path(tmp)):
            root = Path(tmp) / "productions"
            source = root / "old" / "a.png"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"\x89PNG\r\n\x1a\nreference")
            choices = reference_choices([{"path": "old/a.png", "mode": "character", "name": "Maya",
                                          "face": False, "wardrobe": True, "scope": "shots", "shots": [2],
                                          "instruction": "Keep the coat."}])
            planned = studio._plan_references(choices)
            folder = root / "new"
            saved = studio._copy_references(folder, planned)
            studio._save_settings(folder, image_count=2, references=saved)
            count, loaded = studio._load_settings(folder)
            self.assertEqual(count, 2)
            self.assertEqual(loaded, saved)
            self.assertEqual(loaded[0]["id"], "character-01")
            self.assertFalse(loaded[0]["face"])
            self.assertEqual(loaded[0]["shots"], [2])
            self.assertTrue(source.is_file())
            self.assertEqual(Path(loaded[0]["path"]).read_bytes(), source.read_bytes())

    def test_specific_shots_override_planner_and_never_leak_through_sheets(self):
        refs = [{"id": "composition-01", "mode": "composition", "shots": [2]}]
        jobs = self.jobs()
        for job in jobs:
            job["references"] = ["composition-01"]
        result = self.apply(refs, jobs)
        for name in ("maya", "station", "s01"):
            self.assertEqual(result[name]["references"], [])
        self.assertEqual(result["s02"]["references"], ["composition-01"])
        self.assertIn("framing, camera angle and subject placement only", result["s02"]["prompt"])
        self.assertEqual(jobs[0]["references"], ["composition-01"], "caller plan is unchanged")

    def test_named_subjects_follow_structured_shots_even_if_planner_forgets(self):
        refs = [{"id": "character-01", "mode": "character", "name": "Maya", "face": False, "wardrobe": True},
                {"id": "location-01", "mode": "location", "name": "Station"}]
        jobs = self.jobs()
        jobs[-1]["references"] = ["character-01", "location-01"]
        result = self.apply(refs, jobs)
        self.assertEqual(result["s01"]["references"], ["character-01", "location-01"])
        self.assertEqual(result["s02"]["references"], [])
        self.assertIn("Do not copy the face or identity", result["s01"]["prompt"])
        self.assertIn("Keep the wardrobe", result["s01"]["prompt"])

    def test_style_is_last_and_can_be_limited_to_specific_shots(self):
        jobs = self.jobs()
        jobs[-1]["references"] = ["maya"]
        automatic = self.apply([{"id": "style-01", "mode": "style"}], jobs)
        self.assertEqual(automatic["maya"]["references"], [])
        self.assertEqual(automatic["station"]["references"], ["style-01"])
        self.assertEqual(automatic["s02"]["references"], ["maya", "style-01"])
        self.assertIn("Reference image 2", automatic["s02"]["prompt"])
        scoped = self.apply([{"id": "style-01", "mode": "style", "shots": [2]}], jobs)
        self.assertEqual(scoped["station"]["references"], [])
        self.assertEqual(scoped["s01"]["references"], [])
        self.assertEqual(scoped["s02"]["references"], ["maya", "style-01"])

    def test_object_automatic_scope_uses_art_director_attachment(self):
        jobs = self.jobs()
        jobs[-1]["references"] = ["object-01"]
        result = self.apply([{"id": "object-01", "mode": "object", "instruction": "Keep the red dial."}], jobs)
        self.assertEqual(result["s01"]["references"], [])
        self.assertIn("Keep the red dial", result["s02"]["prompt"])
        self.assertIn("shape, materials and identifying details", result["s02"]["prompt"])

    def test_missing_shots_and_excess_references_fail_before_generation(self):
        with self.assertRaisesRegex(ValueError, "missing shots: 3"):
            self.apply([{"id": "style-01", "mode": "style", "shots": [3]}])
        with self.assertRaisesRegex(ValueError, "without a planned image"):
            self.apply([{"id": "style-01", "mode": "style", "shots": [2]}], self.jobs()[:-1])
        with self.assertRaisesRegex(ValueError, "too many references"):
            self.apply([{"id": f"style-{i}", "mode": "style"} for i in range(10)])

    def test_composition_does_not_change_shared_reference_sheet_framing(self):
        jobs = self.jobs()
        for job in jobs:
            job["references"] = ["composition-01"]
        result = self.apply([{"id": "composition-01", "mode": "composition"}], jobs)
        self.assertEqual(result["maya"]["references"], [])
        self.assertEqual(result["station"]["references"], [])
        self.assertEqual(result["s01"]["references"], ["composition-01"])

    def test_long_instructions_fail_instead_of_dropping_reference(self):
        jobs = self.jobs()
        jobs[-1]["prompt"] = "x" * 20000
        with self.assertRaisesRegex(ValueError, "Shorten reference instructions"):
            self.apply([{"id": "style-01", "mode": "style"}], jobs)

    def test_render_manifest_contains_roles_and_scope(self):
        refs = [{"id": "character-01", "mode": "character", "path": "/unused/a.png", "shots": [2],
                 "name": "Lee", "face": True, "wardrobe": False}]
        package = StillPackage(aspect_ratio="16:9", resolution="1K", jobs=[
            StillJob(id="s01", kind="keyframe", prompt="Maya waits."),
            StillJob(id="s02", kind="keyframe", prompt="Lee enters."),
        ])
        with tempfile.TemporaryDirectory() as tmp, patch("screenwriter_studio.images.service.run_batch") as batch:
            render_stills(package, Path(tmp), dry_run=True, inspection_context={"shots": SHOTS}, locked_references=refs)
            spec = json.loads((Path(tmp) / "jobs.json").read_text())
            self.assertEqual(spec["locked_references"], refs)
            self.assertEqual(spec["jobs"][0]["references"], [])
            self.assertEqual(spec["jobs"][1]["references"], ["character-01"])
            self.assertIn("Use the scripted wardrobe", spec["jobs"][1]["prompt"])
            self.assertEqual(batch.call_args.args[0], spec)

    def test_repair_keeps_original_roles_and_explains_image_number_offset(self):
        job = self.apply([{"id": "style-01", "mode": "style"}])["s01"]
        fixed = repair_job(job, {"url": "https://example.com/candidate.png", "inspection": {
            "changes": ["Correct the hand."], "preserve": ["Keep the framing."]}}, Path("/tmp"))
        self.assertEqual(fixed["references"], ["https://example.com/candidate.png", "style-01"])
        self.assertIn("excluding the first candidate image", fixed["prompt"])
        self.assertIn(reference_direction({"mode": "style"}), fixed["prompt"])


if __name__ == "__main__":
    unittest.main()


class ReferenceCoverageTest(unittest.TestCase):
    """Images a reference makes unnecessary, and what the story crew is told about references."""

    LENA = {"id": "character-01", "mode": "character", "name": "Lena", "face": True, "wardrobe": True}
    LAKE = {"id": "location-01", "mode": "location", "name": "the alpine lake"}

    def covered(self, refs, characters=("Lena",), locations=("The alpine lake at dawn, lifting mist",)):
        from screenwriter_studio.references import covered_sheets
        return covered_sheets(refs, list(characters), list(locations))

    def test_matched_character_and_location_references_cover_their_sheets(self):
        self.assertEqual(self.covered([self.LENA, self.LAKE]), 2)
        self.assertEqual(self.covered([{**self.LENA, "name": "Lena Vogt"}]), 1)

    def test_partial_scoped_or_unmatched_references_still_need_a_sheet(self):
        self.assertEqual(self.covered([{**self.LENA, "wardrobe": False}]), 0)
        self.assertEqual(self.covered([{**self.LENA, "scope": "shots", "shots": [1]}]), 0)
        self.assertEqual(self.covered([{**self.LENA, "name": "Maya"}]), 0)
        self.assertEqual(self.covered([{**self.LAKE, "name": "lake"}], locations=["Lakeside cabin, night"]), 0)
        self.assertEqual(self.covered([{"id": "style-01", "mode": "style", "name": "Lena"}]), 0)

    def test_lock_references_keep_counting_and_total_is_capped(self):
        locks = [{"id": f"ref-0{n}", "mode": "lock", "name": f"r{n}"} for n in range(1, 4)]
        self.assertEqual(self.covered(locks), 2)
        self.assertEqual(self.covered([locks[0], self.LAKE]), 2)

    def test_image_limit_counts_only_images_still_to_make(self):
        from tests.fixtures import _piece
        development, screenplay = _piece()
        refs = [{**self.LENA, "name": "Maya"}, {**self.LAKE, "name": "hall"}]
        with patch.object(studio, "_script_issues", return_value=[]), \
                patch.object(studio, "choose_model", return_value=None):
            needed = studio.video_feasibility(development, screenplay, image_count=1,
                                              locked_count=studio._covered(development, refs))
            self.assertEqual(needed, [])
            short = studio.video_feasibility(development, screenplay, image_count=1, locked_count=0)
        self.assertIn("need about 3", short[0])

    def test_story_crew_gets_roles_and_names_not_image_directions(self):
        from tests.fixtures import _piece
        development, _ = _piece()
        refs = [{**self.LENA, "instruction": "She is older here."}, self.LAKE]
        writer = studio.writer_user_message(development, None, None, refs)
        self.assertIn("- Lena: Character: the picture supplies the face and wardrobe.", writer)
        self.assertIn("The user adds: She is older here.", writer)
        self.assertIn("Name a referenced character exactly as its reference", writer)
        for leaked in ("character-01", "location-01", "Do not copy", "Keep the face"):
            self.assertNotIn(leaked, writer)
        story = studio._reference_note([{**self.LENA, "wardrobe": False}], story=True)
        self.assertIn("Describe the wardrobe in the lock.", story)

    def test_art_director_still_gets_ids_and_image_directions(self):
        from tests.fixtures import _piece
        development, screenplay = _piece()
        art = studio.art_user_message(development, screenplay, [self.LENA, self.LAKE])
        self.assertIn("character-01 (Lena)", art)
        self.assertIn(reference_direction(self.LENA), art)
        self.assertNotIn("Name a referenced character exactly", art)
