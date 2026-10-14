"""Durable image approval, bounded repairs, edited takes and version choice, called under the batch folder lock."""

from __future__ import annotations

import fcntl
import json
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from uuid import uuid4

from screenwriter_studio.images.inspection import Inspection, inspect_image, normalize_review
from screenwriter_studio.models import ART_MODEL

MAX_REPAIRS = 2
MAX_PROMPT = 20000
ACTIONS = ("repair", "keep", "inspect", "regenerate")
# A record in one of these states has a provider task or an inspection under way.
IN_FLIGHT = {"queued", "submitting", "polling", "downloading", "inspecting", "running"}
# Fields a record keeps when it is marked blocked again: earlier versions and the edited prompt.
PERSISTENT = ("history", "job_override", "take")
_live_lock = threading.Lock()
_live_keep: dict[str, object] = {}


def effective_job(job: dict, record: dict | None) -> dict:
    """The planned job with the prompt and file name of the take you asked for, if any."""
    override = (record or {}).get("job_override")
    if not isinstance(override, dict):
        return job
    return {**job, **{key: value for key, value in override.items() if key in {"prompt", "filename"}}}


def repair_job(job, record, out):
    from screenwriter_studio.images.generator import _destination, _ref_list
    review = record["inspection"]
    prompt = ("Edit the first image with only these corrections:\n"
              + "\n".join("- " + item for item in review["changes"])
              + "\nPreserve:\n" + "\n".join("- " + item for item in review["preserve"])
              + "\nPreserve all other correct details, identity, composition and style. "
              "Additional images are continuity references.\nOriginal requirements:\n" + job["prompt"])
    dest = _destination(out, job)
    result = {"id": job["id"], "kind": "edit", "prompt": prompt,
              "filename": f"{dest.stem}.repair-{record.get('repairs', 0) + 1}{dest.suffix}",
              "references": [record["url"], *_ref_list(job)]}
    if job.get("aspect_ratio"):
        result["aspect_ratio"] = job["aspect_ratio"]  # a 3:4 character sheet stays 3:4 after a repair
    return result


def _saved_hold(record) -> bool:
    """A review whose file is already on disk, even if inspection never parsed."""
    path = record.get("path")
    return record.get("state") in {"review", "inspecting"} and isinstance(path, str) and Path(path).is_file() and bool(record.get("url"))


def validate_decision(records, decision):
    if not isinstance(decision, dict) or decision.get("action") not in ACTIONS:
        raise ValueError("Choose repair, keep, inspect, or regenerate.")
    if not isinstance(decision.get("id"), str):
        raise ValueError("Image ID must be a string.")
    record = records.get(decision.get("id"))
    if not isinstance(record, dict):
        raise ValueError("This image has not started yet. Resume the image work first.")
    if decision["action"] == "regenerate":
        prompt = decision.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Write a prompt for the new image.")
        if len(prompt) > MAX_PROMPT:
            raise ValueError(f"Prompts are limited to {MAX_PROMPT} characters.")
        if record.get("state") in IN_FLIGHT:
            raise ValueError("This image is still being made. Wait for it to finish, or stop the run first.")
        return record
    token = decision.get("review_id")
    saved = record.get("review_id")
    # Abort accepts a saved hold when the review token is missing or the
    # inspection payload never validated. A conflicting token is still stale.
    if decision["action"] == "keep" and _saved_hold(record) and not (token and saved and token != saved):
        return record
    if record.get("state") != "review" or not token or token != saved:
        raise ValueError("This review has changed. Refresh before choosing.")
    if decision["action"] == "repair" and not record.get("repair_job"):
        raise ValueError("No executable repair is available for this review.")
    return record


def register_live_keep(out, fn) -> None:
    with _live_lock:
        _live_keep[str(Path(out).resolve())] = fn


def unregister_live_keep(out) -> None:
    with _live_lock:
        _live_keep.pop(str(Path(out).resolve()), None)


def apply_live_keep(out, decision):
    """Apply Abort on the batch that already holds this folder, if one is running."""
    with _live_lock:
        fn = _live_keep.get(str(Path(out).resolve()))
    if fn is None:
        return None
    return fn(decision)


