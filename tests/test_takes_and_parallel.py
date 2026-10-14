"""Parallel reviewed images, review that holds back only its dependents, uncertain-image recovery,
edited takes, earlier versions, and clip prompt edits and takes. All provider calls are mocked."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_inspection import ACCEPT, PNG, REPAIR
from tests import test_video
from screenwriter_studio.images.generator import run_batch
from screenwriter_studio.images.review import (
    attach_image_task, forget_image_submission, restore_version, versions,
)
from screenwriter_studio.videos import service
from screenwriter_studio.videos.plan import read_json
from screenwriter_studio.videos.service import apply_prompts, plan_fingerprint, use_take


class ImageBatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name)
        self.spec = {"inspection": {"mode": "manual"}, "aspect_ratio": "16:9", "resolution": "1K",
                     "inspection_context": {"brief": "Hall"},
                     "jobs": [{"id": "maya", "kind": "character", "prompt": "Maya, 3:4 portrait."},
                              {"id": "hall", "kind": "location", "prompt": "The empty hall."},
                              {"id": "s01", "kind": "keyframe", "prompt": "Maya in the hall.", "references": ["maya", "hall"]}]}
        self.lock = threading.Lock()
        self.prompts = []
        self.count = 0

        def generate(key, payload, *, task_id=None, on_submitted=None, cancel=None):
            with self.lock:
                self.count += 1
                task = task_id or f"task-{self.count}"
                self.prompts.append(payload["input"]["prompt"])
            if not task_id:
                on_submitted(task)
            return {"task_id": task, "url": f"https://example.test/{task}.png"}

        self.generate = self._patch("screenwriter_studio.images.generator.generate_one", side_effect=generate)
        self._patch("screenwriter_studio.images.generator.load_api_key", return_value="fake-key")
        self._patch("screenwriter_studio.images.generator.download", side_effect=lambda url, dest: dest.write_bytes(PNG + url.encode()))
        self.inspect = self._patch("screenwriter_studio.images.review.inspect_image", return_value=ACCEPT)

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def manifest(self):
        return {record["id"]: record for record in json.loads((self.out / "manifest.json").read_text())["images"]}

    def test_independent_images_are_made_side_by_side(self):
        barrier = threading.Barrier(2, timeout=5)
        original = self.generate.side_effect

        def together(key, payload, **kwargs):
            if payload["input"]["prompt"] in {"Maya, 3:4 portrait.", "The empty hall."}:
                barrier.wait()  # deadlocks (and times out) if the two sheets ran one after the other
            return original(key, payload, **kwargs)

        self.generate.side_effect = together
        result = run_batch(self.spec, self.out)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.prompts[-1], "Maya in the hall.")  # the shot waited for both sheets
        self.assertEqual(self.generate.call_count, 3)

    def test_a_review_holds_back_only_the_images_that_use_it(self):
        self.inspect.side_effect = lambda record, job, *args, **kwargs: REPAIR if job["id"] == "maya" else ACCEPT
        first = run_batch(self.spec, self.out)
        states = {record["id"]: record["state"] for record in first["images"]}
        self.assertEqual(states, {"maya": "review", "hall": "done", "s01": "blocked"})
        self.assertEqual(first["images"][2]["blocked_by"], ["maya"])
        # Resume with the review still open spends nothing and keeps the shot waiting.
        run_batch(self.spec, self.out, resume=True)
        self.assertEqual(self.generate.call_count, 2)
        maya = self.manifest()["maya"]
        done = run_batch(self.spec, self.out, resume=True, decision={"id": "maya", "review_id": maya["review_id"], "action": "keep"})
        self.assertEqual(done["status"], "complete")

    def test_an_edited_prompt_makes_a_new_take_and_any_version_can_come_back(self):
        run_batch(self.spec, self.out)
        before = self.generate.call_count
        original = self.manifest()["s01"]
        with self.assertRaisesRegex(ValueError, "prompt"):
            run_batch(self.spec, self.out, resume=True, decision={"id": "s01", "action": "regenerate", "prompt": "  "})
        result = run_batch(self.spec, self.out, resume=True,
                           decision={"id": "s01", "action": "regenerate", "prompt": "Maya runs through the hall."})
        self.assertEqual(self.generate.call_count, before + 1)
        self.assertEqual(self.prompts[-1], "Maya runs through the hall.")
        take = {record["id"]: record for record in result["images"]}["s01"]
        self.assertEqual(Path(take["path"]).name, "s01.take-2.png")
        self.assertEqual(take["job_override"]["prompt"], "Maya runs through the hall.")
        self.assertEqual(take["state"], "done")
        self.assertTrue(Path(original["path"]).is_file())
        self.assertEqual([Path(entry["path"]).name for entry in versions(take)], ["s01.png"])
        # The inspector judged the take against the edited prompt.
        self.assertEqual(self.inspect.call_args.args[1]["prompt"], "Maya runs through the hall.")

        calls = self.generate.call_count
        restore_version(self.out, "s01", "s01.png")
        back = self.manifest()["s01"]
        self.assertEqual(back["path"], original["path"])
        self.assertEqual(back["decision"], "human chose this version")
        self.assertEqual([Path(entry["path"]).name for entry in versions(back)], ["s01.take-2.png"])
        self.assertEqual(self.generate.call_count, calls)
        with self.assertRaisesRegex(ValueError, "no longer on disk"):
            restore_version(self.out, "s01", "missing.png")
        # A third take does not overwrite the second.
        run_batch(self.spec, self.out, resume=True, decision={"id": "s01", "action": "regenerate", "prompt": "Maya stops."})
        self.assertEqual(Path(self.manifest()["s01"]["path"]).name, "s01.take-3.png")

    def test_a_new_take_spends_only_on_that_image(self):
        self.inspect.side_effect = lambda record, job, *args, **kwargs: REPAIR if job["id"] == "maya" else ACCEPT
        run_batch(self.spec, self.out)
        before = self.generate.call_count
        run_batch(self.spec, self.out, resume=True, decision={"id": "hall", "action": "regenerate", "prompt": "The hall at dawn."})
        self.assertEqual(self.generate.call_count, before + 1)
        states = self.manifest()
        self.assertEqual(states["maya"]["state"], "review")
        self.assertEqual(states["s01"]["state"], "blocked")

    def test_uncertain_submissions_can_be_attached_or_sent_again(self):
        def lost(key, payload, **kwargs):
            raise TimeoutError("socket timed out after sending")

        self.generate.side_effect = lost
        self.spec["jobs"] = self.spec["jobs"][:1]
        run_batch(self.spec, self.out)
        record = self.manifest()["maya"]
        self.assertEqual(record["state"], "submitting")
        self.assertFalse(record.get("task_id"))
        with self.assertRaisesRegex(ValueError, "task ID"):
            attach_image_task(self.out, "maya", " ")
        attach_image_task(self.out, "maya", "found-task")
        self.assertEqual(self.manifest()["maya"]["state"], "polling")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            forget_image_submission(self.out, "maya")
        self.generate.side_effect = lambda key, payload, *, task_id=None, **kwargs: {"task_id": task_id, "url": "https://example.test/found.png"}
        result = run_batch(self.spec, self.out, resume=True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.generate.call_args.kwargs["task_id"], "found-task")

        # Forgetting turns the record into a plain failure that resume submits again.
        manifest = json.loads((self.out / "manifest.json").read_text())
        manifest["images"][0].update(state="submitting", task_id=None, url=None, error="unknown")
        (self.out / "manifest.json").write_text(json.dumps(manifest))
        forget_image_submission(self.out, "maya")
        self.assertEqual(self.manifest()["maya"]["state"], "failed")

    def test_record_edits_refuse_a_running_batch(self):
        run_batch(self.spec, self.out)
        import fcntl
        with (self.out / ".generation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, "running"):
                restore_version(self.out, "s01", "s01.png")


class ClipPromptAndTakeTest(unittest.TestCase):
    setUp = test_video.VideoRecoveryTest.setUp

    def test_an_edited_clip_prompt_is_sent_and_keeps_the_production_unlocked(self):
        service.render_video(self.folder)
        planned = read_json(self.folder / "videos/jobs.json")
        fingerprint = plan_fingerprint(planned)
        with self.assertRaisesRegex(ValueError, "render or redo"):
            service.render_video(self.folder, prompts={"s01": "Slow push in."})
        service.render_video(self.folder, regenerate=["s01"], prompts={"s01": "Slow push in on Maya."})
        self.assertEqual(self.create.call_args.kwargs["payload"]["input"]["prompt"], "Slow push in on Maya.")
        saved = read_json(self.folder / "videos/jobs.json")
        self.assertEqual(saved["shots"][0]["prompt"], "Slow push in on Maya.")
        self.assertEqual(saved["shots"][0]["original_prompt"], planned["shots"][0]["prompt"])
        self.assertEqual(plan_fingerprint(saved), fingerprint)
        # A later resume still accepts the production.
        self.assertEqual(service.render_video(self.folder)["status"], "complete")
        with self.assertRaisesRegex(ValueError, "1–"):
            apply_prompts({"version": 2, "shots": saved["shots"]}, {"s01": "x" * 3000}, {"s01"})

    def test_an_earlier_take_can_be_made_current_again(self):
        service.render_video(self.folder)
        (self.folder / "videos/s01.mp4").write_bytes(b"first take")
        service.render_video(self.folder, regenerate=["s01"])
        self.assertEqual((self.folder / "videos/s01_v1.mp4").read_bytes(), b"first take")
        (self.folder / "videos/s01.mp4").write_bytes(b"second take")
        calls = self.create.call_count
        use_take(self.folder, "s01", "s01_v1.mp4")
        self.assertEqual((self.folder / "videos/s01.mp4").read_bytes(), b"first take")
        self.assertEqual((self.folder / "videos/s01_v2.mp4").read_bytes(), b"second take")
        clip = read_json(self.folder / "videos/manifest.json")["clips"][0]
        self.assertEqual([take["file"] for take in clip["history"]], ["s01_v2.mp4"])
        self.assertEqual(clip["state"], "done")
        self.assertEqual(self.create.call_count, calls)
        with self.assertRaisesRegex(ValueError, "earlier takes"):
            use_take(self.folder, "s01", "../s01_v1.mp4")
        # A redo after switching never overwrites a kept take.
        service.render_video(self.folder, regenerate=["s01"])
        self.assertEqual(sorted(path.name for path in (self.folder / "videos").glob("s01_v*.mp4")), ["s01_v2.mp4", "s01_v3.mp4"])


if __name__ == "__main__":
    unittest.main()
