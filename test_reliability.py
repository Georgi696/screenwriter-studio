"""Regression coverage for production gates and paid-job recovery. No network."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import generate_stills as generator
from schemas import Beat, Screenplay, ScriptVerdict, Shot, StillJob, StillPackage
from stills import validate_still_package
from studio import ScreenwriterStudio, _script_issues, _stream_stills, art_user_message, editor_user_message, writer_user_message
from test_prompts import _piece


class ScriptGateTest(unittest.TestCase):
    def test_deterministic_checks_reject_duration_and_duplicate_numbers(self):
        development, screenplay = _piece()
        screenplay.shots *= 2
        issues = _script_issues(development, screenplay)
        self.assertTrue(any("16s" in issue for issue in issues))
        self.assertTrue(any("unique" in issue for issue in issues))

    def test_model_approval_cannot_override_runtime_validation(self):
        development, screenplay = _piece()
        result = Mock()
        result.final_output_as.return_value = ScriptVerdict(passed=True, issues=[])
        with patch("studio.Runner.run", new=AsyncMock(return_value=result)):
            verdict = asyncio.run(ScreenwriterStudio()._edit(development, screenplay))
        self.assertFalse(verdict.passed)
        self.assertTrue(verdict.issues)

    def test_exhausted_rewrites_preserve_shots_and_block_art(self):
        development, screenplay = _piece()
        screenplay.shots = [screenplay.shots[0].model_copy(update={"number": n}) for n in range(1, 6)]
        studio = ScreenwriterStudio()
        studio._develop = AsyncMock(return_value=development)
        studio._write = AsyncMock(return_value=screenplay)
        studio._edit = AsyncMock(return_value=ScriptVerdict(passed=False, issues=["Shot list has 5 shots; limit is 3."]))
        studio._art = AsyncMock()

        async def collect():
            return [event async for event in studio.run("hall")]

        with tempfile.TemporaryDirectory() as temp, patch("studio.REPO_ROOT", Path(temp)), patch("studio.load_kie_api_key", return_value="fake"):
            events = asyncio.run(collect())
            folder = Path(temp) / "productions/hall"
            self.assertFalse(json.loads((folder / "verdict.json").read_text())["passed"])
            saved = json.loads((folder / "01_screenplay.json").read_text())
            self.assertEqual(len(saved["shots"]), 5)
        self.assertEqual(studio._write.await_count, 3)
        studio._art.assert_not_awaited()
        self.assertEqual(events[-1].kind, "failed")

    def test_schema_can_represent_shortest_and_longest_runtime(self):
        development, screenplay = _piece()
        short = development.model_dump()
        short.update(runtime_seconds=6, beats=[Beat(name="Hook", timecode="0:00", action="Wait").model_dump()])
        self.assertEqual(type(development).model_validate(short).runtime_seconds, 6)
        development.runtime_seconds = 720
        screenplay.shots = [screenplay.shots[0].model_copy(update={"number": n, "duration_seconds": 8}) for n in range(1, 91)]
        validated = Screenplay.model_validate(screenplay.model_dump())
        self.assertEqual(_script_issues(development, validated), [])

    def test_package_missing_coverage_or_dependencies_is_rejected(self):
        development, screenplay = _piece()
        package = StillPackage(aspect_ratio="16:9", resolution="1K", jobs=[
            StillJob(id="s01", kind="keyframe", prompt="A hallway", references=["missing"])
        ])
        with self.assertRaisesRegex(ValueError, "references"):
            validate_still_package(package, development, screenplay)
        package.jobs[0].references = []
        package.jobs[0].id = "s02"
        with self.assertRaisesRegex(ValueError, "one keyframe"):
            validate_still_package(package, development, screenplay)

    def test_invalid_input_stops_before_chat_calls(self):
        async def run():
            return [event async for event in ScreenwriterStudio().run("hall", runtime_seconds=30, max_shots=1)]
        with patch("studio.Runner.run", new=AsyncMock()) as call:
            with self.assertRaises(ValueError):
                asyncio.run(run())
            call.assert_not_awaited()

    def test_partial_still_failure_is_not_reported_as_complete(self):
        package = StillPackage(aspect_ratio="16:9", resolution="1K", jobs=[])
        async def run():
            return [event async for event in _stream_stills(package, Path("unused"), dry_run=False)]
        with patch("studio.render_stills", return_value={"images": [{"id": "s01", "error": "failed"}]}):
            events = asyncio.run(run())
        self.assertEqual(events[-1].kind, "failed")

    def test_custom_limit_flows_through_to_a_complete_still_preview(self):
        development, screenplay = _piece()
        screenplay.shots = [screenplay.shots[0].model_copy(update={"number": n, "duration_seconds": 5}) for n in range(1, 7)]
        package = StillPackage(aspect_ratio="16:9", resolution="1K", jobs=[
            StillJob(id=f"s{n:02d}", kind="keyframe", prompt="A quiet hallway.") for n in range(1, 7)
        ])
        studio = ScreenwriterStudio()
        studio._develop = AsyncMock(return_value=development)
        studio._write = AsyncMock(return_value=screenplay)
        studio._art = AsyncMock(return_value=package)
        result = Mock()
        result.final_output_as.return_value = ScriptVerdict(passed=True, issues=[])

        async def collect():
            return [event async for event in studio.run("hall", max_shots=6, dry_run=True)]

        with tempfile.TemporaryDirectory() as temp, patch("studio.REPO_ROOT", Path(temp)), patch("studio.load_kie_api_key", return_value="fake"), patch("studio.Runner.run", new=AsyncMock(return_value=result)), patch.object(generator, "_request") as request:
            events = asyncio.run(collect())
            request.assert_not_called()
            self.assertEqual(events[-1].kind, "complete")
            preview = Path(temp) / "productions/hall/images/manifest.preview.json"
            self.assertEqual(len(json.loads(preview.read_text())["images"]), 6)
        locked = studio._write.call_args.args[0]
        self.assertEqual(locked.max_shots, 6)
        self.assertIn("6 shots maximum", writer_user_message(locked, None, None))
        self.assertIn('"shot_budget":6', editor_user_message(locked, screenplay))
        self.assertIn('"max_keyframes": 6', art_user_message(locked, screenplay))


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.spec = {"aspect_ratio": "16:9", "resolution": "1K", "jobs": [
            {"id": "s01", "kind": "keyframe", "prompt": "A quiet hallway."}
        ]}
        self.key = patch.object(generator, "load_api_key", return_value="fake-key")
        self.key.start()
        self.addCleanup(self.key.stop)

    def manifest(self):
        return json.loads((self.out / "manifest.json").read_text())

    @staticmethod
    def download(url, dest):
        dest.write_bytes(b"image")

    def successful_request(self, url, key, payload=None, query=None):
        if payload is not None:
            self.assertEqual(self.manifest()["images"][0]["state"], "submitting")
            return {"code": 200, "data": {"taskId": "task-1"}}
        self.assertEqual(self.manifest()["images"][0]["task_id"], "task-1")
        return {"code": 200, "data": {"state": "success", "resultJson": {"resultUrls": ["https://example.com/frame.png"]}}}

    def test_submit_is_checkpointed_and_success_is_reused(self):
        with patch.object(generator, "_request", side_effect=self.successful_request) as request, patch.object(generator, "POLL_INTERVAL", 0), patch.object(generator, "download", side_effect=self.download):
            generator.run_batch(self.spec, self.out)
            self.assertEqual(request.call_count, 2)
            result = generator.run_batch(self.spec, self.out, resume=True)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(result["images"][0]["state"], "done")

    def test_timeout_resumes_existing_task_without_resubmission(self):
        with patch.object(generator, "_request", return_value={"code": 200, "data": {"taskId": "task-1"}}) as request, patch.object(generator, "MAX_WAIT", 0):
            result = generator.run_batch(self.spec, self.out)
            self.assertEqual(request.call_count, 1)
        self.assertIn("timed out", result["images"][0]["error"])
        self.assertEqual(self.manifest()["images"][0]["state"], "polling")
        with patch.object(generator, "_request", side_effect=self.successful_request) as request, patch.object(generator, "POLL_INTERVAL", 0), patch.object(generator, "download", side_effect=self.download):
            result = generator.run_batch(self.spec, self.out, resume=True)
            self.assertEqual(request.call_count, 1)
            self.assertIsNotNone(request.call_args.kwargs.get("query"))
            self.assertEqual(result["images"][0]["state"], "done")

    def test_download_failure_retries_download_only(self):
        with patch.object(generator, "_request", side_effect=self.successful_request), patch.object(generator, "POLL_INTERVAL", 0), patch.object(generator, "download", side_effect=OSError("disk full")):
            generator.run_batch(self.spec, self.out)
        self.assertEqual(self.manifest()["images"][0]["state"], "downloading")
        with patch.object(generator, "_request") as request, patch.object(generator, "download", side_effect=self.download):
            result = generator.run_batch(self.spec, self.out, resume=True)
            request.assert_not_called()
            self.assertEqual(result["images"][0]["state"], "done")

    def test_ambiguous_submission_never_automatically_duplicates(self):
        with patch.object(generator, "_request", side_effect=TimeoutError("lost response")) as request:
            generator.run_batch(self.spec, self.out)
            result = generator.run_batch(self.spec, self.out, resume=True)
            self.assertEqual(request.call_count, 1)
        self.assertIn("unknown", result["images"][0]["error"])

    def test_changed_plan_and_accidental_rerun_are_rejected(self):
        with patch.object(generator, "_request", side_effect=TimeoutError()):
            generator.run_batch(self.spec, self.out)
        with self.assertRaisesRegex(ValueError, "resume"):
            generator.run_batch(self.spec, self.out)
        self.spec["jobs"][0]["prompt"] = "Changed."
        with self.assertRaisesRegex(ValueError, "differ"):
            generator.run_batch(self.spec, self.out, resume=True)

    def test_dry_run_does_not_overwrite_paid_manifest(self):
        with patch.object(generator, "_request", side_effect=TimeoutError()):
            generator.run_batch(self.spec, self.out)
        before = (self.out / "manifest.json").read_bytes()
        with patch.object(generator, "_request") as request:
            generator.run_batch(self.spec, self.out, dry_run=True)
            request.assert_not_called()
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        self.assertTrue((self.out / "manifest.preview.json").exists())

    def test_cancellation_prevents_new_submissions(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(generator, "_request") as request:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                generator.run_batch(self.spec, self.out, cancel=cancel)
            request.assert_not_called()

    def test_path_escape_collision_and_cycles_fail_before_spending(self):
        bad_specs = [
            {"jobs": [{"id": "s01", "filename": "../escape.png"}]},
            {"jobs": [{"id": "a/b"}, {"id": "a_b"}]},
            {"jobs": [{"id": "s01", "references": ["s02"]}, {"id": "s02", "references": ["s01"]}]},
        ]
        with patch.object(generator, "_request") as request:
            for spec in bad_specs:
                with self.assertRaises(ValueError):
                    generator.run_batch(spec, self.out)
            request.assert_not_called()

    def test_terminal_failure_can_be_retried_on_explicit_resume(self):
        failed = {"code": 200, "data": {"state": "fail", "failMsg": "provider failed"}}
        created = {"code": 200, "data": {"taskId": "task-1"}}
        with patch.object(generator, "_request", side_effect=[created, failed]), patch.object(generator, "POLL_INTERVAL", 0):
            result = generator.run_batch(self.spec, self.out)
        self.assertEqual(result["images"][0]["state"], "failed")
        with patch.object(generator, "_request", side_effect=self.successful_request) as request, patch.object(generator, "POLL_INTERVAL", 0), patch.object(generator, "download", side_effect=self.download):
            result = generator.run_batch(self.spec, self.out, resume=True)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(result["images"][0]["state"], "done")

    def test_temporarily_missing_reference_preserves_existing_child_task(self):
        job = {"id": "child", "prompt": "Quiet hallway.", "references": ["parent"]}
        previous = {"id": "child", "task_id": "existing-child", "state": "polling"}
        with patch.object(generator, "_request") as request:
            result = generator.run_job("fake", job, "16:9", "1K", self.out, {}, False, previous=previous)
            request.assert_not_called()
        self.assertEqual(result["task_id"], "existing-child")
        self.assertEqual(result["state"], "polling")
        self.assertIn("reference", result["error"])

    def test_same_folder_cannot_run_concurrently(self):
        import fcntl
        with (self.out / ".generation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(generator, "_request") as request:
                with self.assertRaisesRegex(RuntimeError, "already has a running batch"):
                    generator.run_batch(self.spec, self.out)
                request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
