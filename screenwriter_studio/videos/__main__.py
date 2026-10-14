"""python -m screenwriter_studio.videos PRODUCTION [--dry-run]."""

import argparse
from pathlib import Path
import threading

from screenwriter_studio.models import load_kie_api_key
from screenwriter_studio.videos.service import attach_task, render_video


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate/resume final video from approved stills.")
    parser.add_argument("production", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Save an offline video plan; no uploads or paid calls.")
    parser.add_argument("--mode", choices=("std", "pro", "4K"), default=None)
    parser.add_argument("--model", choices=("auto", "kling", "seedance"), default=None,
                        help="Choose per shot automatically (default), or require a compatible model.")
    parser.add_argument("--sound", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--shot", action="append", metavar="ID", help="Render only this shot (repeatable), e.g. --shot s02. The film is assembled once every clip exists.")
    parser.add_argument("--redo", action="append", metavar="ID", help="Regenerate a finished shot (repeatable). The old take is kept as ID_vN.mp4. Paid.")
    parser.add_argument("--attach-task", metavar="SHOT=TASK_ID", help="Recover an uncertain submission using its KIE task ID.")
    args = parser.parse_args()
    cancel = threading.Event()
    try:
        if args.attach_task:
            shot, separator, task = args.attach_task.partition("=")
            if not separator:
                raise ValueError("Use --attach-task s01=TASK_ID.")
            attach_task(args.production.resolve(), shot, task)
            print("Task attached. Run again to resume.")
            return 0
        result = render_video(args.production, mode=args.mode, sound=args.sound,
                              selection=args.model, dry_run=args.dry_run, cancel=cancel,
                              shots=args.shot, regenerate=args.redo)
        print({"planned": "Video plan saved.", "partial": "Clips saved; render the remaining shots to assemble the film."}.get(
            result["status"], str(args.production / "videos/final.mp4")))
        return 0
    except KeyboardInterrupt:
        cancel.set()
        print("Stopped. Run again to resume saved video tasks.")
        return 130
    except Exception as exc:
        key = load_kie_api_key()
        print(str(exc).replace(key, "[redacted]") if key else str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
