"""Opening saved productions, the run guard, friendly errors and the bounded log."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from screenwriter_studio.errors import friendly
from screenwriter_studio.paths import WORKSPACE_ROOT
from screenwriter_studio.web import server
from screenwriter_studio.web.board import MAX_LOG_LINES, Board, append_log, board_from_folder


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class OpenProductionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.film = self.root / "film"
        _write(self.film / "00_development.json", {"title": "Film"})
        _write(self.film / "01_screenplay.json", {"shots": []})
        _write(self.film / "verdict.json", {"passed": False, "issues": ["x"]})
        for patcher in (
            mock.patch.object(server, "PRODUCTIONS", self.root),
            mock.patch.object(server, "_board", Board()),
            mock.patch("screenwriter_studio.web.board.STATUS_PATH", self.root / "status.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_board_is_rebuilt_from_the_files_on_disk(self):
        board = board_from_folder(self.film, "productions/film")
        self.assertEqual(board.done, {"development", "writer"})
        self.assertIn("script is ready", board.note)
        _write(self.film / "greenlight.json", {"proceeded": True})
        _write(self.film / "images/jobs.json", {"inspection_context": {"brief": "A bus seat."}, "jobs": []})
        _write(self.film / "images/manifest.json", {"images": [{"id": "s01", "path": "x.png"}, {"id": "s02", "error": "boom"}]})
        board = board_from_folder(self.film, "productions/film")
        self.assertEqual(board.overridden, {"editor"})
        self.assertTrue({"approval", "art"} <= board.done)
        self.assertEqual(board.brief, "A bus seat.")
        self.assertEqual([image["state"] for image in board.images], ["done", "failed"])

    def test_open_puts_the_production_on_the_desk_and_refuses_bad_input(self):
        server.open_production("film")
        self.assertTrue(server._board.folder.endswith("film"))
        self.assertEqual(server.production_path(server._board.folder), self.film)
        for slug in ("../etc", "", None, "missing", "a/b"):
            with self.assertRaises(ValueError, msg=slug):
                server.open_production(slug)
        server._board.running = True
        with self.assertRaisesRegex(ValueError, "Stop the current run"):
            server.open_production("film")

    def test_a_new_run_cannot_replace_one_in_progress(self):
        server._board.running = True
        with mock.patch.object(server, "_ensure_loop"), mock.patch.object(server, "_commit_future") as commit:
            with self.assertRaisesRegex(ValueError, "already in progress"):
                server.start_crew("idea", False, False)
            commit.assert_not_called()


class FriendlyErrorTest(unittest.TestCase):
    def test_paths_tracebacks_and_secrets_are_removed(self):
        secret = "sk-test-12345678"
        error = FileNotFoundError(2, "No such file", str(WORKSPACE_ROOT / "productions/film/images/jobs.json"))
        self.assertEqual(friendly(error), "A required file is missing: jobs.json.")
        text = friendly(RuntimeError(f"failed at /Users/someone/x/clip.mp4 with key {secret}\nTraceback (most recent call last):"), secret)
        self.assertNotIn("/Users/", text)
        self.assertNotIn(secret, text)
        self.assertIn("clip.mp4", text)
        self.assertNotIn("\n", text)
        long = friendly(ValueError("x" * 1000))
        self.assertLessEqual(len(long), 300)
        self.assertIn("expected format", friendly(ValueError("1 validation error for Shot\nfield required\n  https://errors.pydantic.dev/2/v/missing")))


class LogLimitTest(unittest.TestCase):
    def test_log_keeps_the_newest_lines(self):
        board = Board()
        for number in range(MAX_LOG_LINES + 25):
            append_log(board, "writer", f"line {number}")
        self.assertEqual(len(board.log), MAX_LOG_LINES)
        self.assertEqual(len(board.log_stations), MAX_LOG_LINES)
        self.assertEqual(board.log[-1], f"line {MAX_LOG_LINES + 24}")


if __name__ == "__main__":
    unittest.main()
