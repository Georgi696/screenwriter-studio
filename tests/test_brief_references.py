"""Brief-tab image count and reference ids on POST /api/run. No crew run."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from screenwriter_studio.web.board import Board
from screenwriter_studio.web.server import Hub, bind


class BriefRunTest(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        image = Path(root.name) / "productions" / "hall" / "images"
        image.mkdir(parents=True)
        (image / "s01.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"still")
        for replacement in [
            mock.patch("screenwriter_studio.web.board.STATUS_PATH", Path(state.name) / "desk_status.json"),
            mock.patch("screenwriter_studio.web.server._board", Board()),
            mock.patch("screenwriter_studio.web.server.hub", Hub()),
            mock.patch("screenwriter_studio.web.server.PRODUCTIONS", Path(root.name) / "productions"),
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

    def _post_json(self, payload: dict) -> bytes:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/run",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as res:
            return res.read()

    def test_run_rejects_bad_image_count_and_passes_a_valid_count_with_reference(self):
        for value in (True, 0, 241, 1.5, "4"):
            with mock.patch("screenwriter_studio.web.server.start_crew") as start:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json({"idea": "test", "image_count": value})
                self.assertEqual(caught.exception.code, 400)
                start.assert_not_called()
        with mock.patch("screenwriter_studio.web.server.start_crew") as start:
            self._post_json({"idea": "test", "image_count": 4, "references": ["hall/images/s01.png"]})
            start.assert_called_once_with(
                "test", False, False, None, "manual", image_count=4,
                references=[{"path": "hall/images/s01.png", "mode": "lock"}],
            )

    def test_run_passes_reference_modes_and_the_approval_gate(self):
        with mock.patch("screenwriter_studio.web.server.start_crew") as start:
            self._post_json({
                "idea": "test", "approval": True,
                "references": [{"path": "hall/images/s01.png", "mode": "style"}],
            })
            start.assert_called_once_with(
                "test", False, False, None, "manual",
                references=[{"path": "hall/images/s01.png", "mode": "style"}], approval=True,
            )
        with mock.patch("screenwriter_studio.web.server.start_crew") as start:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post_json({"idea": "test", "references": [{"path": "hall/images/s01.png", "mode": "copy"}]})
            self.assertEqual(caught.exception.code, 400)
            start.assert_not_called()

    def test_run_preserves_reference_role_settings(self):
        reference = {"path": "hall/images/s01.png", "mode": "character", "name": "Maya",
                     "scope": "shots", "shots": [1, 3], "face": True, "wardrobe": False,
                     "instruction": "Keep the freckles."}
        with mock.patch("screenwriter_studio.web.server.start_crew") as start:
            self._post_json({"idea": "test", "references": [reference]})
            self.assertEqual(start.call_args.kwargs["references"], [reference])
        for extra in ({"shots": [True]}, {"face": "false"}, {"scope": "shots", "shots": []}):
            with mock.patch("screenwriter_studio.web.server.start_crew") as start:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json({"idea": "test", "references": [{**reference, **extra}]})
                self.assertEqual(caught.exception.code, 400)
                start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
