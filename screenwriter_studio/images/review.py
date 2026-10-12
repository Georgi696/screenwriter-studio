"""Durable image approval and bounded repairs, called under the batch folder lock."""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

from screenwriter_studio.images.inspection import Inspection, inspect_image
from screenwriter_studio.models import ART_MODEL

MAX_REPAIRS = 2
_live_lock = threading.Lock()
_live_keep: dict[str, object] = {}


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
    return result


def _saved_hold(record) -> bool:
    """A review whose file is already on disk, even if inspection never parsed."""
    path = record.get("path")
    return record.get("state") in {"review", "inspecting"} and isinstance(path, str) and Path(path).is_file() and bool(record.get("url"))


def validate_decision(records, decision):
    if not isinstance(decision, dict) or decision.get("action") not in ("repair", "keep", "inspect"):
        raise ValueError("Choose repair, keep, or inspect.")
    if not isinstance(decision.get("id"), str):
        raise ValueError("Image ID must be a string.")
    record = records.get(decision.get("id"))
    if not isinstance(record, dict):
        raise ValueError("This review has changed. Refresh before choosing.")
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
    """Drop a kept image from later blockers without submitting new provider work."""
    for other in list(records.values()):
        if not isinstance(other, dict) or other.get("state") != "blocked":
            continue
        blocked_by = list(other.get("blocked_by") or [])
        released = [
            ref for ref in blocked_by
            if not (records.get(ref, {}).get("state") == "done" and records.get(ref, {}).get("decision") == "human accepted")
        ]
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


def run_reviewed(spec, out, ordered, records, api_key, checkpoint, keep,
                 on_start, cancel, decision=None):
    from screenwriter_studio.images.generator import (
        run_job, _ref_list, choose, build_payload, card_for, prompt_length_error,
    )
    aspect = spec.get("aspect_ratio", "16:9")
    resolution = spec.get("resolution", "1K")
    settings = spec["inspection"]
    mode = settings["mode"]
    urls = {}

    def stopped():
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Generation cancelled. Submitted tasks are saved for resume.")

    def begin_repair(record, actor):
        # Save the exact approved request before any paid submission. Resume uses it verbatim.
        history = [*record.get("history", []),
                   {key: value for key, value in record.items() if key != "history"}]
        next_record = {"id": record["id"], "kind": record["kind"],
                       "repairs": record.get("repairs", 0) + 1,
                       "history": history, "generation_job": record["repair_job"],
                       "decision": actor, "state": "queued"}
        checkpoint(next_record)
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
        _run_review_loop(spec, out, ordered, records, api_key, checkpoint, keep, on_start, cancel, decision, urls, stopped, begin_repair, aspect, resolution, settings, mode)
    finally:
        unregister_live_keep(out)


def _run_review_loop(spec, out, ordered, records, api_key, checkpoint, keep, on_start, cancel, decision, urls, stopped, begin_repair, aspect, resolution, settings, mode):
    from screenwriter_studio.images.generator import (
        run_job, _ref_list, choose, build_payload, card_for, prompt_length_error,
    )
    if decision is None and any(isinstance(record, dict) and record.get("state") == "review" for record in records.values()):
        # Reviews are already waiting. Returning lets the desk clear its running
        # flag instead of polling or inspecting inside this same call.
        for record in list(records.values()):
            keep(dict(record))
        return

    for wave in ordered:
        for job in wave:
            stopped()
            record = dict(records.get(job["id"], {}))
            missing = [ref for ref in _ref_list(job) if not ref.startswith(("https://", "http://")) and ref not in urls]
            if missing:
                unresolved = [
                    ref for ref in missing
                    if not (records.get(ref, {}).get("state") == "done" and records.get(ref, {}).get("decision") == "human accepted")
                ]
                if unresolved:
                    keep({"id": job["id"], "kind": job["kind"], "state": "blocked", "blocked_by": unresolved})
                else:
                    keep({"id": job["id"], "kind": job["kind"], "state": "planned"})
                continue
            if record.get("state") == "done":
                if not Path(record.get("path", "")).is_file():
                    raise ValueError(f"Accepted image {job['id']} is missing. Restore it before resuming.")
                urls[job["id"]] = record["url"]
                keep(record)
                continue
            if record.get("state") == "review":
                keep(record)
                continue
            while True:
                stopped()
                current_job = record.get("generation_job") or job
                if record.get("state") != "inspecting":
                    def save_generation(value):
                        value["kind"] = job["kind"]
                        checkpoint(value)

                    record = run_job(api_key, current_job, aspect, resolution, out, dict(urls),
                                     False, on_start, previous=record,
                                     checkpoint=save_generation, cancel=cancel)
                    record["kind"] = job["kind"]
                    if record.get("error"):
                        keep(record)
                        break
                    record["state"] = "inspecting"
                    keep(record)
                saved = records.get(job["id"], {})
                if saved.get("state") == "done" and saved.get("decision") == "human accepted":
                    record = dict(saved)
                    keep(record)
                    urls[job["id"]] = record["url"]
                    break
                stopped()
                references = [(ref, ref if ref.startswith(("https://", "http://")) else
                               Path(records[ref]["path"])) for ref in _ref_list(job)]
                try:
                    review = inspect_image(record, job, spec.get("inspection_context", {}),
                                           references, api_key=api_key,
                                           model=settings.get("model", ART_MODEL))
                    review = Inspection.model_validate(review).model_dump()
                except Exception as exc:
                    message = str(exc).replace(api_key, "[redacted]") if api_key else str(exc)
                    review = {"verdict": "review", "summary": "Inspection unavailable. Human review required.",
                              "findings": [], "changes": [], "preserve": [], "uncertainty": message[:1000]}
                saved = records.get(job["id"], {})
                if saved.get("state") == "done" and saved.get("decision") == "human accepted":
                    record = dict(saved)
                    keep(record)
                    urls[job["id"]] = record["url"]
                    break
                record.update(inspection=review, review_id=uuid4().hex,
                              inspection_model=settings.get("model", ART_MODEL),
                              state="done" if review["verdict"] == "accept" else "review")
                record.pop("repair_job", None)
                record.pop("repair_unavailable", None)
                if review["changes"] and record.get("repairs", 0) < MAX_REPAIRS:
                    try:
                        proposed = repair_job(job, record, out)
                        model, _ = choose(proposed, aspect, resolution)
                        refs = [ref if ref.startswith(("http://", "https://")) else urls[ref]
                                for ref in _ref_list(proposed)]
                        if len(refs) > card_for(model).max_images:
                            raise ValueError("Repair exceeds the model's reference limit.")
                        length_error = prompt_length_error(model, proposed["prompt"])
                        if length_error:
                            raise ValueError(length_error)
                        build_payload(model, proposed["prompt"], aspect, resolution, refs)
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
                if record["state"] == "done":
                    urls[job["id"]] = record["url"]
                break
