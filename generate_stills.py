#!/usr/bin/env python3
"""Generate screenplay stills through KIE.ai and save them into a folder.

Reads KIE_API_KEY from the environment, then from a .env file in the working
directory or its parents. Never prints the key.

    python generate_stills.py jobs.json --out productions/<slug>/images
    python generate_stills.py jobs.json --out productions/<slug>/images --dry-run

jobs.json:

    {
      "aspect_ratio": "16:9",
      "resolution": "1K",
      "jobs": [
        {"id": "maya", "kind": "character", "prompt": "..."},
        {"id": "s01", "kind": "keyframe", "prompt": "...", "references": ["maya"]}
      ]
    }

kind selects the model unless "model" is set. references are earlier job ids
or public image URLs. Jobs in one wave run together; a later wave waits so it
can pass the earlier result URL as a reference.
"""

from __future__ import annotations

import argparse
import hashlib
import threading
import fcntl
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from image_model_card import (
    EDIT_MODEL as EDIT,
    GPT_IMAGE_MODEL as GPT_IMAGE,
    NANO_BANANA_MODEL as NANO,
    SEEDREAM_MODEL as SEEDREAM,
    card_for,
    prompt_length_error,
    ratio_allowed,
)
from models import IMAGE_MODEL_BY_KIND, load_kie_api_key as load_api_key

CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
POLL_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"
POLL_INTERVAL = 4
MAX_WAIT = 180

POSTER_KINDS = {"poster", "title", "logo", "ui", "text"}
PRODUCT_KINDS = {"product", "packshot"}


def choose(job: dict, aspect: str, resolution: str) -> tuple[str, str]:
    """Return (model, reason). An explicit model wins when the ratio is legal."""
    explicit = (job.get("model") or "").strip()
    kind = (job.get("kind") or "keyframe").strip().lower()
    refs = _ref_list(job)
    if explicit:
        if _ratio_ok(explicit, aspect, resolution):
            return explicit, "model set on the job"
        return NANO, f"{explicit} cannot do {aspect} at {resolution}; using {NANO}"

    routed = IMAGE_MODEL_BY_KIND.get(kind, NANO)
    if kind in POSTER_KINDS:
        model, why = routed, "readable text in the image"
    elif kind in PRODUCT_KINDS and not refs:
        model, why = routed, "photoreal product with no reference"
    elif kind == "edit" and len(refs) == 1:
        model, why = routed, "one reference, one change"
    elif kind == "edit":
        model, why = NANO, f"edit with several references stays on {NANO}"
    else:
        model, why = NANO, "cinematic still; holds identity across references"

    if resolution == "4K" and model == SEEDREAM:
        return NANO, f"4K is outside Seedream; using {NANO}"
    if not _ratio_ok(model, aspect, resolution):
        return NANO, f"{model} cannot do {aspect} at {resolution}; using {NANO}"
    return model, why


def _ratio_ok(model: str, aspect: str, resolution: str) -> bool:
    return ratio_allowed(model, aspect, resolution)


def _ref_list(job: dict) -> list[str]:
    refs = job.get("references") or job.get("reference_urls") or []
    if isinstance(refs, str):
        return [refs]
    return [str(item) for item in refs if str(item).strip()]


def build_payload(model: str, prompt: str, aspect: str, resolution: str, ref_urls: list[str]) -> dict:
    """KIE createTask body. Field names come from the checked model card."""
    card = card_for(model)
    body: dict = {card.field_prompt: prompt, card.field_ratio: aspect}
    if card.field_resolution:
        body[card.field_resolution] = resolution
    if card.field_output_format:
        body[card.field_output_format] = card.output_format
    if card.field_quality:
        body[card.field_quality] = card.quality_for(resolution)
    if card.field_images and (ref_urls or card.image_required):
        body[card.field_images] = ref_urls[: card.max_images]
    unknown = set(body) - set(card.documented_fields)
    if unknown:
        raise RuntimeError(f"undocumented fields for {card.model_id}: {sorted(unknown)}")
    return {"model": card.model_id, "input": body}


def _request(url: str, api_key: str, payload: dict | None = None, query: dict | None = None) -> dict:
    if query:
        url = url + "?" + urllib.parse.urlencode(query)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


class JobFailed(RuntimeError):
    """Provider reports a terminal failure; an explicit resume may submit a retry."""


