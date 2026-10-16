"""Aborting a saved draft hides Proceed. Pages stay on disk. No crew call."""

import json
import tempfile
import threading
import unittest
from screenwriter_studio.web import library
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from screenwriter_studio.schemas import ScriptVerdict
from tests.fixtures import _piece


class AbortDraftTest(unittest.TestCase):
    def setUp(self):
        from screenwriter_studio.web.board import Board
        from screenwriter_studio.web.server import Hub, bind

        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for replacement in [
            mock.patch("screenwriter_studio.web.board.STATUS_PATH", Path(state.name) / "desk_status.json"),
            mock.patch("screenwriter_studio.web.server._board", Board()),
            mock.patch("screenwriter_studio.web.server.hub", Hub()),
        ]:
            replacement.start()
            self.addCleanup(replacement.stop)

        self.httpd = bind(0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)

    def test_abort_marker_blocks_proceed_and_cross_origin_skips_handler(self):
        import screenwriter_studio.web.server as desk

        development, screenplay = _piece()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "productions" / "hall"
            folder.mkdir(parents=True)
            pages = {
                "00_development.json": development.model_dump_json(),
                "01_screenplay.json": screenplay.model_dump_json(),
                "verdict.json": ScriptVerdict(passed=False, issues=["Shot 1 has two actions."]).model_dump_json(),
            }
            for name, text in pages.items():
                (folder / name).write_text(text, encoding="utf-8")
            with mock.patch.object(library, "PRODUCTIONS", root / "productions"), mock.patch.object(desk, "WORKSPACE_ROOT", root), mock.patch.object(library, "WORKSPACE_ROOT", root):
                self.assertTrue(desk.can_proceed("productions/hall"))
                desk._board.folder = "productions/hall"
                desk.abort_draft()
                self.assertEqual(json.loads((folder / "aborted.json").read_text(encoding="utf-8")), {"aborted": True})
                self.assertFalse(desk.can_proceed("productions/hall"))
                for name, text in pages.items():
                    self.assertEqual((folder / name).read_text(encoding="utf-8"), text)
                self.assertEqual(desk._board.folder, "productions/hall")
                self.assertEqual(desk._board.note, desk._ABORT_NOTE)

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/abort-draft",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
        )
        with mock.patch("screenwriter_studio.web.server.abort_draft") as abort:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 403)
            abort.assert_not_called()
