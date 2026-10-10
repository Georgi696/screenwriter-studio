"""Shot and still budgets. No API calls."""

import unittest

from screenwriter_studio.budget import select_stills, shot_budget


class ShotBudgetTest(unittest.TestCase):
    def test_sixty_seconds_is_seven_shots(self):
        self.assertEqual(shot_budget(60), 7)

    def test_thirty_seconds_is_three_shots(self):
        self.assertEqual(shot_budget(30), 3)

    def test_short_piece_is_one_shot(self):
        self.assertEqual(shot_budget(6), 1)
        self.assertEqual(shot_budget(15), 1)

    def test_custom_limit_and_feasibility(self):
        self.assertEqual(shot_budget(30, 6), 6)
        self.assertEqual(shot_budget(16), 2)
        with self.assertRaises(ValueError):
            shot_budget(30, 1)
        with self.assertRaises(ValueError):
            shot_budget(30, 11)


class StillLimitTest(unittest.TestCase):
    def test_sixty_second_package_drops_extra_keyframes(self):
        jobs = [
            ("emil", "character"),
            ("lukas", "character"),
            ("passage", "location"),
            ("street", "location"),
            *[(f"s{number:02d}", "keyframe") for number in range(1, 16)],
            ("s01-end", "edit"),
        ]
        kept = select_stills(jobs, runtime_seconds=60, character_count=2, location_count=2)
        kinds = [kind for _job_id, kind in kept]
        self.assertEqual(kinds.count("character"), 2)
        self.assertEqual(kinds.count("location"), 2)
        self.assertEqual(kinds.count("keyframe"), 7)
        self.assertNotIn("edit", kinds)
        self.assertEqual([job_id for job_id, kind in kept if kind == "keyframe"][-1], "s07")

    def test_under_budget_is_unchanged(self):
        jobs = [("maya", "character"), ("s01", "keyframe")]
        kept = select_stills(jobs, runtime_seconds=60, character_count=1, location_count=0)
        self.assertEqual(kept, jobs)

    def test_duplicate_ids_count_once(self):
        jobs = [("s01", "keyframe"), ("s01", "keyframe"), ("s02", "scene")]
        kept = select_stills(jobs, runtime_seconds=30, character_count=0, location_count=0)
        self.assertEqual(kept, [("s01", "keyframe"), ("s02", "scene")])


if __name__ == "__main__":
    unittest.main()
