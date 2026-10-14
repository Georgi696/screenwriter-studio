"""Offline regressions: approved inputs, billable-job recovery, and video delivery."""

import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.fixtures import _piece
from screenwriter_studio.videos import service
from screenwriter_studio.videos.plan import build_plan, digest, read_json, write_json
from screenwriter_studio.videos.web import stream_video, video_file, video_status


def production(root):
    folder = Path(root) / "film"
    (folder / "images").mkdir(parents=True)
    dev, screenplay = _piece()
    dev.runtime_seconds = 8
    write_json(folder / "00_development.json", dev.model_dump())
    write_json(folder / "01_screenplay.json", screenplay.model_dump())
    write_json(folder / "verdict.json", {"passed": True})
    (folder / "images/s01.png").write_bytes(b"image")
    write_json(folder / "images/jobs.json", {
        "aspect_ratio": "16:9", "inspection": {"mode": "manual"},
        "jobs": [{"id": "s01", "kind": "keyframe"}],
    })
    write_json(folder / "images/manifest.json", {"images": [
        {"id": "s01", "state": "done", "path": str(folder / "images/s01.png")},
    ]})
    return folder


class VideoPlanTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = production(temp.name)

    def test_timing_identity_and_audio_are_preserved(self):
        plan = build_plan(self.folder)
        self.assertEqual(plan["runtime_seconds"], 8)
        self.assertEqual(plan["shots"][0]["duration_seconds"], 8)
        self.assertIn("Maya waits at the door", plan["shots"][0]["prompt"])
        self.assertIn("room tone", plan["shots"][0]["prompt"])
        self.assertEqual(len(plan["shots"][0]["image_sha256"]), 64)

    def test_review_missing_coverage_and_changed_runtime_block_video(self):
        path = self.folder / "images/manifest.json"
        saved = read_json(path)
        for state in ("review", "blocked", "failed", "planned"):
            saved["images"][0]["state"] = state
            write_json(path, saved)
            with self.assertRaisesRegex(ValueError, "Finish and approve"):
                build_plan(self.folder)
        saved["images"] = []
        write_json(path, saved)
        with self.assertRaises(ValueError):
            build_plan(self.folder)
        development = read_json(self.folder / "00_development.json")
        development["runtime_seconds"] = 30
        write_json(self.folder / "00_development.json", development)
        with self.assertRaisesRegex(ValueError, "runtime"):
            build_plan(self.folder)

    def test_rejected_pages_require_explicit_greenlight(self):
        write_json(self.folder / "verdict.json", {"passed": False})
        with self.assertRaisesRegex(ValueError, "Approve"):
            build_plan(self.folder)
        write_json(self.folder / "greenlight.json", {"proceeded": True})
        self.assertEqual(len(build_plan(self.folder)["shots"]), 1)

    def test_preview_spends_no_credits_and_leaves_live_manifest_alone(self):
        with mock.patch.object(service, "_request") as request, mock.patch.object(service, "hosted_reference_url") as upload:
            result = service.render_video(self.folder, dry_run=True)
        self.assertEqual(result["status"], "planned")
        self.assertTrue((self.folder / "videos/jobs.preview.json").is_file())
        self.assertFalse((self.folder / "videos/manifest.json").exists())
        request.assert_not_called()
        upload.assert_not_called()

    def test_external_keyframe_is_rejected(self):
        path = self.folder / "images/manifest.json"
        saved = read_json(path)
        outside = self.folder.parent / "private.png"
        outside.write_bytes(b"private")
        saved["images"][0]["path"] = str(outside)
        write_json(path, saved)
        with self.assertRaisesRegex(ValueError, "outside"):
            build_plan(self.folder)


