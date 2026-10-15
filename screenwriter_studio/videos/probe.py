"""Optional ffprobe checks on keyframes and generated clips. Clips are delivered as-is; nothing is re-encoded or merged."""

import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


def probe_available() -> bool:
    """ffprobe only verifies sizes and durations; without it those checks are skipped."""
    return shutil.which("ffprobe") is not None


def check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise RuntimeError("Video generation stopped. Resume to continue saved work.")


def run_command(args: list[str], cancel=None, timeout: float = 1800, stream: str = "stdout") -> str:
    check_cancel(cancel)
    # Files avoid a blocked stderr pipe when an encoder emits a long diagnostic.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(args, stdout=output, stderr=errors, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                check_cancel(cancel)
                if time.monotonic() > deadline:
                    raise RuntimeError("Video processing timed out; resume to retry.")
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 1500))
                raise RuntimeError("Video processing failed: " + errors.read().decode(errors="replace"))
            chosen = output if stream == "stdout" else errors
            chosen.seek(0)
            return chosen.read().decode(errors="replace")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def probe(path: Path, cancel=None) -> dict:
    return json.loads(run_command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ], cancel, timeout=30))


def validate_keyframes(folder: Path, plan: dict, cancel=None) -> None:
    if not probe_available():
        return
    width, height = map(int, plan["aspect_ratio"].split(":"))
    for shot in plan["shots"]:
        info = probe(folder / shot["image"], cancel)
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        ratio = stream.get("width", 0) / max(1, stream.get("height", 0))
        if abs(ratio / (width / height) - 1) > 0.02:
            raise ValueError(f"{shot['id']}: keyframe dimensions do not match {plan['aspect_ratio']}.")


def validate_clip(path: Path, duration: int, cancel=None) -> dict | None:
    if not probe_available():
        return None
    info = probe(path, cancel)
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not stream or float(stream.get("duration") or info.get("format", {}).get("duration") or 0) < duration - 0.15:
        raise ValueError(f"{path.name}: generated clip is missing video or shorter than the planned shot.")
    return info


QUIET_MEAN_DB = -45.0  # ambience usually averages -30 to -20 dB; below this a clip plays as silence


def sound_check(path: Path, info: dict | None, cancel=None) -> dict | None:
    """Loudness of a clip made with sound. A quiet clip is kept; the desk flags it.

    Needs ffmpeg as well as the ffprobe result; without them nothing is measured.
    """
    if info is None or shutil.which("ffmpeg") is None:
        return None
    if not any(stream.get("codec_type") == "audio" for stream in info.get("streams", [])):
        return {"track": False, "quiet": True}
    log = run_command(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
                       "-af", "volumedetect", "-f", "null", "-"], cancel, timeout=60, stream="stderr")
    levels = {name: float(value) for name, value in re.findall(r"(mean|max)_volume: (-?[\d.]+|-inf) dB", log)}
    if "mean" not in levels:
        return None
    mean, peak = levels["mean"], levels.get("max", levels["mean"])
    return {"track": True, "quiet": mean < QUIET_MEAN_DB,
            "mean_db": round(mean, 1) if math.isfinite(mean) else None,
            "max_db": round(peak, 1) if math.isfinite(peak) else None}
