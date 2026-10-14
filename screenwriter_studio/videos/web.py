"""Public video summaries and bounded HTTP streaming for the local desk."""

from pathlib import Path
import re
from urllib.parse import quote

from screenwriter_studio.videos.assembly import require_tools
from screenwriter_studio.videos.plan import build_plan, read_json


def media_url(folder: Path, name: str) -> str:
    return "/video-media?path=" + quote(f"{folder.name}/videos/{name}", safe="")


def video_status(folder: Path | None, *, options: dict | None = None) -> dict:
    result = {"ready": False, "can_plan": False, "status": "waiting", "clips": [],
              "message": "Finish and approve a keyframe for every shot to generate the final video."}
    if folder is None:
        return result
    try:
        saved_path = folder / "videos/manifest.json"
        saved = read_json(saved_path) if saved_path.exists() else {}
        saved_plan = read_json(folder / "videos/jobs.json") if saved else {}
        options = options or {}
        if saved:
            options = saved_plan
        result.update(status=saved.get("status", "waiting"), saved=bool(saved),
                      mode=options.get("mode", "pro"), sound=options.get("sound", True),
                      selection=options.get("selection", "kling" if saved_plan.get("version") == 1 else "auto"))
        for clip in saved.get("clips", []):
            item = {key: clip.get(key, "") for key in ("id", "state", "error", "model")}
            name = f"{clip['id']}.mp4"
            if re.fullmatch(r"s\d+\.mp4", name) and clip.get("state") == "done" and (folder / "videos" / name).is_file():
                item["src"] = media_url(folder, name)
            result["clips"].append(item)
        if saved.get("status") == "complete" and (folder / "videos/final.mp4").is_file():
            result["src"] = media_url(folder, "final.mp4")
        plan = build_plan(folder, mode=result["mode"], sound=result["sound"], selection=result["selection"],
                          hash_images=False, frozen_plan=saved_plan or None)
        if saved_plan:
            comparable = {**saved_plan, "shots": [
                {k: v for k, v in shot.items() if k != "image_sha256"} for shot in saved_plan["shots"]]}
            if comparable != plan:
                raise ValueError("Saved video belongs to an earlier screenplay or keyframe plan. Use a new production for changed work.")
        result.update(can_plan=True, shots=plan["shots"], runtime_seconds=plan["runtime_seconds"],
                      aspect_ratio=plan["aspect_ratio"])
        # Local paths are only needed by the backend.
        result["shots"] = [{k: v for k, v in shot.items() if k != "image"} for shot in plan["shots"]]
        require_tools()
        result.update(ready=True, message=saved.get("error") or (
            "Final video ready." if result.get("src") else "Ready to animate the approved keyframes."))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["message"] = str(exc) if not isinstance(exc, FileNotFoundError) else "Finish and approve the stills before generating video."
    return result


def video_file(root: Path, raw: str) -> Path | None:
    parts = Path(raw).parts
    if len(parts) != 3 or parts[0] in {".", ".."} or parts[1] != "videos":
        return None
    if not re.fullmatch(r"(?:final|s\d+)\.mp4", parts[2]):
        return None
    path = (root / raw).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path


def stream_video(handler, path: Path, *, attachment: bool = False) -> None:
    """Single byte ranges let browsers seek without buffering an entire movie."""
    with path.open("rb") as stream:
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = handler.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            valid = bool(match and any(match.groups()))
            if valid:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), end) if last else end
                else:
                    start = max(0, size - int(last))
                valid = 0 <= start <= end < size
            if not valid:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
        handler.send_response(206 if range_header else 200)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Content-Length", str(end - start + 1))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Content-Type-Options", "nosniff")
        if range_header:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if attachment:
            handler.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        handler.end_headers()
        stream.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)