def _release_blocked(records, keep):
    """Drop finished images from later blockers without submitting new provider work."""
    for other in list(records.values()):
        if not isinstance(other, dict) or other.get("state") != "blocked":
            continue
        blocked_by = list(other.get("blocked_by") or [])
        released = [ref for ref in blocked_by if records.get(ref, {}).get("state") != "done"]
        if released == blocked_by:
            continue
        nxt = dict(other)
        if released:
            nxt["blocked_by"] = released
            nxt["state"] = "blocked"
        else:
            nxt.pop("blocked_by", None)
            nxt["state"] = "planned"
        keep(nxt)


def _parallel() -> int:
    try:
        from screenwriter_studio.settings import load
        return int(load()["images"]["parallel"])
    except (KeyError, TypeError, ValueError):
        return 3


def run_reviewed(spec, out, ordered, records, api_key, checkpoint, keep,
                 on_start, cancel, decision=None, only=None):
    from screenwriter_studio.images.generator import _destination, choose, job_aspect
    aspect = spec.get("aspect_ratio", "16:9")
    resolution = spec.get("resolution", "1K")
    settings = spec["inspection"]
    mode = settings["mode"]
    urls = {}
    jobs = {job["id"]: job for wave in ordered for job in wave}

    def stopped():
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Generation cancelled. Submitted tasks are saved for resume.")

    def replace(record):
        # An explicit human choice may replace an accepted image; checkpoint otherwise protects it.
        records.pop(record["id"], None)
        checkpoint(record)

    def begin_repair(record, actor):
        # Save the exact approved request before any paid submission. Resume uses it verbatim.
        history = [*record.get("history", []),
                   {key: value for key, value in record.items() if key != "history"}]
        next_record = {"id": record["id"], "kind": record["kind"],
                       "repairs": record.get("repairs", 0) + 1,
                       "history": history, "generation_job": record["repair_job"],
                       "decision": actor, "state": "queued"}
        for key in ("job_override", "take"):
            if key in record:
                next_record[key] = record[key]
        checkpoint(next_record)
        return next_record

    def begin_regenerate(record, prompt):
        """A new take from an edited prompt. Every earlier version stays on disk and can be chosen again."""
        job = jobs.get(record["id"])
        if job is None:
            raise ValueError("This image is not in the saved plan.")
        take = int(record.get("take") or 1) + 1
        base = _destination(out, job)
        while True:
            filename = f"{base.stem}.take-{take}{base.suffix}"
            if not (out / filename).exists():
                break
            take += 1
        edited = {**job, "prompt": prompt, "filename": filename}
        model, _ = choose(edited, aspect, resolution)
        from screenwriter_studio.images.catalog import prompt_length_error
        problem = prompt_length_error(model, prompt)
        if problem:
            raise ValueError(problem)
        history = list(record.get("history", []))
        if record.get("path") and Path(record["path"]).is_file():
            history.append({key: value for key, value in record.items() if key != "history"})
        next_record = {"id": record["id"], "kind": record.get("kind") or job.get("kind"), "history": history,
                       "take": take, "job_override": {"prompt": prompt, "filename": filename},
                       "repairs": 0, "decision": "human edited prompt", "state": "queued"}
        replace(next_record)
        return next_record

    if decision:
        record = dict(validate_decision(records, decision))
        stopped()
        if decision["action"] == "keep":
            if not Path(record.get("path", "")).is_file() or not record.get("url"):
                raise ValueError("The reviewed image is missing; it cannot be accepted.")
            record.update(state="done", decision="human accepted", error="")
            checkpoint(record)
        elif decision["action"] == "repair":
            begin_repair(record, "human approved repair")
        elif decision["action"] == "regenerate":
            begin_regenerate(record, decision["prompt"].strip())
            # A new take spends credits on this image only; other unfinished work waits for Resume.
            only = {record["id"]} if only is None else only
        else:
            record.pop("inspection", None)
            record.pop("repair_job", None)
            record.update(state="inspecting", decision="human requested inspection")
            checkpoint(record)

    def accept_keep(choice):
        if not isinstance(choice, dict) or choice.get("action") != "keep":
            raise ValueError("Wait for the current run to finish before reviewing images.")
        current = dict(validate_decision(records, choice))
        if not Path(current.get("path", "")).is_file() or not current.get("url"):
            raise ValueError("The reviewed image is missing; it cannot be accepted.")
        current.update(state="done", decision="human accepted", error="")
        keep(current)
        _release_blocked(records, keep)
        return current

    register_live_keep(out, accept_keep)
    try:
        _run_review_loop(spec, out, ordered, records, api_key, checkpoint, keep, on_start, cancel,
                         urls, stopped, begin_repair, aspect, resolution, settings, mode, only, job_aspect)
    finally:
        unregister_live_keep(out)


