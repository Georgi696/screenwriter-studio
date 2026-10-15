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

kind selects the model unless "model" is set. references are earlier job ids,
public image URLs, or ids listed in locked_references. A job starts once the jobs
it references are done, so their result URLs can be passed along.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import threading
import fcntl
import json
import uuid
import socket
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from screenwriter_studio.paths import WORKSPACE_ROOT
from screenwriter_studio.images.catalog import (
    EDIT_MODEL as EDIT,
    GPT_IMAGE_MODEL as GPT_IMAGE,
    NANO_BANANA_MODEL as NANO,
    SEEDREAM_MODEL as SEEDREAM,
    card_for,
    prompt_length_error,
    ratio_allowed,
)
from screenwriter_studio.images.upload import SOURCE_LIMIT, UPLOAD_LIMIT, fit_image
from screenwriter_studio.models import IMAGE_MODEL_BY_KIND, load_kie_api_key as load_api_key

CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
POLL_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"
UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"
UPLOAD_PATH = "screenwriter-studio/references"
UPLOAD_REUSE_SECONDS = 20 * 3600
POLL_INTERVAL = 4
MAX_WAIT = 180
REQUEST_TIMEOUT = 60

POSTER_KINDS = {"poster", "title", "logo", "ui", "text"}
PRODUCT_KINDS = {"product", "packshot"}


def job_aspect(job: dict, aspect: str) -> str:
    """A job may frame itself differently from the film, e.g. a portrait character sheet."""
    return str(job.get("aspect_ratio") or aspect)


def choose(job: dict, aspect: str, resolution: str) -> tuple[str, str]:
    """Return (model, reason). An explicit model wins when the ratio is legal."""
    aspect = job_aspect(job, aspect)
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

    if refs and len(refs) > card_for(model).max_images:
        return NANO, f"{model} takes {card_for(model).max_images} reference images; this job has {len(refs)}, using {NANO}"
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


def _within(seconds: float, fn):
    """Bound a provider call. A socket that ignores its timeout must not hold the batch."""
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"Provider request timed out after {seconds}s.")
    if "error" in box:
        raise box["error"]
    return box["value"]


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
        raise ProviderHTTPError(exc.code, f"HTTP {exc.code}: {detail}") from exc


