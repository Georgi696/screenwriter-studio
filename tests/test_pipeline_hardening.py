"""Crew-call repair, ordering of the editor checks, run log and crash-safe writes. No network."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from agents.exceptions import ModelBehaviorError

from screenwriter_studio import fsio, runlog
from screenwriter_studio.schemas import ScriptVerdict, StillJob, StillPackage
from screenwriter_studio.studio import ScreenwriterStudio, art_user_message
from tests.fixtures import _piece


def _result(value, tokens=(10, 5)):
    result = Mock()
    result.final_output_as.return_value = value
    result.context_wrapper.usage.requests = 1
    result.context_wrapper.usage.input_tokens = tokens[0]
    result.context_wrapper.usage.output_tokens = tokens[1]
    result.context_wrapper.usage.total_tokens = sum(tokens)
    return result


class EditorOrderTest(unittest.TestCase):
    def test_a_draft_that_breaks_a_hard_rule_never_reaches_the_editor_model(self):
        development, screenplay = _piece()
        screenplay.shots *= 2  # duration and numbering are wrong
        run = AsyncMock()
        with patch("screenwriter_studio.studio.Runner.run", new=run):
            verdict = asyncio.run(ScreenwriterStudio()._edit(development, screenplay))
        run.assert_not_awaited()
        self.assertFalse(verdict.passed)
        self.assertTrue(any("unique" in issue for issue in verdict.issues))

    def test_a_clean_draft_is_read_by_the_editor_model(self):
        development, screenplay = _piece()
        development.runtime_seconds = 8
        run = AsyncMock(return_value=_result(ScriptVerdict(passed=True, issues=[])))
        with patch("screenwriter_studio.studio.Runner.run", new=run):
            verdict = asyncio.run(ScreenwriterStudio()._edit(development, screenplay))
        run.assert_awaited_once()
        self.assertTrue(verdict.passed)


class CallRepairTest(unittest.TestCase):
    def test_an_unusable_reply_gets_one_more_try_that_names_the_problem(self):
        development, screenplay = _piece()
        development.runtime_seconds = 8
        good = _result(ScriptVerdict(passed=True, issues=[]))
        run = AsyncMock(side_effect=[ModelBehaviorError("Invalid JSON: missing passed"), good])
        studio = ScreenwriterStudio()
        with patch("screenwriter_studio.studio.Runner.run", new=run):
            verdict = asyncio.run(studio._edit(development, screenplay))
        self.assertTrue(verdict.passed)
        self.assertEqual(run.await_count, 2)
        self.assertIn("missing passed", run.await_args_list[1].args[1])
        outcomes = [(entry["attempt"], entry["outcome"]) for entry in studio._pending]
        self.assertEqual(outcomes, [(1, "invalid_output"), (2, "ok")])

    def test_two_unusable_replies_surface_the_error(self):
        development, screenplay = _piece()
        development.runtime_seconds = 8
        run = AsyncMock(side_effect=ModelBehaviorError("bad"))
        with patch("screenwriter_studio.studio.Runner.run", new=run), self.assertRaises(ModelBehaviorError):
            asyncio.run(ScreenwriterStudio()._edit(development, screenplay))
        self.assertEqual(run.await_count, 2)

    def test_calls_are_logged_with_tokens_once_the_folder_exists(self):
        development, screenplay = _piece()
        development.runtime_seconds = 8
        run = AsyncMock(return_value=_result(ScriptVerdict(passed=True, issues=[]), tokens=(120, 30)))
        studio = ScreenwriterStudio()
        with tempfile.TemporaryDirectory() as temp, patch("screenwriter_studio.studio.Runner.run", new=run):
            asyncio.run(studio._edit(development, screenplay))
            self.assertEqual(len(studio._pending), 1)
            studio._bind(Path(temp))
            self.assertEqual(studio._pending, [])
            totals = runlog.summary(Path(temp))
        self.assertEqual(totals["editor"]["input_tokens"], 120)
        self.assertEqual(totals["editor"]["output_tokens"], 30)
        self.assertEqual(totals["editor"]["failures"], 0)


class ArtRepairTest(unittest.TestCase):
    def piece(self):
        development, screenplay = _piece()
        development.runtime_seconds = 8
        return development, screenplay

    def collect(self, studio, development, screenplay, folder):
        async def go():
            return [event async for event in studio._produce(
                folder, development, screenplay, "hall", dry_run=True, inspection_mode="manual")]
        with patch("screenwriter_studio.studio.render_stills", return_value={"images": [], "status": "complete"}):
            return asyncio.run(go())

    def packages(self):
        bad = StillPackage(aspect_ratio="16:9", resolution="1K", jobs=[StillJob(id="s09", kind="keyframe", prompt="A hall.")])
        good = StillPackage(aspect_ratio="16:9", resolution="1K", jobs=[
            StillJob(id="maya", kind="character", prompt="Maya, 3:4 portrait."),
            StillJob(id="hall", kind="location", prompt="The hall."),
            StillJob(id="s01", kind="keyframe", prompt="Wide shot.", references=["maya", "hall"])])
        return bad, good

    def test_an_invalid_plan_goes_back_to_the_art_director_before_any_image_is_made(self):
        development, screenplay = self.piece()
        bad, good = self.packages()
        studio = ScreenwriterStudio()
        studio._art = AsyncMock(side_effect=[bad, good])
        with tempfile.TemporaryDirectory() as temp:
            events = self.collect(studio, development, screenplay, Path(temp))
            saved = json.loads((Path(temp) / "03_still_plan.json").read_text())
        self.assertEqual(studio._art.await_count, 2)
        self.assertEqual(studio._art.await_args_list[1].kwargs["previous"], bad)
        self.assertTrue(studio._art.await_args_list[1].kwargs["issues"])
        self.assertEqual([job["id"] for job in saved["jobs"]], ["maya", "hall", "s01"])
        self.assertEqual(events[-1].kind, "complete")
        self.assertTrue(any("correcting" in event.text for event in events))

    def test_a_plan_that_stays_invalid_stops_after_one_correction(self):
        development, screenplay = self.piece()
        bad, _ = self.packages()
        studio = ScreenwriterStudio()
        studio._art = AsyncMock(return_value=bad)
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            self.collect(studio, development, screenplay, Path(temp))
        self.assertEqual(studio._art.await_count, 2)

    def test_the_correction_message_carries_the_rejected_plan(self):
        development, screenplay = self.piece()
        bad, _ = self.packages()
        run = AsyncMock(return_value=_result(bad))
        with patch("screenwriter_studio.studio.Runner.run", new=run):
            asyncio.run(ScreenwriterStudio()._art(development, screenplay, previous=bad, issues=["one keyframe per shot"]))
        message = run.await_args.args[1]
        self.assertIn("one keyframe per shot", message)
        self.assertIn('"s09"', message)
        self.assertTrue(message.startswith(art_user_message(development, screenplay)))


class AtomicWriteTest(unittest.TestCase):
    def test_a_failed_write_leaves_the_old_file_and_no_temporary(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "verdict.json"
            fsio.atomic_write_json(path, {"passed": True})
            with patch("screenwriter_studio.fsio.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    fsio.atomic_write_json(path, {"passed": False})
            self.assertEqual(json.loads(path.read_text()), {"passed": True})
            self.assertEqual([p.name for p in Path(temp).iterdir()], ["verdict.json"])

    def test_text_round_trips_with_unicode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "script.fountain"
            fsio.atomic_write_text(path, "Последний поезд\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "Последний поезд\n")


class RunLogTest(unittest.TestCase):
    def test_summary_counts_retries_and_failures_and_skips_garbage_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            runlog.append(folder, {"role": "writer", "model": "m", "attempt": 1, "outcome": "invalid_output", "seconds": 2})
            runlog.append(folder, {"role": "writer", "model": "m", "attempt": 2, "outcome": "ok", "seconds": 3,
                                   "input_tokens": 100, "output_tokens": 40})
            with (folder / runlog.FILE).open("a") as handle:
                handle.write("not json\n")
            writer = runlog.summary(folder)["writer"]
        self.assertEqual((writer["calls"], writer["retries"], writer["failures"]), (2, 1, 1))
        self.assertEqual((writer["input_tokens"], writer["output_tokens"], writer["seconds"]), (100, 40, 5.0))

    def test_logging_never_fails_the_production(self):
        runlog.append(Path("/nonexistent/folder"), {"role": "x"})
        runlog.append(None, {"role": "x"})


if __name__ == "__main__":
    unittest.main()