def _run_review_loop(spec, out, ordered, records, api_key, checkpoint, keep, on_start, cancel,
                     urls, stopped, begin_repair, aspect, resolution, settings, mode, only, job_aspect):
    """Run every ready image side by side; an image starts as soon as the images it references are done.

    An image waiting for review only holds back the images that use it. Every worker
    finishes before an error is raised, so no paid task is abandoned mid-poll.
    """
    from screenwriter_studio.images.generator import _ref_list, locked_files, style_reference_ids
    locked = locked_files(spec)
    styles = style_reference_ids(spec)
    pending = [job for wave in ordered for job in wave]
    running: dict = {}

    def resolved(ref: str) -> bool:
        if ref.startswith(("https://", "http://")) or ref in locked or ref in urls:
            return True
        saved = records.get(ref, {})
        if saved.get("state") == "done" and saved.get("url"):
            urls[ref] = saved["url"]
            return True
        return False

    def submitted(record: dict) -> bool:
        # A paid task already exists; its record must never be rewritten as blocked.
        return bool(record.get("task_id") or record.get("url")) or record.get("state") == "submitting"

    def mark_blocked(job: dict, record: dict, missing: list[str]) -> None:
        if submitted(record) and record.get("state") != "done":
            keep(dict(record))
            return
        carried = {key: record[key] for key in PERSISTENT if key in record}
        keep({**carried, "id": job["id"], "kind": job["kind"], "state": "blocked", "blocked_by": missing})

    def schedule(pool) -> None:
        for job in list(pending):
            record = dict(records.get(job["id"], {}))
            state = record.get("state")
            if only is not None and job["id"] not in only:
                pending.remove(job)  # already saved; this run leaves it as it is
                continue
            if state == "done":
                if not Path(record.get("path", "")).is_file():
                    raise ValueError(f"Accepted image {job['id']} is missing. Restore it before resuming.")
                urls[job["id"]] = record["url"]
                keep(record)
                pending.remove(job)
                continue
            if state == "review":
                keep(record)
                pending.remove(job)
                continue
            missing = [ref for ref in _ref_list(effective_job(job, record)) if not resolved(ref)]
            if not missing:
                pending.remove(job)
                running[pool.submit(work, job, record, dict(urls))] = job
                continue
            busy = {item["id"] for item in pending} | {item["id"] for item in running.values()}
            if not any(ref in busy for ref in missing):
                mark_blocked(job, record, missing)

    def work(job: dict, record: dict, known: dict) -> dict:
        from screenwriter_studio.images.generator import build_payload, card_for, choose, prompt_length_error, run_job
        stopped()
        planned = effective_job(job, record)
        while True:
            stopped()
            current_job = record.get("generation_job") or planned
            if record.get("state") != "inspecting":
                def save_generation(value):
                    value["kind"] = job["kind"]
                    checkpoint(value)

                record = run_job(api_key, current_job, aspect, resolution, out, dict(known),
                                 False, on_start, previous=record,
                                 checkpoint=save_generation, cancel=cancel, locked=locked)
                record["kind"] = job["kind"]
                if record.get("error"):
                    keep(record)
                    return record
                record["state"] = "inspecting"
                keep(record)
            saved = records.get(job["id"], {})
            if saved.get("state") == "done" and saved.get("decision") == "human accepted":
                record = dict(saved)
                keep(record)
                return record
            stopped()
            references = []
            for ref in _ref_list(planned):
                if ref.startswith(("https://", "http://")):
                    references.append((ref, ref))
                elif ref in styles:
                    references.append((f"{ref} (style only: compare grade, light and texture, not people, places or composition)", locked[ref]))
                elif ref in locked:
                    references.append((ref, locked[ref]))
                else:
                    references.append((ref, Path(records[ref]["path"])))
            try:
                review = inspect_image(record, planned, spec.get("inspection_context", {}),
                                       references, api_key=api_key,
                                       model=settings.get("model", ART_MODEL),
                                       effort=settings.get("effort"))
                review = Inspection.model_validate(normalize_review(review)).model_dump()
            except Exception as exc:
                message = str(exc).replace(api_key, "[redacted]") if api_key else str(exc)
                review = {"verdict": "review", "summary": "Inspection unavailable. Human review required.",
                          "findings": [], "changes": [], "preserve": [], "uncertainty": message[:1000]}
            saved = records.get(job["id"], {})
            if saved.get("state") == "done" and saved.get("decision") == "human accepted":
                record = dict(saved)
                keep(record)
                return record
            record.update(inspection=review, review_id=uuid4().hex,
                          inspection_model=settings.get("model", ART_MODEL),
                          state="done" if review["verdict"] == "accept" else "review")
            record.pop("repair_job", None)
            record.pop("repair_unavailable", None)
            if review["changes"] and record.get("repairs", 0) < MAX_REPAIRS:
                try:
                    proposed = repair_job(planned, record, out)
                    model, _ = choose(proposed, aspect, resolution)
                    refs = []
                    for ref in _ref_list(proposed):
                        if ref.startswith(("http://", "https://")):
                            refs.append(ref)
                        elif ref in known:
                            refs.append(known[ref])
                        elif ref in locked:
                            refs.append(str(locked[ref]))
                        else:
                            raise KeyError(ref)
                    if len(refs) > card_for(model).max_images:
                        raise ValueError("Repair exceeds the model's reference limit.")
                    length_error = prompt_length_error(model, proposed["prompt"])
                    if length_error:
                        raise ValueError(length_error)
                    build_payload(model, proposed["prompt"], job_aspect(proposed, aspect), resolution, refs)
                    proposed["model"] = model
                    record["repair_job"] = proposed
                except (ValueError, KeyError) as exc:
                    record["repair_unavailable"] = str(exc)
            elif record.get("repairs", 0) >= MAX_REPAIRS:
                record["repair_unavailable"] = "Two-repair limit reached. Keep this version or leave it for review."
            checkpoint(record)
            stopped()
            if (mode == "auto" and review["verdict"] == "repair"
                    and not review["uncertainty"].strip() and record.get("repair_job")):
                record = begin_repair(record, "auto approved repair")
                continue
            keep(record)
            return record

    first_error = None
    pool = ThreadPoolExecutor(max_workers=max(1, _parallel()))
    try:
        while True:
            if first_error is None and not (cancel is not None and cancel.is_set()):
                schedule(pool)
            if not running:
                break
            finished, _ = wait(list(running), timeout=0.5, return_when=FIRST_COMPLETED)
            for future in finished:
                job = running.pop(future)
                try:
                    record = future.result()
                except Exception as exc:
                    first_error = first_error or exc
                    continue
                if record.get("state") == "done" and record.get("url"):
                    urls[job["id"]] = record["url"]
    except BaseException:
        # Ctrl+C or an unreadable plan: start nothing new and let running images reach a checkpoint.
        if cancel is not None:
            cancel.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    if first_error is not None:
        raise first_error
    stopped()
    for job in pending:
        record = dict(records.get(job["id"], {}))
        missing = [ref for ref in _ref_list(effective_job(job, record)) if not resolved(ref)]
        if missing:
            mark_blocked(job, record, missing)


