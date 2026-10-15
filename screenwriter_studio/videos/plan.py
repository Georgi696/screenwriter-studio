"""Deterministic video handoff; planning never calls a model or uploads files."""

import hashlib
import json
from pathlib import Path

from screenwriter_studio.images.upload import SOURCE_LIMIT
from screenwriter_studio.schemas import Development, Screenplay
from screenwriter_studio.videos.catalog import (
    CARDS, CHECKED, KLING, SEEDANCE, SELECTIONS, capability_error, choose_model,
    has_speech, language_codes,
)
from screenwriter_studio.videos.prompts import make_prompt

# KIE request contract checked 2026-10-14:
# https://docs.kie.ai/market/kling/kling-3-0
MODEL = KLING  # Compatibility with the original single-model plans.
MODES = ("std", "pro", "4K")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object.")
    return value


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


STRUCTURED_SHOT_FIELDS = ("camera_move", "characters", "location", "dialogue")


def screenplay_digest(screenplay: Screenplay) -> str:
    """Empty structured shot fields are left out, so plans saved before those fields existed still match."""
    data = screenplay.model_dump()
    for shot in data["shots"]:
        for key in STRUCTURED_SHOT_FIELDS:
            if not shot.get(key):
                shot.pop(key, None)
    return digest(data)


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_file(folder: Path, name: str) -> Path:
    path = Path(name)
    if not path.is_absolute():
        path = folder / path
    path = path.resolve()
    if not path.is_relative_to(folder.resolve()):
        # Manifests store absolute paths; after the productions folder moves, look in images/.
        moved = (folder / "images" / path.name).resolve()
        if moved.is_relative_to(folder.resolve()) and moved.is_file():
            return moved
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError("A required production file is missing or outside this production.")
    return path


