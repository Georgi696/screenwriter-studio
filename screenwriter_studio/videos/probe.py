"""Optional ffprobe checks on keyframes and generated clips. Clips are delivered as-is; nothing is re-encoded or merged."""

import json
from pathlib import Path
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


def run_command(args: list[str], cancel=None, timeout: float = 1800) -> str:
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
            output.seek(0)
            return output.read().decode()
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