class _image_record:
    """One image's saved record, edited under the folder lock while no batch runs."""

    def __init__(self, out: Path, image_id: str) -> None:
        self.out = Path(out)
        self.image_id = image_id

    def __enter__(self):
        if not isinstance(self.image_id, str) or not self.image_id:
            raise ValueError("Choose an image.")
        self.lock = (self.out / ".generation.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise ValueError("Image work for this production is running. Stop it first.") from exc
        try:
            self.manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.lock.close()
            raise ValueError("This production has no saved image work.") from exc
        images = self.manifest.get("images") if isinstance(self.manifest.get("images"), list) else []
        self.records = {record["id"]: record for record in images if isinstance(record, dict) and isinstance(record.get("id"), str)}
        record = self.records.get(self.image_id)
        if record is None:
            self.lock.close()
            raise ValueError("That image is not in this production.")
        return record

    def save(self, record: dict) -> dict:
        self.records[record["id"]] = record
        _release_blocked(self.records, lambda value: self.records.__setitem__(value["id"], value))
        self.manifest["images"] = [self.records.get(item.get("id"), item) for item in self.manifest["images"]
                                   if isinstance(item, dict)]
        states = [item.get("state") for item in self.manifest["images"]]
        self.manifest["status"] = "complete" if states and all(state == "done" for state in states) else "paused"
        temporary = self.out / "manifest.tmp"
        temporary.write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.out / "manifest.json")
        return self.manifest

    def __exit__(self, *exc) -> None:
        self.lock.close()