def generate_one(api_key: str, payload: dict, *, task_id: str | None = None,
                 on_submitted=None, cancel=None) -> dict:
    if cancel is not None and cancel.is_set():
        raise RuntimeError("Generation cancelled.")
    if task_id is None:
        created = _request(CREATE_URL, api_key, payload=payload)
        if created.get("code") != 200:
            raise JobFailed(f"submit rejected: {created.get('msg') or created}")
        task_id = (created.get("data") or {}).get("taskId")
        if not task_id:
            raise RuntimeError("Submission returned no taskId; check the provider before retrying.")
        if on_submitted:
            on_submitted(task_id)

    deadline = time.monotonic() + MAX_WAIT
    while time.monotonic() < deadline:
        if cancel is not None:
            if cancel.wait(POLL_INTERVAL):
                raise RuntimeError("Generation cancelled; provider task saved for resume.")
        else:
            time.sleep(POLL_INTERVAL)
        polled = _request(POLL_URL, api_key, query={"taskId": task_id})
        if polled.get("code") != 200:
            raise RuntimeError(f"Polling task {task_id} failed: {polled.get('msg')}")
        data = polled.get("data") or {}
        state = data.get("state", "")
        if state == "success":
            url = _first_url(data.get("resultJson"))
            if not url:
                raise RuntimeError(f"task {task_id} succeeded with no image URL")
            return {"task_id": task_id, "url": url}
        if state == "fail":
            raise JobFailed(f"task {task_id} failed: {data.get('failMsg') or state}")
    raise RuntimeError(f"task {task_id} timed out after {MAX_WAIT}s; resume to continue polling")


def _first_url(result_json) -> str:
    if isinstance(result_json, str) and result_json.strip():
        try:
            result_json = json.loads(result_json)
        except json.JSONDecodeError:
            return ""
    if not isinstance(result_json, dict):
        return ""
    urls = result_json.get("resultUrls") or result_json.get("result_urls") or []
    if isinstance(urls, str):
        return urls
    if urls:
        return str(urls[0])
    for key in ("resultUrl", "url", "image_url"):
        if result_json.get(key):
            return str(result_json[key])
    return ""


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "screenwriter-stills/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        temporary = dest.with_suffix(dest.suffix + ".part")
        temporary.write_bytes(resp.read())
        temporary.replace(dest)


def waves(jobs: list[dict]) -> list[list[dict]]:
    pending = {job["id"]: job for job in jobs}
    done: set[str] = set()
    ordered: list[list[dict]] = []
    while pending:
        ready = []
        for job in pending.values():
            missing = [
                ref
                for ref in _ref_list(job)
                if not ref.startswith("http://") and not ref.startswith("https://") and ref not in done
            ]
            if not missing:
                ready.append(job)
        if not ready:
            leftovers = ", ".join(sorted(pending))
            raise ValueError(f"missing or circular references among: {leftovers}")
        ready.sort(key=lambda job: job["id"])
        ordered.append(ready)
        for job in ready:
            done.add(job["id"])
            del pending[job["id"]]
    return ordered


