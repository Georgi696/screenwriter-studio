"""A story run that stopped at the screenwriter can be retried without paying for development again."""

import asyncio
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, Mock, patch

from screenwriter_studio import studio as studio_module
from screenwriter_studio.schemas import ScriptVerdict
from screenwriter_studio.studio import ScreenwriterStudio, story_unfinished
from screenwriter_studio.web import library
from tests.fixtures import _piece


class BadGateway(Exception):
    status_code = 502


def _result(value):
    result = Mock()
    result.final_output_as.return_value = value
    return result


def _folder(root: Path, *, screenplay=False, verdict=False, flow=None):
    development, piece = _piece()
    development.runtime_seconds = 8
    folder = root / "productions" / "hall"
    folder.mkdir(parents=True)
    (folder / "00_development.json").write_text(development.model_dump_json(), encoding="utf-8")
    (folder / "run_settings.json").write_text(
        json.dumps({"image_count": None, "references": [], "brief": "a hall", "flow": flow or {}}), encoding="utf-8")
    if screenplay:
        (folder / "01_screenplay.json").write_text(piece.model_dump_json(), encoding="utf-8")
    if verdict:
        (folder / "verdict.json").write_text(ScriptVerdict(passed=True, issues=[]).model_dump_json(), encoding="utf-8")
    return folder, development, piece


class ResumeTest(unittest.TestCase):
    def collect(self, studio, folder):
        async def go():
            return [event async for event in studio.resume(folder)]
        with patch("screenwriter_studio.studio.WORKSPACE_ROOT", folder.parent.parent), \
                patch("screenwriter_studio.studio.load_kie_api_key", return_value="fake"):
            return asyncio.run(go())

    def test_it_writes_the_pages_from_the_saved_development_and_pauses_for_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, development, piece = _folder(Path(temp), flow={"approval": True})
            studio = ScreenwriterStudio()
            studio._develop = AsyncMock()
            studio._write = AsyncMock(return_value=piece)
            studio._edit = AsyncMock(return_value=ScriptVerdict(passed=True, issues=[]))
            events = self.collect(studio, folder)
            self.assertTrue((folder / "01_script.fountain").is_file())
            self.assertTrue(json.loads((folder / "verdict.json").read_text())["passed"])
            self.assertFalse(story_unfinished(folder))
        studio._develop.assert_not_awaited()
        studio._write.assert_awaited_once()
        self.assertEqual(events[-1].kind, "awaiting")

    def test_a_saved_draft_is_read_by_the_editor_without_another_write(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, development, piece = _folder(Path(temp), screenplay=True, flow={"pages_only": True})
            (folder / "drafts").mkdir()
            (folder / "drafts" / "01_screenplay.json").write_text("{}", encoding="utf-8")
            studio = ScreenwriterStudio()
            studio._write = AsyncMock()
            studio._edit = AsyncMock(return_value=ScriptVerdict(passed=True, issues=[]))
            events = self.collect(studio, folder)
            self.assertTrue((folder / "drafts" / "02_screenplay.json").is_file(), "numbering continues after the old draft")
        studio._write.assert_not_awaited()
        self.assertEqual(events[-1].kind, "complete")

    def test_finished_pages_or_a_missing_development_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, *_ = _folder(Path(temp), screenplay=True, verdict=True)
            self.assertFalse(story_unfinished(folder))
            with self.assertRaisesRegex(ValueError, "nothing to retry"):
                self.collect(ScreenwriterStudio(), folder)
            (folder / "verdict.json").unlink()
            self.assertTrue(story_unfinished(folder))
            (folder / "aborted.json").write_text("{}", encoding="utf-8")
            self.assertFalse(story_unfinished(folder))
            (folder / "00_development.json").unlink()
            self.assertFalse(story_unfinished(folder))

    def test_a_run_remembers_how_it_started(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, *_ = _folder(Path(temp), flow={"pages_only": True, "approval": False, "inspection_mode": "auto"})
            self.assertEqual(studio_module._saved_flow(folder), {
                "pages_only": True, "approval": False, "dry_run": False, "inspection_mode": "auto"})
            (folder / "run_settings.json").write_text("not json", encoding="utf-8")
            self.assertEqual(studio_module._saved_flow(folder)["approval"], True)


class OutageRetryTest(unittest.TestCase):
    def run_edit(self, side_effect, waits=(0, 0, 0)):
        development, piece = _piece()
        development.runtime_seconds = 8
        run = AsyncMock(side_effect=side_effect)
        studio = ScreenwriterStudio()
        with patch("screenwriter_studio.studio.Runner.run", new=run), patch.object(studio_module, "OUTAGE_WAITS", waits):
            try:
                return asyncio.run(studio._edit(development, piece)), run, studio
            except Exception as exc:
                return exc, run, studio

    def test_a_gateway_error_is_waited_out_and_asked_again(self):
        good = _result(ScriptVerdict(passed=True, issues=[]))
        verdict, run, studio = self.run_edit([BadGateway("Error code: 502"), BadGateway("Error code: 502"), good])
        self.assertTrue(verdict.passed)
        self.assertEqual(run.await_count, 3)
        self.assertEqual([entry["outcome"] for entry in studio._pending], ["error", "error", "ok"])

    def test_a_lasting_outage_surfaces_after_the_waits_run_out(self):
        error, run, _ = self.run_edit(BadGateway("Error code: 502"))
        self.assertIsInstance(error, BadGateway)
        self.assertEqual(run.await_count, 4)

    def test_other_errors_are_not_retried(self):
        error, run, _ = self.run_edit(ValueError("bad key"))
        self.assertIsInstance(error, ValueError)
        self.assertEqual(run.await_count, 1)


class ResumeEndpointTest(unittest.TestCase):
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
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, path):
        request = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_address[1]}{path}", data=b"{}",
                                         method="POST", headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(request, timeout=5)

    def test_the_desk_offers_and_starts_a_retry(self):
        import screenwriter_studio.web.server as desk
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder, *_ = _folder(root)
            patches = [mock.patch.object(library, "PRODUCTIONS", root / "productions"),
                       mock.patch.object(desk, "WORKSPACE_ROOT", root), mock.patch.object(library, "WORKSPACE_ROOT", root),
                       mock.patch.object(desk, "_ensure_loop"), mock.patch.object(desk, "_commit_future"),
                       mock.patch.object(desk, "_drive_resume", new=Mock(return_value=None))]
            for patcher in patches:
                patcher.start()
                self.addCleanup(patcher.stop)
            desk._board.folder = "productions/hall"
            self.assertTrue(desk.public_view()["can_resume"])
            self.assertEqual(self.post("/api/resume").status, 202)
            self.assertTrue(desk._board.running)
            desk._commit_future.assert_called_once()
            self.assertFalse(desk.public_view()["can_resume"], "not offered while it runs")
            desk._board.running = False
            (folder / "01_screenplay.json").write_text(_piece()[1].model_dump_json(), encoding="utf-8")
            (folder / "verdict.json").write_text(ScriptVerdict(passed=True, issues=[]).model_dump_json(), encoding="utf-8")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("/api/resume")
            self.assertEqual(caught.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
