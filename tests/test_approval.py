"""Approval gate, style references, and the per-station detail. No network."""

import asyncio
import json
import tempfile
import unittest
from screenwriter_studio.web import library
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from screenwriter_studio.events import StudioEvent
from screenwriter_studio.images.service import apply_style_references
from screenwriter_studio.schemas import ScriptVerdict, StillJob, StillPackage
from screenwriter_studio.studio import ScreenwriterStudio, art_user_message, writer_user_message
from screenwriter_studio.web.board import Board, append_log, apply_event, board_from, event_station, token_station
from tests.fixtures import _piece

PNG = b"\x89PNG\r\n\x1a\n" + b"still"


class ApprovalGateTest(unittest.TestCase):
    def test_passed_pages_wait_for_approval_and_proceed_keeps_the_run_settings(self):
        development, screenplay = _piece()
        studio = ScreenwriterStudio()
        studio._develop = AsyncMock(return_value=development)
        studio._write = AsyncMock(return_value=screenplay)
        studio._edit = AsyncMock(return_value=ScriptVerdict(passed=True, issues=[]))
        studio._art = AsyncMock()
        produced = {}

        async def fake_produce(*args, **kwargs):
            produced.update(kwargs)
            return
            yield

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "productions" / "old" / "images"
            source.mkdir(parents=True)
            (source / "a.png").write_bytes(PNG)

            async def run():
                return [event async for event in studio.run(
                    "hall", approval=True, image_count=3,
                    references=[{"path": "old/images/a.png", "mode": "style"}],
                )]

            async def proceed(folder):
                return [event async for event in studio.proceed(folder, dry_run=True, brief="hall")]

            with mock.patch("screenwriter_studio.studio.WORKSPACE_ROOT", root), \
                    mock.patch("screenwriter_studio.studio.load_kie_api_key", return_value="fake"):
                events = asyncio.run(run())
                folder = root / "productions" / "hall"
                settings = json.loads((folder / "run_settings.json").read_text())
                studio._produce = fake_produce
                resumed = asyncio.run(proceed(folder))

        self.assertEqual(events[-1].kind, "awaiting")
        studio._art.assert_not_awaited()
        self.assertEqual(settings["image_count"], 3)
        self.assertEqual([(item["id"], item["mode"]) for item in settings["references"]], [("style-01", "style")])
        self.assertEqual([event.kind for event in resumed], ["signed_off"])
        self.assertEqual(produced["image_count"], 3)
        self.assertEqual([item["mode"] for item in produced["references"]], ["style"])

    def test_board_moves_through_the_approval_station(self):
        board = Board()
        apply_event(board, StudioEvent("approved", "Passed"))
        apply_event(board, StudioEvent("awaiting", "Waiting for you"))
        self.assertEqual(token_station(board), "approval")
        self.assertNotIn("approval", board.done)
        self.assertEqual(event_station(board, StudioEvent("signed_off", "")), "approval")
        apply_event(board, StudioEvent("signed_off", "You approved"))
        self.assertIn("approval", board.done)
        self.assertEqual(token_station(board), "art")

        overridden = Board()
        apply_event(overridden, StudioEvent("greenlit", "Proceeding"))
        self.assertIn("approval", overridden.done)
        self.assertEqual(overridden.overridden, {"editor"})

    def test_log_lines_keep_their_station_and_old_snapshots_pad(self):
        board = Board()
        failure = StudioEvent("failed", "Stopped")
        board.active = "writer"
        append_log(board, event_station(board, failure), "Stopped")
        append_log(board, event_station(board, StudioEvent("art", "")), "Art")
        self.assertEqual(board.log_stations, ["writer", "art"])
        restored = board_from({"log": ["a", "b"], "log_stations": ["development"]})
        self.assertEqual(restored.log_stations, ["development", ""])