def _safe_name(job_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", job_id).strip("._")
    return cleaned or "still"


def run_job(
    api_key: str,
    job: dict,
    aspect: str,
    resolution: str,
    out: Path,
    urls: dict[str, str],
    dry_run: bool,
    on_start: Callable[[dict], None] | None = None,
    *, previous: dict | None = None, checkpoint=None, cancel=None,
) -> dict:
    prompt = (job.get("prompt") or "").strip()
    job_id = str(job["id"])
    record = {**(previous or {}), "id": job_id, "kind": job.get("kind") or "keyframe"}
    if not prompt:
        record["error"] = "prompt is empty"
        return record

    model, reason = choose(job, aspect, resolution)
    ref_urls = []
    for ref in _ref_list(job):
        if ref.startswith("http://") or ref.startswith("https://"):
            ref_urls.append(ref)
        elif ref in urls:
            ref_urls.append(urls[ref])
        else:
            record["error"] = f"reference {ref!r} has no image URL yet"
            return record

    dest = _destination(out, job)
    record.update({"model": model, "reason": reason, "path": str(dest), "references": ref_urls})
    length_error = prompt_length_error(model, prompt)
    if length_error:
        record["error"] = length_error
        return record

    if on_start is not None:
        on_start(dict(record))
    if dry_run:
        record["dry_run"] = True
        record["url"] = f"dry-run://{job_id}"
        return record

    previous = previous or {}
    task_id = previous.get("task_id") if previous.get("state") != "failed" else None
    result_url = previous.get("url") if previous.get("state") != "failed" else None
    if previous.get("state") == "submitting" and not task_id:
        record.update(previous)
        record["error"] = "Submission outcome is unknown. Reconcile this job with the provider before retrying; no duplicate was submitted."
        return record

    def save(**changes):
        record.update(changes)
        if checkpoint is not None:
            checkpoint(dict(record))

    try:
        payload = build_payload(model, prompt, aspect, resolution, ref_urls)
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Generation cancelled.")
        if result_url:
            result = {"task_id": task_id, "url": result_url}
            save(**result, state="downloading")
        else:
            save(state="polling" if task_id else "submitting", task_id=task_id)
            result = generate_one(
                api_key, payload, task_id=task_id, cancel=cancel,
                on_submitted=lambda value: save(task_id=value, state="polling"),
            )
            save(**result, state="downloading")
        download(result["url"], dest)
        record.update(result, state="done", error="")
    except JobFailed as exc:
        record.update(error=str(exc), state="failed")
        return record
    except Exception as exc:
        record["error"] = str(exc)
        return record

    urls[job_id] = result["url"]
    return record


def _destination(out: Path, job: dict) -> Path:
    name = job.get("filename") or f"{_safe_name(str(job['id']))}.png"
    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Image filenames must be plain names inside the output folder.")
    if Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Image filename must have a supported image extension.")
    dest = out / name
    if dest.resolve().parent != out.resolve():
        raise ValueError("Image destination escapes the output folder.")
    return dest



def run_batch(spec: dict, out: Path, *, dry_run: bool = False, resume: bool = False,
              on_record=None, on_start=None, cancel=None) -> dict:
    """Checkpoint submissions and results; resume never repeats a successful job."""
    jobs = spec.get("jobs") or []
    if not jobs:
        raise ValueError("jobs.json has no jobs")
    ids = [job.get("id") for job in jobs]
    if any(not isinstance(value, str) or not value.strip() for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("every job needs a unique string id")
    paths = [_destination(out, job) for job in jobs]
    if len({str(path.resolve()) for path in paths}) != len(paths):
        raise ValueError("Job filenames collide after sanitization.")
    ordered = waves(jobs)  # Validate the whole graph before spending credits.
    aspect = str(spec.get("aspect_ratio") or "16:9")
    resolution = str(spec.get("resolution") or "1K")
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".generation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("This image folder already has a running batch.") from exc
        return _run_locked(spec, out, ordered, aspect, resolution, fingerprint,
                           dry_run, resume, on_record, on_start, cancel)


def _run_locked(spec, out, ordered, aspect, resolution, fingerprint,
                dry_run, resume, on_record, on_start, cancel):
    manifest_path = out / ("manifest.preview.json" if dry_run else "manifest.json")
    records: dict[str, dict] = {}
    if manifest_path.exists() and not dry_run:
        if not resume:
            raise ValueError("An image manifest already exists. Use --resume to continue it.")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("fingerprint") != fingerprint:
            raise ValueError("Saved jobs differ from this plan (or use a legacy manifest). Use a new output folder.")
        records = {record["id"]: record for record in previous.get("images", [])}
    api_key = "" if dry_run else load_api_key()
    if not dry_run and not api_key:
        raise RuntimeError("KIE_API_KEY is not set.")
    manifest = {"aspect_ratio": aspect, "resolution": resolution, "out": str(out),
                "fingerprint": fingerprint, "images": []}
    mutex = threading.Lock()
    urls: dict[str, str] = {}

    def checkpoint(record=None):
        with mutex:
            if record is not None:
                records[record["id"]] = dict(record)
            manifest["images"] = [records[job["id"]] for job in spec["jobs"] if job["id"] in records]
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            temporary.replace(manifest_path)

    def keep(record):
        checkpoint(record)
        if record.get("url") and not record.get("error"):
            urls[record["id"]] = record["url"]
        if on_record:
            on_record(record)

    checkpoint()
    for wave in ordered:
        if cancel is not None and cancel.is_set():
            break
        pending = []
        for job in wave:
            previous = records.get(job["id"], {})
            dest = _destination(out, job)
            if previous.get("state") == "done" and dest.is_file() and previous.get("url"):
                keep(previous)
            else:
                pending.append(job)
        with ThreadPoolExecutor(max_workers=min(3, max(1, len(pending)))) as pool:
            futures = [pool.submit(
                run_job, api_key, job, aspect, resolution, out, dict(urls), dry_run, on_start,
                previous=records.get(job["id"]), checkpoint=checkpoint, cancel=cancel,
            ) for job in pending]
            for future in as_completed(futures):
                keep(future.result())
    if cancel is not None and cancel.is_set():
        raise RuntimeError("Generation cancelled. Submitted tasks are saved for resume.")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate screenplay stills with KIE.ai")
    parser.add_argument("jobs", type=Path, help="path to jobs.json")
    parser.add_argument("--out", type=Path, required=True, help="folder for the images")
    parser.add_argument("--dry-run", action="store_true", help="choose models and write no images")
    parser.add_argument("--resume", action="store_true", help="Reuse completed stills, poll saved tasks, and retry terminal failures")
    args = parser.parse_args()

    spec = json.loads(args.jobs.read_text(encoding="utf-8"))
    try:
        manifest = run_batch(spec, args.out, dry_run=args.dry_run, resume=args.resume)
    except (ValueError, RuntimeError, SystemExit) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    failed = [record for record in manifest["images"] if record.get("error")]
    for record in manifest["images"]:
        status = "FAIL " + record["error"] if record.get("error") else record.get("model", "")
        print(f"{record['id']}: {status} -> {record.get('path', '')}")
    print(f"manifest: {args.out / ('manifest.preview.json' if args.dry_run else 'manifest.json')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
