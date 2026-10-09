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

from models import (
    EDIT_MODEL as EDIT,
    GPT_IMAGE_MODEL as GPT_IMAGE,
    IMAGE_MODEL_BY_KIND,
    NANO_BANANA_MODEL as NANO,
    NANO_MAX_REFS,
    NANO_RATIOS,
    SEEDREAM_MODEL as SEEDREAM,
    SEEDREAM_RATIOS,
    SUNBURST_1K_ONLY,
    SUNBURST_RATIOS,
    load_kie_api_key as load_api_key,
)

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
    if model == NANO:
        return aspect in NANO_RATIOS
    if model in {GPT_IMAGE, EDIT}:
        if aspect not in SUNBURST_RATIOS:
            return False
        if resolution in {"2K", "4K"} and aspect in SUNBURST_1K_ONLY:
            return False
        return True
    if model == SEEDREAM:
        return aspect in SEEDREAM_RATIOS and resolution != "4K"
    return True


def _ref_list(job: dict) -> list[str]:
    refs = job.get("references") or job.get("reference_urls") or []
    if isinstance(refs, str):
        return [refs]
    return [str(item) for item in refs if str(item).strip()]


def build_payload(model: str, prompt: str, aspect: str, resolution: str, ref_urls: list[str]) -> dict:
    if model == GPT_IMAGE:
        payload = {"prompt": prompt, "aspect_ratio": aspect, "resolution": resolution}
        return {"model": model, "input": payload}
    if model == SEEDREAM:
        quality = "high" if resolution in {"2K", "4K"} else "basic"
        return {
            "model": model,
            "input": {
                "prompt": prompt,
                "aspect_ratio": aspect,
                "quality": quality,
                "output_format": "png",
            },
        }
    if model == EDIT:
        return {
            "model": model,
            "input": {
                "prompt": prompt,
                "input_urls": ref_urls[:16],
                "aspect_ratio": aspect,
                "resolution": resolution,
            },
        }
    body = {
        "prompt": prompt,
        "aspect_ratio": aspect,
        "resolution": resolution,
        "output_format": "png",
    }
    if ref_urls:
        body["image_input"] = ref_urls[:NANO_MAX_REFS]
    return {"model": NANO, "input": body}


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


def generate_one(api_key: str, payload: dict) -> dict:
    created = _request(CREATE_URL, api_key, payload=payload)
    if created.get("code") != 200:
        raise RuntimeError(f"submit rejected: {created.get('msg') or created}")
    task_id = (created.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"no taskId: {created}")

    deadline = time.time() + MAX_WAIT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        polled = _request(POLL_URL, api_key, query={"taskId": task_id})
        data = polled.get("data") or {}
        state = data.get("state", "")
        if state == "success":
            url = _first_url(data.get("resultJson"))
            if not url:
                raise RuntimeError(f"task {task_id} succeeded with no image URL")
            return {"task_id": task_id, "url": url}
        if state == "fail":
            raise RuntimeError(f"task {task_id} failed: {data.get('failMsg') or state}")
    raise RuntimeError(f"task {task_id} timed out after {MAX_WAIT}s")


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
        dest.write_bytes(resp.read())


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
            raise SystemExit(f"missing or circular references among: {leftovers}")
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
) -> dict:
    prompt = (job.get("prompt") or "").strip()
    job_id = str(job["id"])
    record = {"id": job_id, "kind": job.get("kind") or "keyframe"}
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

    filename = job.get("filename") or f"{_safe_name(job_id)}.png"
    dest = out / filename
    record.update({"model": model, "reason": reason, "path": str(dest), "references": ref_urls})
    if on_start is not None:
        on_start(dict(record))
    if dry_run:
        record["dry_run"] = True
        record["url"] = f"dry-run://{job_id}"
        return record

    limit = 5000 if model == SEEDREAM else 20000
    if len(prompt) > limit:
        record["error"] = f"prompt is {len(prompt)} characters; {model} allows {limit}"
        return record

    try:
        payload = build_payload(model, prompt, aspect, resolution, ref_urls)
        result = generate_one(api_key, payload)
        download(result["url"], dest)
    except Exception as exc:  # noqa: BLE001 — one bad still must not drop the batch
        record["error"] = str(exc)
        return record

    record["task_id"] = result["task_id"]
    record["url"] = result["url"]
    urls[job_id] = result["url"]
    return record


def run_batch(
    spec: dict,
    out: Path,
    *,
    dry_run: bool = False,
    on_record: Callable[[dict], None] | None = None,
    on_start: Callable[[dict], None] | None = None,
) -> dict:
    """Generate every still in spec and write manifest.json. Returns the manifest."""
    jobs = spec.get("jobs") or []
    if not jobs:
        raise ValueError("jobs.json has no jobs")
    ids = [str(job.get("id", "")).strip() for job in jobs]
    if any(not job_id for job_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("every job needs a unique id")

    aspect = str(spec.get("aspect_ratio") or "16:9")
    resolution = str(spec.get("resolution") or "1K")
    api_key = "" if dry_run else load_api_key()
    if not dry_run and not api_key:
        raise RuntimeError(
            "KIE_API_KEY is not set. Export it or put it in a .env file. "
            "Keys are created at https://kie.ai/api-key"
        )

    out.mkdir(parents=True, exist_ok=True)
    urls: dict[str, str] = {}
    records: list[dict] = []

    def _keep(record: dict) -> None:
        records.append(record)
        if record.get("url"):
            urls[record["id"]] = record["url"]
        if on_record is not None:
            on_record(record)

    for wave in waves(jobs):
        if dry_run or len(wave) == 1:
            for job in wave:
                _keep(run_job(api_key, job, aspect, resolution, out, urls, dry_run, on_start=on_start))
            continue
        with ThreadPoolExecutor(max_workers=min(3, len(wave))) as pool:
            futures = [
                pool.submit(
                    run_job,
                    api_key,
                    job,
                    aspect,
                    resolution,
                    out,
                    dict(urls),
                    dry_run,
                    on_start,
                )
                for job in wave
            ]
            for future in as_completed(futures):
                _keep(future.result())

    manifest = {
        "aspect_ratio": aspect,
        "resolution": resolution,
        "out": str(out),
        "images": records,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate screenplay stills with KIE.ai")
    parser.add_argument("jobs", type=Path, help="path to jobs.json")
    parser.add_argument("--out", type=Path, required=True, help="folder for the images")
    parser.add_argument("--dry-run", action="store_true", help="choose models and write no images")
    args = parser.parse_args()

    spec = json.loads(args.jobs.read_text(encoding="utf-8"))
    try:
        manifest = run_batch(spec, args.out, dry_run=args.dry_run)
    except (ValueError, RuntimeError, SystemExit) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    failed = [record for record in manifest["images"] if record.get("error")]
    for record in manifest["images"]:
        status = "FAIL " + record["error"] if record.get("error") else record.get("model", "")
        print(f"{record['id']}: {status} -> {record.get('path', '')}")
    print(f"manifest: {args.out / 'manifest.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
