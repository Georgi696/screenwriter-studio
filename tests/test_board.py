"""Board moves. No API calls, no desk server."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from screenwriter_studio.events import StudioEvent

from screenwriter_studio.web.board import Board, apply_event, client_payload, scrub, token_glyph, token_station


class SessionLocationTest(unittest.TestCase):
    def test_legacy_snapshot_is_read_and_next_save_uses_state_directory(self):
        from screenwriter_studio.web import board

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "desk_status.json"
            old.write_text(json.dumps({"brief": "Last train", "revision": 7}))
            new = root / ".state/desk_status.json"
            with patch.object(board, "PROJECT_ROOT", root), patch.object(board, "STATUS_PATH", new):
                restored = board.board_from(board.load_status())
                self.assertEqual(restored.brief, "Last train")
                board.save_status(restored)
                self.assertEqual(board.load_status()["revision"], 8)
                self.assertTrue(new.is_file())
                self.assertEqual(json.loads(old.read_text())["revision"], 7)


class ScrubTest(unittest.TestCase):
    def test_key_is_redacted(self):
        board = Board()
        board.note = "token sk-test-secret-value leaked"
        board.log = ["see sk-test-secret-value"]
        board.images = [
            {
                "id": "s01",
                "kind": "keyframe",
                "model": "nano",
                "path": "/tmp/secret/s01.png",
                "error": "sk-test-secret-value",
                "state": "failed",
            }
        ]
        payload = client_payload(board, "sk-test-secret-value")
        blob = json.dumps(payload)
        self.assertNotIn("sk-test-secret-value", blob)
        self.assertNotIn("/tmp/secret", blob)
        self.assertNotIn("path", payload["images"][0])
        self.assertIn("[redacted]", payload["note"])

    def test_short_secret_is_left_alone(self):
        self.assertEqual(scrub("a cat", "a"), "a cat")


class TokenTest(unittest.TestCase):
    def test_brief_waits_then_development_takes_it(self):
        board = Board()
        board.edge = "brief-dev"
        self.assertEqual(token_station(board), "brief")
        self.assertEqual(token_glyph(board), "sheet")
        apply_event(board, StudioEvent("development", "Different display wording"))
        self.assertEqual(board.active, "development")
        self.assertEqual(token_station(board), "development")

    def test_editor_return_and_stills(self):
        board = Board()
        apply_event(board, StudioEvent("development", "Different display wording"))
        apply_event(board, StudioEvent("writing", "Writing"))
        self.assertEqual(token_station(board), "writer")
        self.assertIn("development", board.done)
        self.assertEqual(token_glyph(board), "page")

        apply_event(board, StudioEvent("reviewing", "Review"))
        self.assertEqual(board.active, "editor")
        self.assertFalse(board.returning)

        apply_event(board, StudioEvent("rejected", "The editor sent the draft back."))
        self.assertTrue(board.returning)
        self.assertEqual(token_station(board), "writer")
        self.assertEqual(token_glyph(board), "page")
        self.assertEqual(board.note, "The editor sent the draft back.")

        apply_event(board, StudioEvent("rewriting", "Rewrite"))
        self.assertEqual(board.active, "writer")
        self.assertTrue(board.returning)

        apply_event(board, StudioEvent("approved", "Approved"))
        self.assertFalse(board.returning)
        self.assertEqual(token_station(board), "art")
        self.assertEqual(token_glyph(board), "shot")

        apply_event(board, StudioEvent("art", "Art"))
        self.assertEqual(board.active, "art")

        apply_event(board, StudioEvent("stills", "Stills"))
        self.assertEqual(token_station(board), "images")
        self.assertEqual(token_glyph(board), "frame")

        apply_event(board, StudioEvent("image", "Started", image={"id": "maya", "phase": "start"}))
        self.assertEqual(board.images[0]["state"], "running")
        apply_event(board, StudioEvent("image", "Done", image={"id": "maya", "path": "images/maya.png"}))
        self.assertEqual(len(board.images), 1)
        self.assertEqual(board.images[0]["state"], "done")
        apply_event(board, StudioEvent("image", "Failed", image={"id": "s01", "error": "empty"}))
        self.assertEqual(board.images[-1]["state"], "failed")
        apply_event(board, StudioEvent("image", "Preview", image={"id": "s02", "dry_run": True}))
        self.assertEqual(board.images[-1]["state"], "planned")

    def test_missing_key_stops_the_board(self):
        board = Board()
        apply_event(board, StudioEvent("failed", "KIE_API_KEY is not set."))
        self.assertIsNone(board.active)
        self.assertEqual(board.note, "KIE_API_KEY is not set.")
        self.assertEqual(token_station(board), "brief")


if __name__ == "__main__":
    unittest.main()