class VideoRecoveryTest(unittest.TestCase):
    def setUp(self):
        VideoPlanTest.setUp(self)
        for name in ("validate_keyframes", "validate_clip"):
            patcher = mock.patch.object(service, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("load_kie_api_key", "test-key"), ("hosted_reference_url", "https://host/frame.png")):
            patcher = mock.patch.object(service, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request = mock.patch.object(service, "_request", return_value={"code": 200, "data": {"taskId": "task-1"}})
        self.create = self.request.start()
        self.addCleanup(self.request.stop)
        poll = mock.patch.object(service, "poll_task", return_value="https://host/result.mp4")
        self.poll = poll.start()
        self.addCleanup(poll.stop)
        download = mock.patch.object(service, "download_video", side_effect=lambda url, path, cancel: path.write_bytes(b"clip"))
        self.download = download.start()
        self.addCleanup(download.stop)

    def test_finished_clips_are_not_submitted_again(self):
        self.assertEqual(service.render_video(self.folder)["status"], "complete")
        request = self.create.call_args.kwargs["payload"]
        self.assertEqual(request["model"], "kling-3.0/video")
        self.assertEqual(request["input"]["duration"], "8")
        self.assertFalse(request["input"]["multi_shots"])
        service.render_video(self.folder)
        self.create.assert_called_once()

    def _two_shots(self):
        script = read_json(self.folder / "01_screenplay.json")
        script["shots"].append({**script["shots"][0], "number": 2})
        write_json(self.folder / "01_screenplay.json", script)
        dev = read_json(self.folder / "00_development.json")
        dev["runtime_seconds"] = 16
        write_json(self.folder / "00_development.json", dev)
        spec = read_json(self.folder / "images/jobs.json")
        spec["jobs"].append({"id": "s02", "kind": "keyframe"})
        write_json(self.folder / "images/jobs.json", spec)
        (self.folder / "images/s02.png").write_bytes(b"second image")
        images = read_json(self.folder / "images/manifest.json")
        images["images"].append({"id": "s02", "state": "done", "path": str(self.folder / "images/s02.png")})
        write_json(self.folder / "images/manifest.json", images)

    def test_clips_run_side_by_side(self):
        self._two_shots()
        barrier = threading.Barrier(2, timeout=5)
        self.poll.side_effect = lambda *args: (barrier.wait(), "https://host/result.mp4")[1]
        self.assertEqual(service.render_video(self.folder)["status"], "complete")
        self.assertEqual(self.create.call_count, 2)

    def test_one_failed_clip_does_not_abandon_the_other(self):
        self._two_shots()
        calls = iter([RuntimeError("down"), "https://host/result.mp4"])
        lock = threading.Lock()
        def poll(*args):
            with lock:
                result = next(calls)
            if isinstance(result, Exception):
                raise result
            return result
        self.poll.side_effect = poll
        with self.assertRaisesRegex(RuntimeError, "down"):
            service.render_video(self.folder)
        states = sorted(clip["state"] for clip in read_json(self.folder / "videos/manifest.json")["clips"])
        self.assertEqual(states, ["done", "polling"])
        self.poll.side_effect = None
        self.poll.return_value = "https://host/result.mp4"
        self.assertEqual(service.render_video(self.folder)["status"], "complete")
        self.assertEqual(self.create.call_count, 2)

    def test_single_shot_renders_then_rest_completes(self):
        self._two_shots()
        self.assertEqual(service.render_video(self.folder, shots=["s01"])["status"], "partial")
        self.assertEqual(self.create.call_count, 1)
        self.assertEqual(service.render_video(self.folder)["status"], "complete")
        self.assertEqual(self.create.call_count, 2)

    def test_redo_replaces_one_clip_and_keeps_the_old_take(self):
        self._two_shots()
        service.render_video(self.folder)
        self.download.side_effect = lambda url, path, cancel: path.write_bytes(b"new take")
        result = service.render_video(self.folder, regenerate=["s02"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.create.call_count, 3)
        videos = self.folder / "videos"
        self.assertEqual((videos / "s02_v1.mp4").read_bytes(), b"clip")
        self.assertEqual((videos / "s02.mp4").read_bytes(), b"new take")
        self.assertEqual((videos / "s01.mp4").read_bytes(), b"clip")
        with self.assertRaisesRegex(ValueError, "Unknown shot"):
            service.render_video(self.folder, shots=["s09"])

    def test_mixed_model_production_submits_each_shot_to_its_selected_adapter(self):
        script = read_json(self.folder / "01_screenplay.json")
        script["shots"].append({**script["shots"][0], "number": 2, "duration_seconds": 12})
        write_json(self.folder / "01_screenplay.json", script)
        dev = read_json(self.folder / "00_development.json")
        dev["runtime_seconds"] = 20
        write_json(self.folder / "00_development.json", dev)
        spec = read_json(self.folder / "images/jobs.json")
        spec["jobs"].append({"id": "s02", "kind": "keyframe"})
        write_json(self.folder / "images/jobs.json", spec)
        (self.folder / "images/s02.png").write_bytes(b"second image")
        images = read_json(self.folder / "images/manifest.json")
        images["images"].append({"id": "s02", "state": "done", "path": str(self.folder / "images/s02.png")})
        write_json(self.folder / "images/manifest.json", images)
        result = service.render_video(self.folder)
        # Clips are submitted concurrently, so arrival order is not guaranteed.
        requests = sorted((call.kwargs["payload"] for call in self.create.call_args_list),
                          key=lambda request: request["model"] != "kling-3.0/video")
        self.assertEqual([request["model"] for request in requests], ["kling-3.0/video", "bytedance/seedance-2-5"])
        self.assertEqual(requests[1]["input"]["duration"], 12)
        self.assertIn("first_frame_url", requests[1]["input"])
        self.assertEqual([clip["model"] for clip in result["clips"]], [request["model"] for request in requests])
        with mock.patch("screenwriter_studio.videos.plan.choose_model", side_effect=AssertionError("must not reroute")):
            service.render_video(self.folder)
        self.assertEqual(self.create.call_count, 2)

    def test_version_one_provider_task_resumes_without_model_or_prompt_migration(self):
        original = build_plan(self.folder, selection="kling", frozen_plan={"version": 1, "shots": []})
        out = self.folder / "videos"
        out.mkdir()
        write_json(out / "jobs.json", original)
        write_json(out / "manifest.json", {"version": 1, "fingerprint": digest(original), "status": "paused",
                   "clips": [{"id": "s01", "state": "polling", "task_id": "original-kling-task"}]})
        service.render_video(self.folder)
        self.create.assert_not_called()
        self.assertEqual(self.poll.call_args.args[1], "original-kling-task")
        self.assertEqual(read_json(out / "jobs.json"), original)

    def test_poll_interruption_resumes_saved_id_without_submit(self):
        self.poll.side_effect = [TimeoutError("pending"), "https://host/result.mp4"]
        with self.assertRaises(TimeoutError):
            service.render_video(self.folder)
        self.assertEqual(read_json(self.folder / "videos/manifest.json")["clips"][0]["task_id"], "task-1")
        service.render_video(self.folder)
        self.create.assert_called_once()

    def test_failed_download_reuses_result_url(self):
        self.download.side_effect = OSError("download interrupted")
        with self.assertRaises(OSError):
            service.render_video(self.folder)
        self.download.side_effect = lambda url, path, cancel: path.write_bytes(b"clip")
        service.render_video(self.folder)
        self.create.assert_called_once()
        self.poll.assert_called_once()

    def test_uncertain_submission_is_not_repeated_and_can_attach_task(self):
        self.create.side_effect = TimeoutError("lost response")
        with self.assertRaises(TimeoutError):
            service.render_video(self.folder)
        with self.assertRaisesRegex(ValueError, "outcome unknown"):
            service.render_video(self.folder)
        service.attach_task(self.folder, "s01", "recovered-task")
        service.render_video(self.folder)
        self.create.assert_called_once()
        self.assertEqual(self.poll.call_args.args[1], "recovered-task")

    def test_terminal_provider_failure_retries_explicitly_and_keeps_history(self):
        self.poll.side_effect = [service.JobFailed("provider failed"), "https://host/result.mp4"]
        with self.assertRaises(service.JobFailed):
            service.render_video(self.folder)
        result = service.render_video(self.folder)
        self.assertEqual(self.create.call_count, 2)
        self.assertEqual(result["clips"][0]["attempts"][0]["task_id"], "task-1")

    def test_changed_input_and_settings_cannot_reuse_saved_jobs(self):
        service.render_video(self.folder)
        with self.assertRaisesRegex(ValueError, "changed"):
            service.render_video(self.folder, sound=False)
        script_path = self.folder / "01_screenplay.json"
        original = read_json(script_path)
        write_json(script_path, {**original, "fountain": "Title: A different screenplay"})
        with self.assertRaisesRegex(ValueError, "changed"):
            service.render_video(self.folder)
        write_json(script_path, original)
        self.create.assert_called_once()

    def test_definite_rejections_fail_cleanly_and_unknown_outcomes_can_be_forgotten(self):
        import urllib.error
        from screenwriter_studio.images.generator import ProviderHTTPError
        self.create.side_effect = ProviderHTTPError(429, "HTTP 429: too many requests")
        with self.assertRaises(service.JobFailed):
            service.render_video(self.folder)
        self.assertEqual(read_json(self.folder / "videos/manifest.json")["clips"][0]["state"], "failed")
        self.create.side_effect = urllib.error.URLError(ConnectionRefusedError("refused"))
        with self.assertRaises(service.JobFailed):
            service.render_video(self.folder)
        self.create.side_effect = TimeoutError("read timed out")
        with self.assertRaises(TimeoutError):
            service.render_video(self.folder)
        self.assertEqual(read_json(self.folder / "videos/manifest.json")["clips"][0]["state"], "uncertain")
        self.create.side_effect = None
        with self.assertRaisesRegex(ValueError, "outcome unknown"):
            service.render_video(self.folder)
        service.forget_submission(self.folder, "s01")
        self.assertEqual(service.render_video(self.folder)["status"], "complete")
        with self.assertRaisesRegex(ValueError, "Only an uncertain"):
            service.forget_submission(self.folder, "s01")

    def test_short_clip_is_kept_for_viewing_and_marked_failed(self):
        self.validate = mock.patch.object(service, "validate_clip", side_effect=ValueError("s01.mp4: shorter than planned.")).start()
        self.addCleanup(mock.patch.stopall)
        with self.assertRaises(service.JobFailed):
            service.render_video(self.folder)
        clip = read_json(self.folder / "videos/manifest.json")["clips"][0]
        self.assertEqual(clip["state"], "failed")
        self.assertTrue((self.folder / "videos/s01_rejected.mp4").is_file())
        self.assertFalse((self.folder / "videos/s01.mp4").exists())

    def test_ctrl_c_stops_queued_clips_and_pauses(self):
        self._two_shots()
        self.poll.side_effect = KeyboardInterrupt
        cancel = threading.Event()
        with self.assertRaises(KeyboardInterrupt):
            service.render_video(self.folder, cancel=cancel)
        self.assertTrue(cancel.is_set())
        self.assertEqual(read_json(self.folder / "videos/manifest.json")["status"], "paused")

    def test_dry_run_rejects_single_shot_options(self):
        with self.assertRaisesRegex(ValueError, "covers every shot"):
            service.render_video(self.folder, dry_run=True, regenerate=["s01"])

    def test_replacing_a_shot_image_keeps_other_clips_and_allows_redo(self):
        service.render_video(self.folder)
        (self.folder / "images/s01.png").write_bytes(b"repaired image")
        # The finished clip stays; nothing is resubmitted just because the image changed.
        self.assertEqual(service.render_video(self.folder)["status"], "complete")
        self.create.assert_called_once()
        service.render_video(self.folder, regenerate=["s01"])
        self.assertEqual(self.create.call_count, 2)
        clip = read_json(self.folder / "videos/manifest.json")["clips"][0]
        self.assertEqual(clip["image_sha256"], service.file_digest(self.folder / "images/s01.png"))

    def test_cancel_before_work_does_not_submit(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            service.render_video(self.folder, cancel=cancel)
        self.create.assert_not_called()

    def test_another_process_holds_generation_lock(self):
        import fcntl
        out = self.folder / "videos"
        out.mkdir()
        with (out / ".generation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "still running or still stopping"):
                service.render_video(self.folder)
        self.create.assert_not_called()


class ClipDownloadTest(unittest.TestCase):
    def test_zip_holds_finished_clips_in_shot_order_and_shot_plan_is_public(self):
        import zipfile
        from screenwriter_studio.videos.web import clips_zip
        from screenwriter_studio.web.server import shot_plan
        with tempfile.TemporaryDirectory() as root:
            folder = production(root)
            out = folder / "videos"
            out.mkdir()
            for name, data in (("s01.mp4", b"one"), ("s02.mp4", b"two"), ("s02_v1.mp4", b"old")):
                (out / name).write_bytes(data)
            write_json(out / "manifest.json", {"clips": [
                {"id": "s01", "state": "done"}, {"id": "s02", "state": "done"}, {"id": "s03", "state": "pending"}]})
            handler = mock.Mock()
            handler.wfile = io.BytesIO()
            self.assertTrue(clips_zip(handler, folder))
            with zipfile.ZipFile(io.BytesIO(handler.wfile.getvalue())) as bundle:
                self.assertEqual(bundle.namelist(), ["s01.mp4", "s02.mp4"])
            write_json(out / "manifest.json", {"clips": [{"id": "s01", "state": "pending"}]})
            self.assertFalse(clips_zip(mock.Mock(), folder))
            plan = shot_plan(folder)
            self.assertEqual(plan["shots"][0]["id"], "s01")
            self.assertEqual(plan["aspect_ratio"], "16:9")
            self.assertEqual(shot_plan(None)["shots"], [])


class VideoDeliveryTest(unittest.TestCase):
    def test_ranges_download_and_private_paths(self):
        with tempfile.TemporaryDirectory() as root:
            folder = production(root)
            (folder / "videos").mkdir()
            path = folder / "videos/s02.mp4"
            path.write_bytes(b"0123456789")
            for requested, expected, code in (("bytes=2-5", b"2345", 206), ("bytes=-3", b"789", 206), ("bytes=7-", b"789", 206), (None, b"0123456789", 200), ("bytes=20-", b"", 416), ("bytes=0-1,4-5", b"", 416)):
                handler = mock.Mock()
                handler.headers = {"Range": requested} if requested else {}
                handler.wfile = io.BytesIO()
                stream_video(handler, path, attachment=True)
                handler.send_response.assert_called_once_with(code)
                self.assertEqual(handler.wfile.getvalue(), expected)
            self.assertEqual(video_file(Path(root), "film/videos/s02.mp4"), path.resolve())
            for name in ("../film/videos/s02.mp4", str(path), "film/.env", "film/images/s01.png"):
                self.assertIsNone(video_file(Path(root), name))
            (folder / "videos/s01.mp4").symlink_to(Path(root).parent / "private.mp4")
            self.assertIsNone(video_file(Path(root), "film/videos/s01.mp4"))

    def test_status_exposes_no_local_paths_and_needs_no_ffmpeg(self):
        with tempfile.TemporaryDirectory() as root:
            folder = production(root)
            with mock.patch("shutil.which", return_value=None):
                status = video_status(folder)
            self.assertTrue(status["ready"])
            self.assertTrue(status["can_plan"])
            self.assertNotIn(str(folder), json.dumps(status))
            # Default settings: Kling 3.0 pro with sound, 27 credits per second, for an 8-second shot.
            self.assertEqual(status["shots"][0]["credits"], 27 * 8)
            self.assertEqual(status["estimate"]["remaining"], 27 * 8)


class VideoProviderTest(unittest.TestCase):
    def test_poll_contract_success_pending_and_terminal_failure(self):
        cancel = mock.Mock()
        cancel.is_set.return_value = False
        replies = [{"code": 200, "data": {"state": "generating"}},
                   {"code": 200, "data": {"state": "success", "resultJson": json.dumps({"resultUrls": ["https://host/clip.mp4"]})}}]
        with mock.patch.object(service, "_request", side_effect=replies) as request:
            self.assertEqual(service.poll_task("key", "saved-task", cancel), "https://host/clip.mp4")
        self.assertEqual(request.call_args.kwargs, {"query": {"taskId": "saved-task"}})
        cancel.wait.assert_called_once_with(service.POLL_INTERVAL)
        with mock.patch.object(service, "_request", return_value={"code": 200, "data": {"state": "fail", "failMsg": "rejected"}}):
            with self.assertRaises(service.JobFailed):
                service.poll_task("key", "saved-task", cancel)

    def test_poll_api_error_preserves_task_and_cancel_never_polls(self):
        cancel = threading.Event()
        with mock.patch.object(service, "POLL_INTERVAL", 0), \
                mock.patch.object(service, "_request", return_value={"code": 500, "msg": "unavailable"}) as request:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                service.poll_task("key", "saved-task", cancel)
            # Status checks are retried before the clip pauses; they never resubmit.
            self.assertEqual(request.call_count, service.POLL_RETRIES + 1)
            request.reset_mock()
            cancel.set()
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                service.poll_task("key", "saved-task", cancel)
            request.assert_not_called()


class VideoHTTPTest(unittest.TestCase):
    def setUp(self):
        from screenwriter_studio.web import server
        from screenwriter_studio.web.board import Board
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = production(self.root)
        self.server = server
        for patcher in (
            mock.patch.object(server, "PRODUCTIONS", self.root),
            mock.patch.object(server, "_board", Board(folder=str(self.folder))),
            mock.patch.object(server, "hub", server.Hub()),
            mock.patch.object(server, "_cancel", None),
            mock.patch.object(server, "_future", None),
            mock.patch.object(server, "_run_serial", 0),
            mock.patch("screenwriter_studio.web.board.STATUS_PATH", self.root / "status.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.httpd = server.bind(0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)

    def post(self, data):
        return urllib.request.urlopen(urllib.request.Request(
            self.url + "/api/video", data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method="POST"), timeout=5)

    def test_route_preflights_inputs_and_prevents_concurrent_work(self):
        with mock.patch.object(self.server, "_ensure_loop"), mock.patch.object(self.server, "_commit_future") as commit:
            commit.side_effect = lambda serial, cancel, coro: coro.close()
            with self.post({"mode": "std", "sound": False}) as response:
                self.assertEqual(response.status, 202)
            self.assertTrue(self.server._board.running)
            self.assertEqual(self.server._board.active, "video")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post({})
            self.assertEqual(caught.exception.code, 409)
            commit.assert_called_once()

    def test_single_shot_and_redo_reach_the_renderer_and_are_validated(self):
        with mock.patch.object(self.server, "_ensure_loop"), \
                mock.patch.object(self.server, "_commit_future") as commit, \
                mock.patch.object(self.server, "_drive_video", new=mock.Mock(return_value=None)) as drive:
            commit.side_effect = lambda serial, cancel, coro: None
            for data in ({"shots": "s01"}, {"redo": ["../x"]}, {"shots": [1]}, {"shots": ["s01"], "dry_run": True}):
                with self.assertRaises(urllib.error.HTTPError):
                    self.post(data)
            drive.assert_not_called()
            with self.post({"redo": ["s01"]}) as response:
                self.assertEqual(response.status, 202)
            self.assertEqual(drive.call_args.args[-3:], (None, ["s01"], None))
            # An edited prompt is accepted only for the shot being rendered or redone.
            self.server._board.running = False  # the mocked run above never finishes
            with self.assertRaises(urllib.error.HTTPError):
                self.post({"redo": ["s01"], "prompts": {"s02": "Different."}})
            with self.post({"redo": ["s01"], "prompts": {"s01": "Slow push in on Maya."}}) as response:
                self.assertEqual(response.status, 202)
            self.assertEqual(drive.call_args.args[-1], {"s01": "Slow push in on Maya."})

    def test_invalid_settings_or_unapproved_still_never_launch(self):
        with mock.patch.object(self.server, "_ensure_loop"), mock.patch.object(self.server, "_commit_future") as commit:
            for data in ({"sound": "yes"}, {"mode": "unknown"}, {"dry_run": "false"}, {"selection": "unknown"}):
                with self.assertRaises(urllib.error.HTTPError):
                    self.post(data)
            saved = read_json(self.folder / "images/manifest.json")
            saved["images"][0]["state"] = "review"
            write_json(self.folder / "images/manifest.json", saved)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post({})
            self.assertIn(b"approve", caught.exception.read())
            commit.assert_not_called()

    def test_mp4_endpoint_supports_seek_and_library_discovery(self):
        (self.folder / "videos").mkdir()
        (self.folder / "videos/s01.mp4").write_bytes(b"0123456789")
        request = urllib.request.Request(self.url + "/video-media?path=film/videos/s01.mp4", headers={"Range": "bytes=4-7"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertEqual(response.headers["Content-Range"], "bytes 4-7/10")
            self.assertEqual(response.read(), b"4567")
        with urllib.request.urlopen(self.url + "/api/library", timeout=5) as response:
            library = json.load(response)
        self.assertEqual(library["projects"][0]["videos"][0]["path"], "film/videos/s01.mp4")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url + "/video-media?path=../private.mp4", timeout=5)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
