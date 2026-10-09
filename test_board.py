"""Board moves. No API calls, no desk server."""

import json
import unittest

from board import Board, apply_chunk, client_payload, scrub, token_glyph, token_station


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
        apply_chunk(board, "**Development** is locking the logline and the beats.\n\n")
        self.assertEqual(board.active, "development")
        self.assertEqual(token_station(board), "development")

    def test_editor_return_and_stills(self):
        board = Board()
        apply_chunk(board, "**Development** is locking the logline and the beats.\n\n")
        apply_chunk(board, "**Screenwriter** is writing the pages.\n\n")
        self.assertEqual(token_station(board), "writer")
        self.assertIn("development", board.done)
        self.assertEqual(token_glyph(board), "page")

        apply_chunk(board, "**Script editor** is reading draft 1.\n\n")
        self.assertEqual(board.active, "editor")
        self.assertFalse(board.returning)

        apply_chunk(board, "The editor sent it back:\n- Hook is late.\n\n")
        self.assertTrue(board.returning)
        self.assertEqual(token_station(board), "writer")
        self.assertEqual(token_glyph(board), "page")
        self.assertEqual(board.note, "The editor sent the draft back.")

        apply_chunk(board, "**Screenwriter** is rewriting from the editor's notes.\n\n")
        self.assertEqual(board.active, "writer")
        self.assertTrue(board.returning)

        apply_chunk(board, "The editor passed the draft.\n\n")
        self.assertFalse(board.returning)
        self.assertEqual(token_station(board), "art")
        self.assertEqual(token_glyph(board), "shot")

        apply_chunk(board, "**Art director** is writing the still jobs.\n\n")
        self.assertEqual(board.active, "art")

        apply_chunk(board, "**Stills** is calling KIE.ai.\n\n")
        self.assertEqual(token_station(board), "images")
        self.assertEqual(token_glyph(board), "frame")

        apply_chunk(board, "**Still** maya (character) started on nano-banana-2-1.\n\n")
        self.assertEqual(board.images[0]["state"], "running")
        self.assertEqual(board.images[0]["id"], "maya")

        apply_chunk(
            board,
            "- maya (character) — nano-banana-2-1 — `productions/hall/images/maya.png`\n\n",
        )
        self.assertEqual(len(board.images), 1)
        self.assertEqual(board.images[0]["state"], "done")
        self.assertEqual(board.images[0]["path"], "productions/hall/images/maya.png")

        apply_chunk(board, "- s01 (keyframe) failed: prompt is empty\n\n")
        self.assertEqual(board.images[-1]["state"], "failed")
        self.assertEqual(board.images[-1]["error"], "prompt is empty")

    def test_missing_key_stops_the_board(self):
        board = Board()
        apply_chunk(board, "KIE_API_KEY is not set. Add it to `.env` in the repo root.\n")
        self.assertIsNone(board.active)
        self.assertEqual(board.note, "KIE_API_KEY is not set.")
        self.assertEqual(token_station(board), "brief")


if __name__ == "__main__":
    unittest.main()