def build_plan(folder: Path, *, mode: str = "pro", sound: bool = True,
               hash_images: bool = True, selection: str = "auto", frozen_plan: dict | None = None) -> dict:
    if mode not in MODES or type(sound) is not bool:
        raise ValueError("Choose std, pro, or 4K quality and a boolean sound setting.")
    if selection not in SELECTIONS:
        raise ValueError("Choose auto, kling, or seedance model selection.")
    legacy = bool(frozen_plan and frozen_plan.get("version") == 1)
    if legacy and selection != "kling":
        raise ValueError("This saved video uses Kling. Start a new production to change its model.")
    frozen_shots = {shot["id"]: shot for shot in frozen_plan["shots"]} if frozen_plan else {}
    development = Development.model_validate(read_json(folder / "00_development.json"))
    screenplay = Screenplay.model_validate(read_json(folder / "01_screenplay.json"))
    if (folder / "aborted.json").exists():
        raise ValueError("This production was aborted.")
    verdict = read_json(folder / "verdict.json")
    if verdict.get("passed") is not True:
        greenlight = folder / "greenlight.json"
        if not greenlight.exists() or read_json(greenlight).get("proceeded") is not True:
            raise ValueError("Approve the saved screenplay before generating video.")
    numbers = [shot.number for shot in screenplay.shots]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("Shot numbers must be sequential and unique.")
    if sum(shot.duration_seconds for shot in screenplay.shots) != development.runtime_seconds:
        raise ValueError("Shot durations must add up to the locked runtime.")
    spec = read_json(folder / "images/jobs.json")
    if spec.get("aspect_ratio") != development.aspect_ratio:
        raise ValueError("Keyframes must match the locked aspect ratio.")
    manifest = read_json(folder / "images/manifest.json")
    if manifest.get("fingerprint") and manifest["fingerprint"] != digest(spec):
        raise ValueError("The saved still plan changed after generation. Resolve the stills before generating video.")
    records = manifest.get("images", [])
    images = {record["id"]: record for record in records}
    if len(images) != len(records):
        raise ValueError("Duplicate image IDs in the still manifest.")
    pending = [job["id"] for job in spec.get("jobs", [])
               if images.get(job["id"], {}).get("state") != "done"]
    if pending:
        raise ValueError("Finish and approve the images first: " + ", ".join(pending))
    keyframes = {job["id"] for job in spec.get("jobs", []) if job.get("kind") in {"keyframe", "scene"}}
    shots = []
    for shot in screenplay.shots:
        shot_id = f"s{shot.number:02d}"
        record = images.get(shot_id, {})
        if shot_id not in keyframes or record.get("state") != "done" or record.get("dry_run"):
            raise ValueError(f"{shot_id}: a completed, approved keyframe is required for every shot.")
        source = local_file(folder, str(record.get("path") or ""))
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError(f"{shot_id}: unsupported keyframe format.")
        if not 0 < source.stat().st_size <= SOURCE_LIMIT:
            raise ValueError(f"{shot_id}: keyframes must be nonempty and at most {SOURCE_LIMIT // 1_000_000} MB.")
        prompt = (
            "SUBJECT: Preserve the subjects and identities in the supplied first frame.\n"
            "ENVIRONMENT: Continue the setting shown in the first frame.\n"
            f"ACTION: Over {shot.duration_seconds} seconds: {shot.action}\n"
            f"CAMERA MOVEMENT: {shot.framing}. Maintain one continuous shot.\n"
            "STYLE: Preserve the first frame's lighting, grade, and texture.\n"
            f"SOUND FX: {shot.audio if sound else 'Silent clip.'}\n"
            f"CONSTRAINTS: Preserve anatomy, wardrobe, props, and spatial continuity. {shot.notes}\n"
            f"Spoken language: {development.language}. Use only dialogue specified above."
        )
        # Preserve the exact v1 prompt for previously submitted jobs.
        if legacy and len(prompt) > 2500:
            raise ValueError(f"{shot_id}: video prompt exceeds the studio's 2500-character limit.")
        item = {"id": shot_id, "duration_seconds": shot.duration_seconds,
                "prompt": prompt, "image": source.relative_to(folder.resolve()).as_posix()}
        if not legacy:
            if frozen_plan:
                if shot_id not in frozen_shots:
                    raise ValueError("The saved video shot list changed.")
                # Rebuild input fingerprints below, but never reroute/rewrite saved jobs.
                item = {**frozen_shots[shot_id], "image": item["image"], "duration_seconds": shot.duration_seconds}
                item.pop("image_sha256", None)
            else:
                card, reason = choose_model(development, shot, mode=mode, sound=sound, selection=selection)
                try:
                    prompt = make_prompt(card, development, shot, sound=sound)
                except ValueError:
                    if selection != "auto" or card.id != KLING:
                        raise
                    # Preserve a long required action/dialogue instead of truncating it.
                    card, _ = choose_model(development, shot, mode=mode, sound=sound, selection="seedance")
                    prompt = make_prompt(card, development, shot, sound=sound)
                    reason = "Required shot instructions exceed Kling's prompt limit; Seedance can retain them within its documented limit."
                item.update(model=card.id, model_name=card.name, selection_reason=reason,
                            prompt=prompt,
                            sources=list(card.sources), docs_checked=CHECKED,
                            speech_languages=sorted(language_codes(development.language)) if sound and has_speech(shot) else [])
        if hash_images:
            item["image_sha256"] = file_digest(source)
        shots.append(item)
    common = {"mode": mode, "sound": sound, "screenplay_sha256": screenplay_digest(screenplay),
            "aspect_ratio": development.aspect_ratio,
            "runtime_seconds": development.runtime_seconds, "shots": shots}
    if legacy:
        return {"version": 1, "model": MODEL, **common}
    return {**(frozen_plan or {}), "version": 2, "selection": selection,
            "development_sha256": digest(development.model_dump()), **common}


def payload(plan: dict, shot: dict, image_url: str) -> dict:
    model = shot.get("model", plan.get("model"))
    if model not in CARDS:
        raise ValueError("Unknown video model in the saved plan.")
    card = CARDS[model]
    error = capability_error(card, duration=shot["duration_seconds"], mode=plan["mode"],
                             aspect=plan["aspect_ratio"], languages=set(shot.get("speech_languages", [])))
    if error:
        raise ValueError(f"{shot['id']}: {card.name} {error}.")
    if not 0 < len(shot["prompt"]) <= card.prompt_max:
        raise ValueError(f"{shot['id']}: prompt is outside {card.name}'s documented limit.")
    if model == SEEDANCE:
        return {"model": model, "input": {
            "prompt": shot["prompt"], "first_frame_url": image_url,
            "generate_audio": plan["sound"], "duration": shot["duration_seconds"],
            "aspect_ratio": "adaptive", "resolution": {"std": "720p", "pro": "1080p"}[plan["mode"]],
            "output_format": "mp4",
        }}
    return {"model": model, "input": {
        "prompt": shot["prompt"], "image_urls": [image_url],
        "sound": plan["sound"], "duration": str(shot["duration_seconds"]),
        "aspect_ratio": plan["aspect_ratio"], "mode": plan["mode"], "multi_shots": False,
    }}
