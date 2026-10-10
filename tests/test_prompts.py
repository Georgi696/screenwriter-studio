"""Prompt size on the editor loop. No API calls."""

import asyncio
import json
import unittest

from screenwriter_studio.budget import shot_budget
from screenwriter_studio.playbooks import PLAYBOOKS
from screenwriter_studio.studio import editor_user_message, writer_user_message


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
                    f"Do not write more than {budget} shots."
                ),
                "Playbook:\n" + PLAYBOOKS["narrative"],
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
        self.assertLess(len(message), len(writer_user_message(development, None, None)))


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


if __name__ == "__main__":
    unittest.main()
