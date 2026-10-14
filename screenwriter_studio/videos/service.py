"""Paid video-clip jobs with durable checkpoints and explicit resume semantics."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
from pathlib import Path
import re
import threading
import time
import urllib.request

from screenwriter_studio.images.generator import (
    POLL_URL, JobFailed, _first_url, _request, _within, hosted_reference_url, submit_task,
)
from screenwriter_studio import settings as studio_settings
from screenwriter_studio.models import load_kie_api_key
from screenwriter_studio.videos.probe import check_cancel, validate_clip, validate_keyframes
from screenwriter_studio.videos.catalog import CARDS
from screenwriter_studio.videos.plan import build_plan, digest, file_digest, payload, read_json, write_json

POLL_INTERVAL = 5
MAX_WAIT = 1800
POLL_RETRIES = 3
USER_AGENT = "ScreenwriterStudio/1.0"


def download_video(url: str, path: Path, cancel=None) -> None:
    if not url.startswith("https://"):
        raise ValueError("Provider video URL must use HTTPS.")
    temporary = path.with_suffix(".mp4.part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as stream:
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
    failures = 0
    while time.monotonic() < deadline:
        check_cancel(cancel)
        try:
            reply = _within(60, lambda: _request(POLL_URL, key, query={"taskId": task_id}))
            if reply.get("code") != 200:
                raise RuntimeError(f"Video status unavailable: {reply.get('msg') or 'provider error'}")
        except (RuntimeError, OSError) as exc:
            # A status check never creates work, so a blip is retried before the clip pauses.
            failures += 1
            if failures > POLL_RETRIES:
                raise RuntimeError(f"Video status unavailable after {POLL_RETRIES} retries: {exc}") from exc
            cancel.wait(POLL_INTERVAL * failures)
            continue
        failures = 0
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
                 shots: list[str] | None = None, regenerate: list[str] | None = None,
                 prompts: dict[str, str] | None = None) -> dict:
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
            raise ValueError("Video work for this production is still running or still stopping (stopping can take "
                             "up to a minute while open requests finish). Try again shortly.") from exc
        saved_path = out / "manifest.json"
        saved = read_json(saved_path) if saved_path.exists() else None
        saved_plan = read_json(out / "jobs.json") if saved else None
        defaults = studio_settings.load()["video"]
        # A started production keeps its own choices; a new one follows the Idea-tab settings.
        mode = mode if mode is not None else (saved_plan or {}).get("mode", defaults["mode"])
        sound = sound if sound is not None else (saved_plan or {}).get("sound", defaults["sound"])
        selection = selection if selection is not None else (saved_plan or {}).get("selection", "kling" if saved_plan else defaults["selection"])
        plan = build_plan(folder, mode=mode, sound=sound, selection=selection, frozen_plan=saved_plan)
        fingerprint = plan_fingerprint(plan)
        if saved and saved.get("fingerprint") not in {fingerprint, digest(plan)}:
            raise ValueError("The screenplay or the video settings changed since these clips were started. "
                             "Saved clips belong to the original plan; start a new production for a different version.")
        if dry_run:
            if shots or regenerate or prompts:
                raise ValueError("A clip plan covers every shot; render or redo shots without --dry-run.")
            write_json(out / "jobs.preview.json", plan)
            return {"status": "planned", "plan": plan}
        if prompts:
            apply_prompts(plan, prompts, set(shots or []) | set(regenerate or []))
        # Validate every provider request before uploading images or submitting a task.
        for shot in plan["shots"]:
            payload(plan, shot, "https://preflight.invalid/frame.png")
        validate_keyframes(folder, plan, cancel)
        if saved and saved.get("fingerprint") != fingerprint:
            saved["fingerprint"] = fingerprint  # upgrade: a replaced shot image no longer locks the production
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
                kept = next_take(out, clip["id"])
                if path.is_file():
                    path.replace(kept)
                history = clip.get("history", []) + [{"file": kept.name, "task_id": clip.get("task_id"), "sha256": clip.get("sha256")}]
                for field in ("task_id", "url", "sha256", "request"):
                    clip.pop(field, None)
                clip.update(history=history, state="pending", error="")
        def work(shot, clip):
            check_cancel(cancel)
            path = out / f"{shot['id']}.mp4"
            if clip.get("state") == "done" and path.is_file():
                # A finished clip is final, even if you trimmed it in place; use redo to replace it.
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
                        checkpoint(clip, state="submitting", request=request, error="",
                                   image_sha256=shot.get("image_sha256"))
                        try:
                            # Late lookup keeps this module's request function the one in use.
                            reply = submit_task(key, request, send=lambda *a, **k: _request(*a, **k))
                            if reply.get("code") != 200:
                                raise JobFailed(str(reply.get("msg") or "Video submission rejected."))
                            task_id = (reply.get("data") or {}).get("taskId")
                            if not isinstance(task_id, str) or not task_id:
                                raise RuntimeError("Video submission returned no task ID.")
                        except JobFailed:
                            raise
                        except Exception:
                            checkpoint(clip, state="uncertain")
                            raise
                        checkpoint(clip, task_id=task_id, state="polling")
                    checkpoint(clip, state="polling", error="")
                    url = poll_task(key, clip["task_id"], cancel)
                    checkpoint(clip, url=url, state="downloading")
                check_cancel(cancel)
                download_video(clip["url"], path, cancel)
                try:
                    validate_clip(path, shot["duration_seconds"], cancel)
                except ValueError as exc:
                    # The clip is paid for and on disk: keep it viewable and let a resume try again.
                    rejected = out / f"{shot['id']}_rejected.mp4"
                    path.replace(rejected)
                    raise JobFailed(f"{exc} Kept as {rejected.name}; resuming submits a new take (paid).") from exc
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
            pool = ThreadPoolExecutor(max_workers=min(studio_settings.load()["video"]["parallel"], max(1, len(chosen))))
            try:
                futures = [pool.submit(work, shot, clip) for shot, clip in chosen]
                for future in futures:
                    try:
                        future.result()
                    except Exception as exc:
                        errors.append(exc)
            except BaseException:
                # Ctrl+C: stop queued clips from being submitted and let running ones stop polling.
                cancel.set()
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            pool.shutdown(wait=True)
            if errors:
                raise errors[0]
            check_cancel(cancel)
            if any(clip.get("state") != "done" for clip in manifest["clips"]):
                checkpoint(status="partial", error="")
                return manifest
            checkpoint(status="complete", error="")
        except BaseException as exc:
            checkpoint(status="paused", error=str(exc) or "Stopped.")
            raise
        return manifest


def attach_task(folder: Path, shot_id: str, task_id: str) -> None:
    """Recover an uncertain submission after the user checks their provider history."""
    if not task_id.strip() or len(task_id) > 200:
        raise ValueError("A provider task ID is required.")
    with _uncertain_clip(folder, shot_id) as (manifest, clip, save):
        clip.update(task_id=task_id.strip(), state="polling", error="")
        save()


def forget_submission(folder: Path, shot_id: str) -> None:
    """Mark an uncertain submission as never created, after checking KIE's task history.

    The next run submits that shot again. If KIE did create the first task, it is
    billed too, so only do this when the history shows no task for the shot.
    """
    with _uncertain_clip(folder, shot_id) as (manifest, clip, save):
        attempts = clip.get("attempts", []) + [{"task_id": None, "error": "Submission forgotten by the user."}]
        clip.update(state="failed", attempts=attempts, error="Not submitted; the next run sends it again.")
        save()


class _uncertain_clip:
    def __init__(self, folder: Path, shot_id: str) -> None:
        self.out = Path(folder) / "videos"
        self.shot_id = shot_id

    def __enter__(self):
        self.lock = (self.out / ".generation.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise ValueError("This production has video work running. Stop it first.") from exc
        manifest = read_json(self.out / "manifest.json")
        clip = next((c for c in manifest["clips"] if c["id"] == self.shot_id), None)
        if not clip or clip.get("task_id") or clip.get("state") not in {"submitting", "uncertain"}:
            self.lock.close()
            raise ValueError("Only an uncertain submission without a task ID can be recovered.")
        return manifest, clip, lambda: write_json(self.out / "manifest.json", manifest)

    def __exit__(self, *exc) -> None:
        self.lock.close()


def plan_fingerprint(plan: dict) -> str:
    """What locks a production's clips: screenplay, settings and planned prompts, not each image's bytes.

    Replacing one shot's image must not freeze every other clip; that shot's clip
    records the image it was made from instead. A prompt you edited counts as the
    planned prompt it replaced, so editing one shot never locks the others.
    """
    def planned(shot: dict) -> dict:
        kept = {k: v for k, v in shot.items() if k not in {"image_sha256", "original_prompt"}}
        if "original_prompt" in shot:
            kept["prompt"] = shot["original_prompt"]
        return kept
    return digest({**plan, "shots": [planned(shot) for shot in plan["shots"]]})


def apply_prompts(plan: dict, prompts: dict, wanted: set[str]) -> None:
    """Put your edited prompts on the shots being rendered or redone. The planned prompt is kept beside it."""
    if plan.get("version") == 1:
        raise ValueError("This older Kling plan cannot take edited prompts.")
    if not isinstance(prompts, dict) or set(prompts) - wanted:
        raise ValueError("Edit a prompt only for a shot you render or redo.")
    for shot in plan["shots"]:
        text = prompts.get(shot["id"])
        if text is None:
            continue
        card = CARDS.get(shot.get("model", plan.get("model")))
        text = text.strip() if isinstance(text, str) else ""
        if card is None or not 0 < len(text) <= card.prompt_max:
            limit = card.prompt_max if card else 2500
            raise ValueError(f"{shot['id']}: the prompt must be 1–{limit} characters.")
        if text != shot["prompt"]:
            shot.setdefault("original_prompt", shot["prompt"])
            shot["prompt"] = text


def next_take(out: Path, shot_id: str) -> Path:
    """The next unused name for an earlier take, e.g. s02_v3.mp4."""
    used = [int(match.group(1)) for path in out.glob(f"{shot_id}_v*.mp4")
            if (match := re.fullmatch(rf"{re.escape(shot_id)}_v(\d+)\.mp4", path.name))]
    return out / f"{shot_id}_v{max(used, default=0) + 1}.mp4"


def use_take(folder: Path, shot_id: str, name: str) -> dict:
    """Make an earlier take the clip again. The current take is kept as an earlier take. No provider call."""
    out = Path(folder) / "videos"
    if not re.fullmatch(rf"{re.escape(shot_id)}_v\d+\.mp4", name or ""):
        raise ValueError("Choose one of this shot's earlier takes.")
    with (out / ".generation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("This production has video work running. Stop it first.") from exc
        manifest = read_json(out / "manifest.json")
        clip = next((c for c in manifest.get("clips", []) if c.get("id") == shot_id), None)
        if clip is None or clip.get("state") in {"submitting", "uncertain", "polling", "downloading"}:
            raise ValueError("This clip is still being made. Wait for it to finish first.")
        entry = next((take for take in clip.get("history", []) if take.get("file") == name), None)
        chosen = out / name
        if entry is None or not chosen.is_file():
            raise ValueError("That take is no longer on disk.")
        history = [take for take in clip["history"] if take is not entry]
        current = out / f"{shot_id}.mp4"
        if current.is_file():
            kept = next_take(out, shot_id)
            current.replace(kept)
            history.append({"file": kept.name, "task_id": clip.get("task_id"), "sha256": clip.get("sha256")})
        chosen.replace(current)
        for field in ("url", "request"):
            clip.pop(field, None)
        clip.update(history=history, state="done", error="", task_id=entry.get("task_id"),
                    sha256=entry.get("sha256") or file_digest(current))
        if manifest.get("status") not in {"running"}:
            manifest["status"] = "complete" if all(c.get("state") == "done" for c in manifest["clips"]) else "partial"
        write_json(out / "manifest.json", manifest)
        return manifest
