"""What is on disk for the desk: saved productions, their files, uploads and the cost view.

Nothing here touches the live run. Functions take a production folder (or its label) and read
files, so the crew thread never waits on them.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import urllib.parse
from pathlib import Path
from uuid import uuid4

from screenwriter_studio import settings as studio_settings
from screenwriter_studio.images.review import versions
from screenwriter_studio.images.upload import SOURCE_LIMIT, suffix_matches
from screenwriter_studio.paths import WORKSPACE_ROOT
from screenwriter_studio.pricing import production_estimate, remaining_images, spent_estimate
from screenwriter_studio.references import covered_sheets, reference_choices
from screenwriter_studio.schemas import Development, Screenplay
from screenwriter_studio.studio import _load_settings as load_run_settings
from screenwriter_studio.videos.plan import read_json as read_video_json
from screenwriter_studio.videos.web import video_file
from screenwriter_studio.web.board import scrub

PRODUCTIONS = WORKSPACE_ROOT / "productions"
DOCUMENT_SUFFIXES = {".json", ".jsonl", ".md", ".fountain", ".txt"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MAX_TEXT = 200_000
_MAX_IMAGE = SOURCE_LIMIT
_MAX_UPLOADED_IMAGE = 8_000_000
_REFERENCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _secret() -> str:
    return os.environ.get("KIE_API_KEY", "")


_cost_cache: dict = {}


def _version_label(entry: dict) -> str:
    if entry.get("repairs"):
        return f"Repair {entry['repairs']}" + (f" of take {entry['take']}" if int(entry.get("take") or 1) > 1 else "")
    return f"Take {entry['take']}" if int(entry.get("take") or 1) > 1 else "Original"


def production_view(folder: Path | None) -> dict:
    """The open production as the app bar shows it: its title and the credits it has used."""
    if folder is None:
        return {"title": "", "slug": "", "spent": None}
    development = _read_saved(folder / "00_development.json")
    title = str(development.get("title") or "") if isinstance(development, dict) else ""
    spec = _read_saved(folder / "images/jobs.json")
    manifest = _read_saved(folder / "images/manifest.json")
    plan = _read_saved(folder / "videos/jobs.json")
    clips = _read_saved(folder / "videos/manifest.json")
    spent = None
    if isinstance(spec, dict) and isinstance(manifest, dict):
        try:
            spent = spent_estimate(spec, manifest, plan if isinstance(plan, dict) else None,
                                   (clips or {}).get("clips") if isinstance(clips, dict) else None)
        except (TypeError, ValueError, KeyError):
            spent = None
    return {"title": title, "slug": folder.name, "spent": spent}


def image_references(spec: dict) -> dict[str, list[dict]]:
    """The pictures each image was asked to follow, so a review can show them side by side."""
    jobs = {str(job.get("id")): job for job in spec.get("jobs") or [] if isinstance(job, dict)}
    locked = {str(item.get("id")): item for item in spec.get("locked_references") or [] if isinstance(item, dict)}
    root = PRODUCTIONS.resolve()
    found: dict[str, list[dict]] = {}
    for image_id, job in jobs.items():
        refs = []
        for ref in job.get("references") or []:
            ref = str(ref)
            if ref in jobs:
                kind = str(jobs[ref].get("kind") or "image")
                refs.append({"label": f"{ref} · {kind}", "role": _KIND_ROLES.get(kind, ""),
                             "src": "/media?id=" + urllib.parse.quote(ref, safe="")})
            elif ref in locked:
                try:
                    path = Path(str(locked[ref].get("path") or "")).resolve()
                except OSError:
                    continue
                if path.is_file() and _under(path, root):
                    role = str(locked[ref].get("mode") or "")
                    refs.append({"label": str(locked[ref].get("name") or ref) + (f" · {role}" if role else ""), "role": role,
                                 "src": "/library-media?path=" + urllib.parse.quote(path.relative_to(root).as_posix(), safe="")})
        if refs:
            found[image_id] = refs
    return found


def cost_view(folder: Path | None, proceed: bool) -> dict:
    """Credit estimates for the next paid step, from the saved files. Cached until one of them changes."""
    result = {"estimate": None, "images_estimate": None, "image_resolution": "", "image_prompts": {}, "image_references": {}}
    if folder is None:
        return result
    names = ("00_development.json", "01_screenplay.json", "run_settings.json", "images/jobs.json", "images/manifest.json")
    stamps = []
    for path in [folder / name for name in names] + [studio_settings.PATH]:
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    key = (str(folder), proceed, tuple(stamps))
    if _cost_cache.get("key") == key:
        value = _cost_cache["value"]
        return {**value, "image_prompts": dict(value["image_prompts"]), "image_references": dict(value["image_references"])}
    try:
        if proceed:
            development = Development.model_validate(read_video_json(folder / "00_development.json"))
            screenplay = Screenplay.model_validate(read_video_json(folder / "01_screenplay.json"))
            image_count, references = load_run_settings(folder)
            locked = covered_sheets(references, [c.name for c in development.characters], list(development.locations))
            result["estimate"] = production_estimate(development, screenplay, values=studio_settings.load(),
                                                     image_count=image_count, locked_count=locked)
        spec_path = folder / "images/jobs.json"
        if spec_path.is_file():
            spec = read_video_json(spec_path)
            manifest_path = folder / "images/manifest.json"
            manifest = read_video_json(manifest_path) if manifest_path.is_file() else {}
            result["image_resolution"] = str(spec.get("resolution") or "1K")
            result["image_prompts"] = {str(job.get("id")): str(job.get("prompt") or "")
                                       for job in spec.get("jobs", []) if isinstance(job, dict)}
            result["image_references"] = image_references(spec)
            remaining = remaining_images(spec, manifest)
            if remaining["count"]:
                result["images_estimate"] = remaining
    except (OSError, ValueError, KeyError, TypeError):
        pass
    _cost_cache.update(key=key, value=result)
    return {**result, "image_prompts": dict(result["image_prompts"]), "image_references": dict(result["image_references"])}


def shot_plan(folder: Path | None) -> dict:
    """The saved shot list, so every tab can tie an image or clip back to its shot."""
    empty = {"shots": [], "runtime_seconds": 0, "aspect_ratio": ""}
    if folder is None:
        return empty
    try:
        development = json.loads((folder / "00_development.json").read_text(encoding="utf-8"))
        screenplay = json.loads((folder / "01_screenplay.json").read_text(encoding="utf-8"))
        shots = [{"id": f"s{int(shot['number']):02d}", "number": int(shot["number"]),
                  "duration_seconds": int(shot["duration_seconds"]),
                  **{key: str(shot.get(key) or "") for key in ("framing", "camera_move", "location", "action", "audio", "notes")},
                  "characters": [str(name) for name in shot.get("characters") or []],
                  "dialogue": [{"speaker": str(line.get("speaker") or ""), "text": str(line.get("text") or "")}
                               for line in shot.get("dialogue") or [] if isinstance(line, dict)]}
                 for shot in screenplay["shots"]]
        return {"shots": shots, "runtime_seconds": int(development.get("runtime_seconds") or 0),
                "aspect_ratio": str(development.get("aspect_ratio") or "")}
    except (OSError, ValueError, KeyError, TypeError):
        return empty


def saved_verdict(folder: str) -> bool | None:
    """Whether the editor passed the saved pages, or nothing when there is no verdict yet."""
    path = production_path(folder)
    if path is None:
        return None
    try:
        verdict = json.loads((path / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    passed = verdict.get("passed") if isinstance(verdict, dict) else None
    return passed if isinstance(passed, bool) else None


def production_path(folder: str) -> Path | None:
    """A production directory under productions/, or nothing."""
    if not folder or not isinstance(folder, str):
        return None
    path = Path(folder)
    if not path.is_absolute():
        path = WORKSPACE_ROOT / folder
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_dir() or not _under(resolved, PRODUCTIONS):
        return None
    return resolved


def can_proceed(folder: str) -> bool:
    """Saved pages can continue when stills have not been submitted and the draft was not aborted."""
    path = production_path(folder)
    if path is None:
        return False
    if (path / "aborted.json").is_file():
        return False
    needed = ("00_development.json", "01_screenplay.json", "verdict.json")
    if not all((path / name).is_file() for name in needed):
        return False
    images = path / "images"
    if (images / "jobs.json").is_file() or (images / "manifest.json").is_file():
        return False
    try:
        verdict = json.loads((path / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(verdict, dict) and isinstance(verdict.get("passed"), bool)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _production_roots(folder: str) -> list[Path]:
    roots: list[Path] = []
    if PRODUCTIONS.is_dir():
        roots.append(PRODUCTIONS.resolve())
    if folder:
        candidate = Path(folder)
        if not candidate.is_absolute():
            candidate = WORKSPACE_ROOT / folder
        if candidate.is_dir():
            roots.append(candidate.resolve())
    return roots


def _safe_image(path: str, folder: str) -> Path | None:
    if not path:
        return None
    file = Path(path)
    try:
        if not file.is_file():
            return None
        resolved = file.resolve()
    except OSError:
        return None
    if resolved.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    if resolved.stat().st_size > _MAX_IMAGE:
        return None
    if any(_under(resolved, root) for root in _production_roots(folder)):
        return resolved
    return None


def library_catalog() -> tuple[list[dict], str]:
    """Documents and images under productions/<slug>/, plus a change signature."""
    root = PRODUCTIONS.resolve() if PRODUCTIONS.is_dir() else PRODUCTIONS
    projects: list[dict] = []
    signature: list[str] = []
    if not root.is_dir():
        return projects, ""
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        documents: list[dict] = []
        images: list[dict] = []
        videos: list[dict] = []
        for file in sorted(folder.rglob("*")):
            if not file.is_file() or file.name.startswith("."):
                continue
            resolved = file.resolve()
            if not _under(resolved, root):
                continue
            suffix = resolved.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                bucket = images
            elif suffix in DOCUMENT_SUFFIXES:
                bucket = documents
            elif suffix == ".mp4" and video_file(root, resolved.relative_to(root).as_posix()):
                bucket = videos
            else:
                continue
            relative = resolved.relative_to(root).as_posix()
            stat = resolved.stat()
            bucket.append({"path": relative, "name": resolved.relative_to(folder.resolve()).as_posix()})
            signature.append(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}")
        hints, saved = _image_hints(folder, root)
        summary = _project_summary(folder, root)
        for item in images:
            item.update(hints.get(item["path"], {}))
        projects.append({"slug": folder.name, "documents": documents, "images": images, "videos": videos,
                         "references": saved, "summary": summary})
    return projects, "|".join(signature)


_ROLE_LABELS = {"style": "style", "character": "character", "location": "location",
                "object": "object", "composition": "composition"}
_KIND_ROLES = {"character": "character", "location": "location", "product": "object", "packshot": "object"}


def _read_saved(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _project_summary(folder: Path, root: Path) -> dict:
    """How far a production got, what waits for the user, and a picture to recognise it by."""
    root = root.resolve()
    development = _read_saved(folder / "00_development.json")
    title = str(development.get("title") or "") if isinstance(development, dict) else ""
    summary = {"title": title or folder.name, "stage": "idea", "needs": 0, "images": [0, 0], "clips": [0, 0], "thumb": ""}
    if (folder / "01_screenplay.json").is_file():
        summary["stage"] = "story"
    manifest = _read_saved(folder / "images/manifest.json")
    records = [r for r in (manifest or {}).get("images") or [] if isinstance(r, dict)] if isinstance(manifest, dict) else []
    if records:
        summary["stage"] = "images"
        summary["images"] = [sum(1 for r in records if r.get("state") == "done"), len(records)]
        summary["needs"] = sum(1 for r in records if r.get("state") in {"review", "failed"})
        shots = sorted((r for r in records if re.fullmatch(r"s\d{2,3}", str(r.get("id") or ""))), key=lambda r: r["id"])
        for record in shots + records:
            try:
                path = Path(str(record.get("path") or "")).resolve()
            except OSError:
                continue
            if path.is_file() and _under(path, root) and path.suffix.lower() in _REFERENCE_SUFFIXES:
                summary["thumb"] = path.relative_to(root).as_posix()
                break
    videos = _read_saved(folder / "videos/manifest.json")
    clips = [c for c in (videos or {}).get("clips") or [] if isinstance(c, dict)] if isinstance(videos, dict) else []
    if clips:
        summary["stage"] = "clips"
        summary["clips"] = [sum(1 for c in clips if c.get("state") == "done"), len(clips)]
    return summary


def _image_hints(folder: Path, root: Path) -> tuple[dict[str, dict], list[dict]]:
    """What the studio already knows about a production's images, so the picker need not guess.

    Returns hints by library path (role, subject, a readable label, and whether the file is an
    earlier version) and the production's saved reference choices, pointing at its own copies.
    """
    def library_path(raw) -> str | None:
        try:
            path = Path(str(raw)).resolve()
            return path.relative_to(root).as_posix() if path.is_file() and _under(path, root) else None
        except (OSError, ValueError):
            return None

    hints: dict[str, dict] = {}
    saved: list[dict] = []
    settings = _read_saved(folder / "run_settings.json")
    refs = settings.get("references") if isinstance(settings, dict) else None
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or ref.get("mode") not in _ROLE_LABELS:
            continue
        path = library_path(ref.get("path"))
        if path is None:
            continue
        name = str(ref.get("name") or "")
        hint = {"role": ref["mode"], "subject": name, "label": f"{name or ref.get('id', '')} · {_ROLE_LABELS[ref['mode']]} reference"}
        if ref["mode"] == "character":
            hint.update(face=ref.get("face", True) is not False, wardrobe=ref.get("wardrobe", True) is not False)
        hints[path] = hint
        saved.append({"path": path, "mode": ref["mode"], "name": name, "instruction": str(ref.get("instruction") or ""),
                      "scope": "shots" if ref.get("shots") else "auto", "shots": list(ref.get("shots") or []),
                      **({"face": hint["face"], "wardrobe": hint["wardrobe"]} if ref["mode"] == "character" else {})})

    jobs = _read_saved(folder / "images/jobs.json")
    manifest = _read_saved(folder / "images/manifest.json")
    development = _read_saved(folder / "00_development.json")
    if not isinstance(jobs, dict) or not isinstance(manifest, dict):
        return hints, saved
    characters = development.get("characters") if isinstance(development, dict) else None
    names = {str(c.get("name", "")).casefold(): str(c.get("name")) for c in characters or [] if isinstance(c, dict)}
    kinds = {job.get("id"): job.get("kind") for job in jobs.get("jobs") or [] if isinstance(job, dict)}
    for record in manifest.get("images") or []:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            continue
        image_id, kind = record["id"], kinds.get(record["id"])
        role = _KIND_ROLES.get(kind)
        shot = re.fullmatch(r"s(\d{2,3})", image_id) if kind in {"keyframe", "scene"} else None
        if role == "character":
            subject = names.get(image_id.casefold(), image_id.replace("-", " ").replace("_", " ").title())
        else:
            subject = image_id.replace("-", " ").replace("_", " ")
        label = f"Shot {int(shot.group(1))}" if shot else f"{subject} · {kind or 'image'}"
        hint = {"label": label, **({"role": role, "subject": subject} if role else {})}
        current = library_path(record.get("path"))
        if current:
            hints.setdefault(current, hint)
        for entry in versions(record):
            earlier = library_path(entry.get("path"))
            if earlier:
                hints.setdefault(earlier, {**hint, "label": label + " · earlier version", "earlier": True})
    return hints, saved


def reference_file(path: str) -> Path | None:
    """A PNG, JPEG, or WebP already stored under productions/."""
    file = library_file(path)
    if file is None or file.suffix.lower() not in _REFERENCE_SUFFIXES:
        return None
    try:
        size = file.stat().st_size
        header = file.read_bytes()[:12]
    except OSError:
        return None
    if size <= 0 or size > _MAX_IMAGE or not suffix_matches(file.suffix.lower(), header):
        return None
    return file


def parse_image_count(value) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 240:
        raise ValueError("Image count must be an integer between 1 and 240.")
    return value


def parse_references(value) -> list[dict]:
    """Validate reference roles and settings against files in the library."""
    chosen: list[dict] = []
    seen: set[str] = set()
    for choice in reference_choices(value):
        relative = choice["path"]
        if reference_file(relative) is None:
            raise ValueError("Reference image is not in a production folder.")
        if relative not in seen:
            seen.add(relative)
            chosen.append(choice)
    return chosen


def save_reference_upload(filename, encoded) -> dict:
    """Store an uploaded still under productions/.references without using the client path."""
    if not isinstance(filename, str) or not isinstance(encoded, str):
        raise ValueError("Upload must be a PNG, JPEG, or WebP image.")
    suffix = Path(filename).suffix.lower()
    if suffix not in _REFERENCE_SUFFIXES or Path(filename).name != filename:
        raise ValueError("Upload must be a PNG, JPEG, or WebP image.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Upload must be a PNG, JPEG, or WebP image.") from exc
    if not data or len(data) > _MAX_UPLOADED_IMAGE or not suffix_matches(suffix, data[:12]):
        raise ValueError("Upload must be a PNG, JPEG, or WebP image within the size limit.")
    if not PRODUCTIONS.is_dir():
        raise ValueError("Upload path is not allowed.")
    root = PRODUCTIONS.resolve()
    dest_dir = (root / ".references").resolve()
    if not _under(dest_dir, root):
        raise ValueError("Upload path is not allowed.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / f"{uuid4().hex}{suffix}").resolve()
    if not _under(dest, dest_dir) or dest.exists():
        raise ValueError("Upload path is not allowed.")
    temporary = dest.with_suffix(dest.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(dest)
    relative = dest.relative_to(root).as_posix()
    return {
        "id": relative,
        "src": "/library-media?path=" + urllib.parse.quote(relative, safe=""),
    }


def library_file(path: str) -> Path | None:
    raw = (path or "").strip()
    if not raw or raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
        return None
    if not PRODUCTIONS.is_dir():
        return None
    root = PRODUCTIONS.resolve()
    file = (root / raw).resolve()
    if not file.is_file() or not _under(file, root):
        return None
    if file.suffix.lower() not in DOCUMENT_SUFFIXES | IMAGE_SUFFIXES:
        return None
    return file


def library_text(file: Path) -> str:
    text = file.read_text(encoding="utf-8", errors="replace")
    if file.suffix.lower() == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2)
        except json.JSONDecodeError:
            pass
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n\n… truncated"
    return scrub(text, _secret())