class ProviderHTTPError(RuntimeError):
    """The provider answered with an HTTP error status."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class JobFailed(RuntimeError):
    """Provider reports a terminal failure; an explicit resume may submit a retry."""


def submit_task(api_key: str, payload: dict, send=None) -> dict:
    """createTask, sorting failures into "certainly not created" and "unknown".

    A 4xx answer, a refused connection or an unknown host means no task exists, so
    the job is failed and an explicit resume may submit it again. A timeout, a reset
    after sending, or a 5xx may have created a billable task, so those stay unknown.
    """
    send = send or _request
    try:
        return _within(REQUEST_TIMEOUT, lambda: send(CREATE_URL, api_key, payload=payload))
    except ProviderHTTPError as exc:
        if 400 <= exc.code < 500:
            raise JobFailed(f"submit rejected: {exc}") from exc
        raise
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.gaierror, ConnectionRefusedError)):
            raise JobFailed(f"not sent, the provider could not be reached: {exc.reason}") from exc
        raise


def wait_for(payload: dict) -> float:
    """Larger images and more references take longer; a fixed wait turned slow jobs into errors."""
    body = payload.get("input") or {}
    scale = {"2K": 1.7, "4K": 2.7}.get(str(body.get("resolution") or ""), 1.0)
    references = body.get("image_input") or body.get("input_urls") or []
    return MAX_WAIT * scale + 20 * len(references)


def generate_one(api_key: str, payload: dict, *, task_id: str | None = None,
                 on_submitted=None, cancel=None) -> dict:
    if cancel is not None and cancel.is_set():
        raise RuntimeError("Generation cancelled.")
    if task_id is None:
        created = submit_task(api_key, payload)
        if created.get("code") != 200:
            raise JobFailed(f"submit rejected: {created.get('msg') or created}")
        task_id = (created.get("data") or {}).get("taskId")
        if not task_id:
            raise RuntimeError("Submission returned no taskId; check the provider before retrying.")
        if on_submitted:
            on_submitted(task_id)

    deadline = time.monotonic() + wait_for(payload)
    while time.monotonic() < deadline:
        if cancel is not None:
            if cancel.wait(POLL_INTERVAL):
                raise RuntimeError("Generation cancelled; provider task saved for resume.")
        else:
            time.sleep(POLL_INTERVAL)
        polled = _within(REQUEST_TIMEOUT, lambda: _request(POLL_URL, api_key, query={"taskId": task_id}))
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
    def fetch() -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "screenwriter-stills/1.0"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.read()

    payload = _within(90, fetch)
    if _header_kind(payload[:12]) is None:
        # An expired link or a CDN error page arrives as HTML; never save it as a still.
        raise RuntimeError("The provider returned a file that is not an image. Resume to retry the download.")
    temporary = dest.with_suffix(dest.suffix + ".part")
    temporary.write_bytes(payload)
    temporary.replace(dest)


_MAX_REFERENCE_BYTES = SOURCE_LIMIT
_REFERENCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _header_kind(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def _suffix_matches(suffix: str, header: bytes) -> bool:
    kind = _header_kind(header)
    if kind == ".jpg":
        return suffix in {".jpg", ".jpeg"}
    return kind is not None and kind == suffix


def locked_files(spec: dict) -> dict[str, Path]:
    """Ids of images that already exist. They are references, not jobs to generate."""
    raw = spec.get("locked_references") or []
    if not isinstance(raw, list):
        raise ValueError("Locked references are unreadable.")
    root = WORKSPACE_ROOT.resolve()
    found: dict[str, Path] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Locked references are unreadable.")
        ref_id = item.get("id")
        raw_path = item.get("path")
        if not isinstance(ref_id, str) or not ref_id.strip() or not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("Locked references are unreadable.")
        if ref_id in found:
            raise ValueError("Locked reference ids must be unique.")
        path = Path(raw_path)
        if not path.is_absolute():
            path = WORKSPACE_ROOT / path
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ValueError("Reference image is not available.") from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("Reference image is outside the workspace.") from exc
        if not resolved.is_file():
            raise ValueError("Reference image is not available.")
        suffix = resolved.suffix.lower()
        if suffix not in _REFERENCE_SUFFIXES:
            raise ValueError("Reference image must be a PNG, JPEG, or WebP.")
        try:
            size = resolved.stat().st_size
            header = resolved.read_bytes()[:12]
        except OSError as exc:
            raise ValueError("Reference image is not available.") from exc
        if size <= 0 or size > _MAX_REFERENCE_BYTES or not _suffix_matches(suffix, header):
            raise ValueError("Reference image must be a PNG, JPEG, or WebP within the size limit.")
        found[ref_id] = resolved
    return found


def style_reference_ids(spec: dict) -> set[str]:
    """Locked references the user marked as look only. Identity checks do not apply to them."""
    return {
        item["id"] for item in spec.get("locked_references") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("mode") == "style"
    }


def reference_data_url(path: Path) -> str:
    """Encode a workspace image as a data URL for the upload API, shrunk to the upload cap."""
    data = path.read_bytes()
    if not data or len(data) > _MAX_REFERENCE_BYTES:
        raise ValueError("Reference image is too large.")
    if _header_kind(data[:12]) is None:
        raise ValueError(f"Reference {path.name} must be a PNG, JPEG, or WebP image.")
    data, kind = fit_image(data, UPLOAD_LIMIT)
    # Label the upload by its real bytes: a JPEG saved as .png must not be sent as image/png.
    mime = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[kind]
    return f"data:{mime};base64," + base64.b64encode(data).decode()


_uploads: dict[str, tuple[str, float]] = {}
_upload_lock = threading.Lock()


def hosted_reference_url(api_key: str, path: Path) -> str:
    """KIE image tasks reject data URLs, so local references go through KIE's file
    store first. Uploads are deleted after 24 hours; reuse stays inside that window."""
    data_url = reference_data_url(path)
    digest = hashlib.sha256(data_url.encode()).hexdigest()
    with _upload_lock:
        cached = _uploads.get(digest)
        if cached and time.time() - cached[1] < UPLOAD_REUSE_SECONDS:
            return cached[0]
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[data_url[5:data_url.index(";")]]
        # A unique name per upload: a reused name can be overwritten or served stale by the file store.
        body = {"base64Data": data_url, "uploadPath": UPLOAD_PATH,
                "fileName": f"{digest[:16]}-{uuid.uuid4().hex[:8]}{extension}"}
        reply = _within(REQUEST_TIMEOUT, lambda: _request(UPLOAD_URL, api_key, payload=body))
        url = str((reply.get("data") or {}).get("downloadUrl") or "")
        if reply.get("code") != 200 or not url.startswith("https://"):
            raise RuntimeError(f"Reference upload of {path.name} rejected: {reply.get('msg') or 'no download URL'}")
        _uploads[digest] = (url, time.time())
        return url


def waves(jobs: list[dict], satisfied: set[str] | None = None) -> list[list[dict]]:
    pending = {job["id"]: job for job in jobs}
    done: set[str] = set(satisfied or ())
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
    locked: dict[str, Path] | None = None,
) -> dict:
    prompt = (job.get("prompt") or "").strip()
    aspect = job_aspect(job, aspect)
    job_id = str(job["id"])
    record = {**(previous or {}), "id": job_id, "kind": job.get("kind") or "keyframe"}
    if not prompt:
        record["error"] = "prompt is empty"
        return record

    model, reason = choose(job, aspect, resolution)
    locked = locked or {}
    ref_urls = []
    sent_urls = []
    for ref in _ref_list(job):
        if ref.startswith("http://") or ref.startswith("https://"):
            ref_urls.append(ref)
            sent_urls.append(ref)
        elif ref in urls:
            ref_urls.append(urls[ref])
            sent_urls.append(urls[ref])
        elif ref in locked:
            ref_urls.append(str(locked[ref]))
            try:
                sent_urls.append(str(locked[ref]) if dry_run else hosted_reference_url(api_key, locked[ref]))
            except (OSError, ValueError, RuntimeError) as exc:
                record["error"] = f"reference {ref}: {exc}"
                return record
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
        payload = build_payload(model, prompt, aspect, resolution, sent_urls)
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
              on_record=None, on_start=None, cancel=None, decision=None, only=None) -> dict:
    """Checkpoint submissions and results; resume never repeats a successful job."""
    settings = spec.get("inspection")
    if settings is not None and (not isinstance(settings, dict) or settings.get("mode") not in ("manual", "auto")):
        raise ValueError("Inspection mode must be manual or auto.")
    if decision and (not settings or dry_run or not resume):
        raise ValueError("Review decisions require resuming an inspected batch.")
    jobs = spec.get("jobs") or []
    if not jobs:
        raise ValueError("jobs.json has no jobs")
    ids = [job.get("id") for job in jobs]
    if any(not isinstance(value, str) or not value.strip() for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("every job needs a unique string id")
    locked = locked_files(spec)
    overlap = set(locked) & set(ids)
    if overlap:
        raise ValueError("Locked reference ids must not reuse still job ids.")
    paths = [_destination(out, job) for job in jobs]
    if settings:
        paths += [_destination(out, {"id": job["id"], "filename": f"{dest.stem}.repair-{n}{dest.suffix}"})
                  for job in jobs for dest in [_destination(out, job)] for n in (1, 2)]
    if len({str(path.resolve()) for path in paths}) != len(paths):
        raise ValueError("Job filenames collide after sanitization.")
    ordered = waves(jobs, set(locked))  # Validate the whole graph before spending credits.
    aspect = str(spec.get("aspect_ratio") or "16:9")
    resolution = str(spec.get("resolution") or "1K")
    if settings:
        for job in jobs:
            model, _ = choose(job, aspect, resolution)
            if len(_ref_list(job)) > card_for(model).max_images:
                raise ValueError(f"{job['id']}: reference count exceeds the selected model's limit.")
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".generation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("This image folder already has a running batch.") from exc
        return _run_locked(spec, out, ordered, aspect, resolution, fingerprint,
                           dry_run, resume, on_record, on_start, cancel, decision, locked, only)


def _run_locked(spec, out, ordered, aspect, resolution, fingerprint,
                dry_run, resume, on_record, on_start, cancel, decision, locked, only=None):
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
                previous = records.get(record["id"])
                if (
                    isinstance(previous, dict)
                    and previous.get("state") == "done"
                    and previous.get("decision") == "human accepted"
                    and not (record.get("state") == "done" and record.get("decision") == "human accepted")
                ):
                    record = previous
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

    if spec.get("inspection") and not dry_run:
        from screenwriter_studio.images.review import run_reviewed
        run_reviewed(spec, out, ordered, records, api_key, checkpoint, keep,
                     on_start, cancel, decision, only=set(only) if only else None)
        manifest["status"] = ("complete" if len(records) == len(spec["jobs"]) and
                              all(r.get("state") == "done" for r in records.values()) else "paused")
        checkpoint()
        return manifest

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
                previous=records.get(job["id"]), checkpoint=checkpoint, cancel=cancel, locked=locked,
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
    parser.add_argument("--review-id", help="Exact review_id from manifest.json")
    parser.add_argument("--image", help="Image ID for a review decision")
    parser.add_argument("--decision", choices=["keep", "repair", "inspect"])
    parser.add_argument("--regenerate", metavar="ID", help="Make a new take of one image (paid). Use with --prompt.")
    parser.add_argument("--prompt", help="The edited prompt for --regenerate.")
    parser.add_argument("--use-version", metavar="ID=FILE", help="Make an earlier version (e.g. s01=s01.png) the image again. No provider call.")
    parser.add_argument("--attach-task", metavar="ID=TASK_ID", help="Recover an uncertain image submission with its KIE task ID.")
    parser.add_argument("--forget-submission", metavar="ID",
                        help="After checking KIE's task history, mark an uncertain image submission as never created.")
    args = parser.parse_args()
    if any((args.decision, args.image, args.review_id)) and not all((args.decision, args.image, args.review_id)):
        parser.error("--decision, --image and --review-id must be supplied together")
    if bool(args.regenerate) != bool(args.prompt):
        parser.error("--regenerate and --prompt must be supplied together")

    from screenwriter_studio.images.review import attach_image_task, forget_image_submission, restore_version
    try:
        if args.forget_submission:
            forget_image_submission(args.out, args.forget_submission)
            print("Submission forgotten. Resume to send that image again.")
            return 0
        for value, label in ((args.attach_task, "--attach-task"), (args.use_version, "--use-version")):
            if value and "=" not in value:
                parser.error(f"Use {label} ID=VALUE.")
        if args.attach_task:
            image, _, task = args.attach_task.partition("=")
            attach_image_task(args.out, image, task)
            print("Task attached. Resume to poll it.")
            return 0
        if args.use_version:
            image, _, name = args.use_version.partition("=")
            restore_version(args.out, image, name)
            print(f"{image} now uses {name}.")
            return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    spec = json.loads(args.jobs.read_text(encoding="utf-8"))
    if args.regenerate:
        decision = {"id": args.regenerate, "action": "regenerate", "prompt": args.prompt}
    else:
        decision = {"id": args.image, "review_id": args.review_id, "action": args.decision} if args.decision else None
    try:
        manifest = run_batch(spec, args.out, dry_run=args.dry_run, resume=args.resume or bool(args.regenerate), decision=decision)
    except (ValueError, RuntimeError, SystemExit) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    failed = [record for record in manifest["images"] if record.get("error")]
    for record in manifest["images"]:
        status = "FAIL " + record["error"] if record.get("error") else record.get("model", "")
        print(f"{record['id']}: {status} -> {record.get('path', '')}")
    print(f"manifest: {args.out / ('manifest.preview.json' if args.dry_run else 'manifest.json')}")
    if manifest.get("status") == "paused":
        print("Paused: review findings and repair_job in manifest.json. Resume with --image ID --review-id TOKEN --decision keep|repair|inspect.")
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
