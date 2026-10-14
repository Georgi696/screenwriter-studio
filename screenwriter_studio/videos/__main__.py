"""python -m screenwriter_studio.videos PRODUCTION [--dry-run]."""

import argparse
from pathlib import Path
import threading

import sys

from screenwriter_studio.models import load_kie_api_key
from screenwriter_studio.pricing import CHECKED, SOURCE, describe, plan_estimate

from screenwriter_studio.videos.service import attach_task, forget_submission, render_video, use_take


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate/resume one video clip per approved shot image.")
    parser.add_argument("production", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Save an offline video plan; no uploads or paid calls.")
    parser.add_argument("--mode", choices=("std", "pro", "4K"), default=None)
    parser.add_argument("--model", choices=("auto", "kling", "seedance"), default=None,
                        help="Choose per shot automatically (default), or require a compatible model.")
    parser.add_argument("--sound", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--shot", action="append", metavar="ID", help="Render only this shot (repeatable), e.g. --shot s02. ")
    parser.add_argument("--redo", action="append", metavar="ID", help="Regenerate a finished shot (repeatable). The old take is kept as ID_vN.mp4. Paid.")
    parser.add_argument("--forget-submission", metavar="SHOT",
                        help="After checking KIE's task history, mark an uncertain submission as never created so the next run sends it again.")
    parser.add_argument("--attach-task", metavar="SHOT=TASK_ID", help="Recover an uncertain submission using its KIE task ID.")
    parser.add_argument("--prompt", action="append", metavar="SHOT=TEXT",
                        help="Use this prompt for a shot you render or redo (repeatable). The planned prompt is kept beside it.")
    parser.add_argument("--use-take", metavar="SHOT=FILE", help="Make an earlier take (e.g. s02=s02_v1.mp4) the clip again. No provider call.")
    args = parser.parse_args()
    cancel = threading.Event()
    try:
        if args.forget_submission:
            forget_submission(args.production.resolve(), args.forget_submission)
            print("Submission forgotten. Run again to submit that shot.")
            return 0
        if args.use_take:
            shot, separator, name = args.use_take.partition("=")
            if not separator:
                raise ValueError("Use --use-take s02=s02_v1.mp4.")
            use_take(args.production.resolve(), shot, name)
            print(f"{shot} now uses {name}.")
            return 0
        prompts = {}
        for item in args.prompt or []:
            shot, separator, text = item.partition("=")
            if not separator:
                raise ValueError("Use --prompt s02=TEXT.")
            prompts[shot] = text
        if args.attach_task:
            shot, separator, task = args.attach_task.partition("=")
            if not separator:
                raise ValueError("Use --attach-task s01=TASK_ID.")
            attach_task(args.production.resolve(), shot, task)
            print("Task attached. Run again to resume.")
            return 0
        result = render_video(args.production, mode=args.mode, sound=args.sound,
                              selection=args.model, dry_run=args.dry_run, cancel=cancel,
                              shots=args.shot, regenerate=args.redo, prompts=prompts or None)
        print({"planned": "Video plan saved.", "partial": "Clips saved; render the remaining shots to finish the set."}.get(
            result["status"], f"Clips are in {args.production / 'videos'}"))
        if result["status"] == "planned":
            estimate = plan_estimate(result["plan"])
            for shot in result["plan"]["shots"]:
                credits = estimate["shots"].get(shot["id"])
                print(f"  {shot['id']} · {shot['duration_seconds']}s · {shot.get('model_name', shot.get('model', ''))}"
                      + (f" · {describe(credits)}" if credits is not None else ""))
            print(f"Estimated cost of every clip: {describe(estimate['credits'])}. Prices from {SOURCE}, checked {CHECKED}.")
        return 0
    except KeyboardInterrupt:
        cancel.set()
        print("Stopped. Run again to resume saved video tasks.")
        return 130
    except Exception as exc:
        key = load_kie_api_key()
        print(str(exc).replace(key, "[redacted]") if key else str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
