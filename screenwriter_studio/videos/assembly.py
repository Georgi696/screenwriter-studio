"""Normalize clips and assemble a timed, browser-playable H.264/AAC movie."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def require_tools() -> None:
    if not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")):
        raise ValueError("Install FFmpeg (including ffprobe) and restart the desk before generating video.")


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
                    raise RuntimeError("Video processing timed out; resume to retry assembly.")
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
    width, height = map(int, plan["aspect_ratio"].split(":"))
    for shot in plan["shots"]:
        info = probe(folder / shot["image"], cancel)
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        ratio = stream.get("width", 0) / max(1, stream.get("height", 0))
        if abs(ratio / (width / height) - 1) > 0.02:
            raise ValueError(f"{shot['id']}: keyframe dimensions do not match {plan['aspect_ratio']}.")


def validate_clip(path: Path, duration: int, cancel=None) -> dict:
    info = probe(path, cancel)
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not stream or float(stream.get("duration") or info.get("format", {}).get("duration") or 0) < duration - 0.15:
        raise ValueError(f"{path.name}: generated clip is missing video or shorter than the planned shot.")
    return info


def assemble(folder: Path, plan: dict, cancel=None) -> Path:
    out = folder / "videos"
    # Standardize dimensions as well as time bases, fps, pixel format and audio.
    edge = {"std": 720, "pro": 1080, "4K": 2160}[plan["mode"]]
    wide = edge * 16 // 9
    width, height = {"16:9": (wide, edge), "9:16": (edge, wide), "1:1": (edge, edge)}[plan["aspect_ratio"]]
    with tempfile.TemporaryDirectory(prefix=".assemble-", dir=out) as temporary:
        work = Path(temporary)
        names = []
        for shot in plan["shots"]:
            source = out / f"{shot['id']}.mp4"
            info = validate_clip(source, shot["duration_seconds"], cancel)
            has_audio = plan["sound"] and any(s.get("codec_type") == "audio" for s in info["streams"])
            name = f"{shot['id']}.mp4"
            args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
            if not has_audio:
                args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            args += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
                     "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,tpad=stop_mode=clone:stop_duration=0.15",
                     "-af", "aresample=48000,apad", "-t", str(shot["duration_seconds"]),
                     "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(work / name)]
            run_command(args, cancel)
            names.append(name)
        listing = work / "clips.txt"
        listing.write_text("".join(f"file '{name}'\n" for name in names), encoding="utf-8")
        target = work / "final.mp4"
        run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                     "-safe", "1", "-i", str(listing), "-c", "copy", "-t", str(plan["runtime_seconds"]),
                     "-movflags", "+faststart", str(target)], cancel)
        info = validate_clip(target, plan["runtime_seconds"], cancel)
        if abs(float(info["format"]["duration"]) - plan["runtime_seconds"]) > 0.2:
            raise ValueError("Assembled video does not match the locked runtime.")
        check_cancel(cancel)
        target.replace(out / "final.mp4")
    return out / "final.mp4"