class StyleReferenceTest(unittest.TestCase):
    def test_style_goes_last_on_keyframes_and_never_on_characters(self):
        jobs = [
            {"id": "maya", "kind": "character", "prompt": "Maya on grey.", "references": ["style-01"]},
            {"id": "hall", "kind": "location", "prompt": "The hall.", "references": []},
            {"id": "s01", "kind": "keyframe", "prompt": "Maya waits.", "references": ["style-01", "maya", "ref-01"]},
        ]
        styled = {job["id"]: job for job in apply_style_references(jobs, ["style-01"], "16:9", "1K")}
        self.assertEqual(styled["maya"]["references"], [])
        self.assertNotIn("Style reference", styled["maya"]["prompt"])
        self.assertEqual(styled["hall"]["references"], ["style-01"])
        self.assertEqual(styled["s01"]["references"], ["maya", "ref-01", "style-01"])
        self.assertIn("the last reference image sets the look only", styled["s01"]["prompt"])
        self.assertEqual(jobs[2]["references"], ["style-01", "maya", "ref-01"])

    def test_prompts_separate_locked_subjects_from_style(self):
        development, screenplay = _piece()
        locked = [{"id": "ref-01", "name": "maya.png", "mode": "lock"}]
        style = [{"id": "style-01", "name": "grade.png", "mode": "style"}]
        art_locked = art_user_message(development, screenplay, locked)
        self.assertIn("only to the stills whose shot shows that person or place", art_locked)
        self.assertNotIn("Style references", art_locked)
        art_style = art_user_message(development, screenplay, style)
        self.assertIn("Style references. They set the look only", art_style)
        self.assertNotIn("different plot", art_style)
        writer = writer_user_message(development, None, None, style)
        self.assertNotIn("Keep the locked faces", writer)
        self.assertIn("Style references", writer)


class StageDetailTest(unittest.TestCase):
    def test_stage_detail_reads_each_station_from_the_folder(self):
        import screenwriter_studio.web.server as desk

        development, screenplay = _piece()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "productions" / "hall"
            (folder / "drafts").mkdir(parents=True)
            (folder / "00_development.json").write_text(development.model_dump_json(), encoding="utf-8")
            (folder / "01_screenplay.json").write_text(screenplay.model_dump_json(), encoding="utf-8")
            (folder / "drafts" / "01_screenplay.json").write_text(screenplay.model_dump_json(), encoding="utf-8")
            (folder / "drafts" / "01_verdict.json").write_text(
                ScriptVerdict(passed=False, issues=["Shot 1 is long."]).model_dump_json(), encoding="utf-8")
            (folder / "drafts" / "02_verdict.json").write_text(
                ScriptVerdict(passed=True, issues=[]).model_dump_json(), encoding="utf-8")
            (folder / "verdict.json").write_text(ScriptVerdict(passed=True, issues=[]).model_dump_json(), encoding="utf-8")
            plan = StillPackage(aspect_ratio="16:9", resolution="1K", jobs=[
                StillJob(id="s01", kind="keyframe", prompt="Maya waits.", references=["style-01"]),
            ])
            (folder / "03_still_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
            (folder / "run_settings.json").write_text(json.dumps(
                {"image_count": None, "references": [{"id": "style-01", "name": "a.png", "mode": "style", "path": "x"}]}
            ), encoding="utf-8")
            board = Board()
            board.folder = "productions/hall"
            board.brief = "A hall at night."
            with mock.patch.object(library, "PRODUCTIONS", root / "productions"), \
                    mock.patch.object(desk, "WORKSPACE_ROOT", root), mock.patch.object(library, "WORKSPACE_ROOT", root), \
                    mock.patch.object(desk, "_board", board):
                details = {station: desk.stage_detail(station) for station in desk.STAGES}
                with self.assertRaises(ValueError):
                    desk.stage_detail("../etc")

        self.assertEqual(details["brief"]["data"]["brief"], "A hall at night.")
        self.assertEqual(details["brief"]["data"]["references"][0]["mode"], "style")
        self.assertEqual(details["development"]["data"]["title"], "Hall")
        self.assertEqual(details["writer"]["data"]["shots"][0]["number"], 1)
        self.assertEqual(details["writer"]["files"], [{"path": "hall/drafts/01_screenplay.json", "name": "Draft 1"}])
        self.assertEqual([(v["draft"], v["passed"]) for v in details["editor"]["data"]["verdicts"]], [(1, False), (2, True)])
        self.assertTrue(details["approval"]["data"]["passed"])
        self.assertTrue(details["approval"]["data"]["waiting"])
        self.assertEqual(details["art"]["data"]["jobs"][0]["references"], [{"id": "style-01", "mode": "style"}])
        for detail in details.values():
            for file in detail["files"]:
                self.assertFalse(file["path"].startswith("/"))


if __name__ == "__main__":
    unittest.main()