def _uncertain(record: dict) -> bool:
    return record.get("state") == "submitting" and not record.get("task_id")


def attach_image_task(out: Path, image_id: str, task_id: str) -> dict:
    """Recover an uncertain submission with the task ID found in KIE's task history; resume polls it."""
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
        raise ValueError("A KIE task ID is required.")
    context = _image_record(out, image_id)
    with context as record:
        if not _uncertain(record):
            raise ValueError("Only an image whose submission is uncertain can take a task ID.")
        return context.save({**record, "task_id": task_id.strip(), "state": "polling", "error": ""})


def forget_image_submission(out: Path, image_id: str) -> dict:
    """Mark an uncertain submission as never created, after checking KIE's task history.

    The next resume submits it again. If KIE did create the first task, both are billed.
    """
    context = _image_record(out, image_id)
    with context as record:
        if not _uncertain(record):
            raise ValueError("Only an image whose submission is uncertain can be sent again.")
        attempts = [*record.get("attempts", []), {"task_id": None, "error": "Submission forgotten by the user."}]
        return context.save({**record, "state": "failed", "attempts": attempts,
                             "error": "Not submitted; resume sends it again."})


def versions(record: dict) -> list[dict]:
    """Earlier versions of an image that are still on disk, newest first, one per file."""
    seen, found = {record.get("path")}, []
    for entry in reversed(record.get("history") or []):
        path = entry.get("path") if isinstance(entry, dict) else None
        if not path or path in seen or not Path(path).is_file() or not entry.get("url"):
            continue
        seen.add(path)
        found.append(entry)
    return found


def restore_version(out: Path, image_id: str, name: str) -> dict:
    """Make an earlier version (an original, a repair or another take) the image again. No provider call."""
    context = _image_record(out, image_id)
    with context as record:
        if record.get("state") in IN_FLIGHT:
            raise ValueError("This image is still being made. Wait for it to finish first.")
        chosen = next((entry for entry in versions(record) if Path(entry["path"]).name == name), None)
        if chosen is None:
            raise ValueError("That version is no longer on disk.")
        history = [entry for entry in record.get("history") or [] if entry is not chosen]
        if record.get("path") and Path(record["path"]).is_file():
            history.append({key: value for key, value in record.items() if key != "history"})
        restored = {key: value for key, value in chosen.items() if key not in {"repair_job", "repair_unavailable", "blocked_by"}}
        restored.update(history=history, state="done", error="", decision="human chose this version",
                        take=max(int(record.get("take") or 1), int(chosen.get("take") or 1)))
        return context.save(restored)
