"""Run-request validation and the desk's shared status snapshot. No network."""

import json
import unittest
from unittest import mock

from screenwriter_studio.web import server as desk


class RunRequestTest(unittest.TestCase):
    def parse(self, **body):
        return desk.parse_run_request(body)

    def test_a_plain_idea_becomes_start_crew_arguments(self):
        request = self.parse(idea="  A bus.  ")
        self.assertEqual(request, {"idea": "A bus.", "pages_only": False, "dry_run": False,
                                   "max_shots": None, "inspection_mode": "manual"})

    def test_optional_locks_are_passed_only_when_set(self):
        request = self.parse(idea="x", runtime_seconds=30, aspect_ratio="9:16", max_shots=4, approval=True, image_count=9)
        self.assertEqual((request["runtime_seconds"], request["aspect_ratio"], request["max_shots"]), (30, "9:16", 4))
        self.assertEqual((request["approval"], request["image_count"]), (True, 9))
        self.assertNotIn("references", request)

    def test_each_bad_field_names_itself(self):
        cases = [
            ({"idea": "x", "max_shots": 0}, "Shot limit"),
            ({"idea": "x", "max_shots": True}, "Shot limit"),
            ({"idea": "x", "runtime_seconds": 5}, "Runtime"),
            ({"idea": "x", "aspect_ratio": "4:3"}, "Aspect ratio"),
            ({"idea": "x", "inspection_mode": "never"}, "Inspection mode"),
            ({"idea": "x", "image_count": 0}, "Image count"),
            ({"idea": "   "}, "Write an idea"),
            ({}, "Write an idea"),
        ]
        for body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, message):
                desk.parse_run_request(body)

    def test_every_post_route_has_a_handler(self):
        for path, (name, body) in desk.DeskHandler._POST_ROUTES.items():
            with self.subTest(path=path):
                self.assertTrue(callable(getattr(desk.DeskHandler, name)))
                self.assertIn(body, {None, "json", "upload"})


class SharedSnapshotTest(unittest.TestCase):
    def test_pages_on_the_same_revision_share_one_serialized_view(self):
        with mock.patch.object(desk, "public_view", return_value={"n": 1}) as build, \
                mock.patch.object(desk, "_view_cache", (-1, b"")):
            first = desk.view_bytes(41)
            second = desk.view_bytes(41)
            self.assertEqual(json.loads(first), {"n": 1})
            self.assertIs(first, second)
            build.assert_called_once()
            desk.view_bytes(42)
            self.assertEqual(build.call_count, 2)


if __name__ == "__main__":
    unittest.main()
