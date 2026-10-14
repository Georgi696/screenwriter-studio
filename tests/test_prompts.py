"""Prompt size on the editor loop. No API calls."""

import asyncio
import json
import unittest

from screenwriter_studio.budget import shot_budget
from screenwriter_studio.crew.art import art_agent
from screenwriter_studio.crew.writer import writer_agent
from screenwriter_studio.playbooks import CAMERA_COVERAGE, PLAYBOOKS
from screenwriter_studio.studio import art_user_message, editor_user_message, writer_user_message


from tests.fixtures import _piece


class WriterPromptTest(unittest.TestCase):
    def test_first_draft_still_carries_the_lock_and_the_playbook(self):
        development, screenplay = _piece()
        budget = shot_budget(development.runtime_seconds)
        expected = "\n\n".join(
            [
                "Locked development. Do not renegotiate it.",
                development.model_dump_json(indent=2),
                (
                    f"Shot budget: {budget} shots maximum for {development.runtime_seconds} seconds "
                    f"(a configurable still-generation limit). Each shot becomes one generated image. "
                    f"Do not write more than {budget} shots. "
                    "Each clip is 3–30 seconds. Clips longer than 15 seconds use Seedance 2.5. "
                    "Vary the cameras on those shots. Do not add shots to get a new angle."
                ),
                "Playbook:\n" + PLAYBOOKS["narrative"],
                CAMERA_COVERAGE,
            ]
        )
        self.assertEqual(writer_user_message(development, None, None), expected)
        self.assertEqual(writer_user_message(development, screenplay, []), expected)

    def test_rewrite_drops_the_playbook_and_the_style_bible(self):
        development, screenplay = _piece()
        message = writer_user_message(development, screenplay, ["Hook is late."])
        self.assertNotIn("Playbook:", message)
        self.assertNotIn("One dramatic question", message)
        self.assertNotIn("one window", message)
        self.assertNotIn("short black hair", message)
        self.assertIn("Hook is late.", message)
        self.assertIn("Maya waits.", message)
        self.assertIn('"shot_budget":3', message)
        self.assertIn(development.logline, message)
        self.assertIn("Consecutive shots must not share the same camera height, angle, and subject distance.", message)
        self.assertLess(len(message), len(writer_user_message(development, None, None)))


class CameraCoverageTest(unittest.TestCase):
    def test_writer_and_art_prompts_require_angle_variety(self):
        development, screenplay = _piece()
        required = (
            "Consecutive shots must not share the same camera height, angle, and subject distance.",
            "wide, medium, and close",
            "high, eye, and low",
            "different axis (side, rear, overhead, or ground)",
            "a low angle into a charge, a high angle on a collapse, a tight insert, an over-shoulder",
            "Cinematic or dynamic alone is not a camera.",
            "Do not add shots.",
            "Camera variety is framing, not extra action inside the shot.",
        )
        messages = (
            writer_agent.instructions,
            writer_user_message(development, None, None),
            writer_user_message(development, screenplay, ["Hook is late."]),
            art_agent.instructions,
            art_user_message(development, screenplay),
        )
        for message in messages:
            for phrase in required:
                self.assertIn(phrase, message)
        art_message = art_user_message(development, screenplay)
        self.assertIn("concrete camera angle, height, and subject distance", art_message)
        self.assertIn("Do not add keyframes.", art_message)


class EditorPromptTest(unittest.TestCase):
    def test_checklist_json_is_compact_and_complete(self):
        development, screenplay = _piece()
        message = editor_user_message(development, screenplay)
        raw = message.split("\n\n", 1)[1]
        payload = json.loads(raw)
        self.assertEqual(payload["logline"], development.logline)
        self.assertEqual(payload["shot_budget"], 3)
        self.assertEqual(payload["fountain"], screenplay.fountain)
        self.assertLess(len(raw), len(json.dumps(payload, indent=2)))
        self.assertNotIn("\n  ", raw)


class MissingKeyTest(unittest.TestCase):
    def test_run_stops_before_any_model_call(self):
        import screenwriter_studio.studio as studio

        original = studio.load_kie_api_key
        studio.load_kie_api_key = lambda: ""

        async def collect():
            lines = []
            async for chunk in studio.ScreenwriterStudio().run("a quiet hallway"):
                lines.append(chunk)
            return lines

        try:
            lines = asyncio.run(collect())
        finally:
            studio.load_kie_api_key = original
        self.assertEqual(len(lines), 1)
        self.assertIn("KIE_API_KEY is not set", lines[0].text)


class EditorConvergenceTest(unittest.TestCase):
    def test_writer_and_editor_share_one_shot_rule_and_editor_sees_previous_issues(self):
        from screenwriter_studio.crew.editor import editor_agent
        from screenwriter_studio.playbooks import SHOT_TAKE_RULE
        development, screenplay = _piece()
        self.assertIn(SHOT_TAKE_RULE, writer_agent.instructions)
        self.assertIn(SHOT_TAKE_RULE, editor_agent.instructions)
        self.assertNotIn("previous_issues", editor_user_message(development, screenplay))
        message = editor_user_message(development, screenplay, ["Shot 2 jumps location."])
        self.assertIn('"previous_issues":["Shot 2 jumps location."]', message)
        self.assertIn("previous_issues", editor_agent.instructions)

    def test_rewrite_loop_hands_each_verdict_to_the_next_read(self):
        import asyncio
        from unittest.mock import patch
        from screenwriter_studio.schemas import ScriptVerdict
        from screenwriter_studio.studio import ScreenwriterStudio
        development, screenplay = _piece()
        seen = []

        async def fake_edit(self, dev, play, previous=None):
            seen.append(previous)
            return ScriptVerdict(passed=len(seen) == 3, issues=[] if len(seen) == 3 else [f"issue {len(seen)}"])

        async def fake_write(self, *args, **kwargs):
            return screenplay

        async def fake_develop(self, *args, **kwargs):
            return development

        async def collect():
            return [e async for e in ScreenwriterStudio().run("idea", pages_only=True)]

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as root, \
                patch("screenwriter_studio.studio.WORKSPACE_ROOT", pathlib.Path(root)), \
                patch("screenwriter_studio.studio._project_dir", lambda slug: pathlib.Path(root) / "film"), \
                patch("screenwriter_studio.studio.load_kie_api_key", return_value="key"), \
                patch.object(ScreenwriterStudio, "_develop", fake_develop), \
                patch.object(ScreenwriterStudio, "_write", fake_write), \
                patch.object(ScreenwriterStudio, "_edit", fake_edit):
            events = asyncio.run(collect())
        self.assertEqual(seen, [None, ["issue 1"], ["issue 2"]])
        self.assertIn("approved", [e.kind for e in events])


if __name__ == "__main__":
    unittest.main()
