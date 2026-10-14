"""Paid video jobs with durable checkpoints and explicit resume semantics."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
from pathlib import Path
import threading
import time
import urllib.request

from screenwriter_studio.images.generator import (
    CREATE_URL, POLL_URL, JobFailed, _first_url, _request, _within, hosted_reference_url,
)
from screenwriter_studio.models import load_kie_api_key
from screenwriter_studio.videos.assembly import (
    assemble, check_cancel, require_tools, validate_clip, validate_keyframes,
)
from screenwriter_studio.videos.plan import build_plan, digest, file_digest, payload, read_json, write_json

POLL_INTERVAL = 5
MAX_WAIT = 1800
MAX_PARALLEL = 3


def download_video(url: str, path: Path, cancel=None) -> None:
    if not url.startswith("https://"):
        raise ValueError("Provider video URL must use HTTPS.")
    temporary = path.with_suffix(".mp4.part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as stream:
            size = 0
            deadline = time.monotonic() + 600
            while True:
                check_cancel(cancel)
                if time.monotonic() > deadline:
                    raise TimeoutError("Video download timed out; resume to retry the saved URL.")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 2_000_000_000:
                    raise ValueError("Provider clip exceeds the 2 GB download limit.")
                stream.write(chunk)
        if not size:
            raise ValueError("Provider returned an empty video.")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def poll_task(key: str, task_id: str, cancel) -> str:
    deadline = time.monotonic() + MAX_WAIT
    while time.monotonic() < deadline:
        check_cancel(cancel)
        reply = _within(60, lambda: _request(POLL_URL, key, query={"taskId": task_id}))
        if reply.get("code") != 200:
            raise RuntimeError(f"Video status unavailable: {reply.get('msg') or 'provider error'}")
        data = reply.get("data") or {}
        if data.get("state") == "success":
            url = _first_url(data.get("resultJson"))
            if not url:
                raise RuntimeError("Completed video task has no result URL. Resume to poll again.")
            return url
        if data.get("state") == "fail":
            raise JobFailed(str(data.get("failMsg") or "Video provider task failed."))
        cancel.wait(POLL_INTERVAL)
    raise RuntimeError("Video task is still pending. Resume to continue polling the saved task.")


def render_video(folder: Path, *, mode: str | None = None, sound: bool | None = None,
                 selection: str | None = None, dry_run: bool = False, on_update=None, cancel=None,
                 shots: list[str] | None = None, regenerate: list[str] | None = None) -> dict:
    folder = folder.resolve()
    cancel = cancel or threading.Event()
    out = folder / "videos"
    if out.resolve().parent != folder:
        raise ValueError("Video output must stay inside this production.")
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".generation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("This production already has video work running.") from exc
        saved_path = out / "manifest.json"
        saved = read_json(saved_path) if saved_path.exists() else None
        saved_plan = read_json(out / "jobs.json") if saved else None
        mode = mode if mode is not None else (saved_plan or {}).get("mode", "pro")
        sound = sound if sound is not None else (saved_plan or {}).get("sound", True)
        selection = selection if selection is not None else (saved_plan or {}).get("selection", "kling" if saved_plan else "auto")
        plan = build_plan(folder, mode=mode, sound=sound, selection=selection, frozen_plan=saved_plan)
        fingerprint = digest(plan)
        if saved and saved.get("fingerprint") != fingerprint:
            raise ValueError("The screenplay, keyframes, or video settings changed. Saved video work belongs to the original plan; use a new production for a different version.")
        if dry_run:
            write_json(out / "jobs.preview.json", plan)
            return {"status": "planned", "plan": plan}
        require_tools()
        # Validate every provider request before uploading images or submitting a task.
        for shot in plan["shots"]:
            payload(plan, shot, "https://preflight.invalid/frame.png")
        validate_keyframes(folder, plan, cancel)
        manifest = saved or {"version": 1, "fingerprint": fingerprint, "clips": [
            {"id": shot["id"], "model": shot.get("model", plan.get("model")), "state": "pending"} for shot in plan["shots"]]}
        expected = [shot["id"] for shot in plan["shots"]]
        if [clip.get("id") for clip in manifest.get("clips", [])] != expected:
            raise ValueError("Video manifest does not match the shot list.")
        wanted = set(shots or []) | set(regenerate or [])
        if wanted - set(expected):
            raise ValueError("Unknown shot: " + ", ".join(sorted(wanted - set(expected))))
        if set(regenerate or []) - {c["id"] for c in manifest["clips"] if c.get("state") == "done"}:
            raise ValueError("Only a finished clip can be regenerated.")
        write_json(out / "jobs.json", plan)
        guard = threading.Lock()

        def checkpoint(clip=None, **changes):
            # Workers share the manifest; mutate and write under one lock.
            with guard:
                (clip if clip is not None else manifest).update(changes)
                write_json(saved_path, manifest)
                if on_update:
                    on_update(manifest)

        for clip in manifest["clips"]:
            if clip["id"] in set(regenerate or []):
                # Keep the old take on disk; the new one replaces it in the cut.
                path = out / f"{clip['id']}.mp4"
                version = len(clip.get("history", [])) + 1
                kept = out / f"{clip['id']}_v{version}.mp4"
                if path.is_file():
                    path.replace(kept)
                history = clip.get("history", []) + [{"file": kept.name, "task_id": clip.get("task_id"), "sha256": clip.get("sha256")}]
                for field in ("task_id", "url", "sha256", "request"):
                    clip.pop(field, None)
                clip.update(history=history, state="pending", error="")
        if not regenerate and saved and saved.get("status") == "complete":
            final = out / "final.mp4"
            if final.is_file() and file_digest(final) == saved.get("final_sha256"):
                return saved

        def work(shot, clip):
            check_cancel(cancel)
            path = out / f"{shot['id']}.mp4"
            if clip.get("state") == "done" and path.is_file() and file_digest(path) == clip.get("sha256"):
                return
            # A lost submit response cannot be retried safely: the first request may be billable.
            if clip.get("state") in {"submitting", "uncertain"} and not clip.get("task_id"):
                raise ValueError(f"{shot['id']}: submission outcome unknown. Check KIE's task history and attach its task ID before resuming (see recovery docs).")
            if clip.get("state") == "failed":
                attempts = clip.get("attempts", []) + [{"task_id": clip.get("task_id"), "error": clip.get("error")}]
                checkpoint(clip, attempts=attempts, task_id=None, url=None, state="pending")
            try:
                if not clip.get("url"):
                    key = load_kie_api_key()
                    if not key:
                        raise ValueError("KIE_API_KEY is not set. Add it to .env before generating video.")
                    if not clip.get("task_id"):
                        image_url = hosted_reference_url(key, folder / shot["image"])
                        check_cancel(cancel)
                        request = payload(plan, shot, image_url)
                        checkpoint(clip, state="submitting", request=request, error="")
                        try:
                            reply = _within(60, lambda: _request(CREATE_URL, key, payload=request))
                            if reply.get("code") != 200:
                                raise JobFailed(str(reply.get("msg") or "Video submission rejected."))
                            task_id = (reply.get("data") or {}).get("taskId")
                            if not isinstance(task_id, str) or not task_id:
                                raise RuntimeError("Video submission returned no task ID.")
                        except JobFailed:
                            raise
                        except Exception:
                            clip["state"] = "uncertain"
                            raise
                        checkpoint(clip, task_id=task_id, state="polling")
                    checkpoint(clip, state="polling", error="")
                    url = poll_task(key, clip["task_id"], cancel)
                    checkpoint(clip, url=url, state="downloading")
                check_cancel(cancel)
                download_video(clip["url"], path, cancel)
                validate_clip(path, shot["duration_seconds"], cancel)
                checkpoint(clip, state="done", sha256=file_digest(path), error="")
            except Exception as exc:
                checkpoint(clip, **({"state": "failed"} if isinstance(exc, JobFailed) else {}), error=str(exc))
                raise

        checkpoint(status="running", error="")
        try:
            chosen = [(shot, clip) for shot, clip in zip(plan["shots"], manifest["clips"])
                      if not wanted or shot["id"] in wanted]
            # Clips are independent paid jobs: submit and poll them side by side.
            # Every worker finishes (or fails) before the first error is raised,
            # so no already-paid task is abandoned mid-poll.
            errors = []
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, max(1, len(chosen)))) as pool:
                futures = [pool.submit(work, shot, clip) for shot, clip in chosen]
                for future in futures:
                    try:
                        future.result()
                    except Exception as exc:
                        errors.append(exc)
            if errors:
                raise errors[0]
            check_cancel(cancel)
            if any(clip.get("state") != "done" for clip in manifest["clips"]):
                checkpoint(status="partial", error="")
                return manifest
            checkpoint(status="assembling")
            final = assemble(folder, plan, cancel)
            checkpoint(status="complete", final_sha256=file_digest(final), error="")
        except Exception as exc:
            checkpoint(status="paused", error=str(exc))
            raise
        return manifest


def attach_task(folder: Path, shot_id: str, task_id: str) -> None:
    """Recover an uncertain submission after the user checks their provider history."""
    if not task_id.strip() or len(task_id) > 200:
        raise ValueError("A provider task ID is required.")
    out = folder / "videos"
    with (out / ".generation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = read_json(out / "manifest.json")
        clip = next((c for c in manifest["clips"] if c["id"] == shot_id), None)
        if not clip or clip.get("task_id") or clip.get("state") not in {"submitting", "uncertain"}:
            raise ValueError("Only an uncertain submission without a task ID can be recovered.")
        clip.update(task_id=task_id.strip(), state="polling", error="")
        write_json(out / "manifest.json", manifest)
