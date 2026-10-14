"""Command-line entry point for the crew and local desk."""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

from screenwriter_studio.paths import WORKSPACE_ROOT
from screenwriter_studio.studio import ScreenwriterStudio


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the screenwriter crew")
    parser.add_argument("idea", nargs="?", help="The piece to develop")
    parser.add_argument("--pages-only", action="store_true", help="Stop after the script editor")
    parser.add_argument("--dry-run", action="store_true", help="Plan stills without image credits; chat calls still incur charges")
    parser.add_argument("--aspect", default=None, choices=["16:9", "9:16", "1:1"], help="Lock 16:9, 9:16, or 1:1")
    parser.add_argument("--runtime", type=int, default=None, help="Lock runtime in seconds")
    parser.add_argument("--max-shots", type=int, default=None, help="Maximum shots/keyframes; default is one per 8 seconds")
    parser.add_argument("--inspection", choices=["manual", "auto"], default="manual", help="Inspect every still; manual approves repairs, auto allows up to two")
    parser.add_argument("--no-approval", action="store_true",
                        help="Continue straight into paid image work when the editor passes the pages. "
                             "By default the run pauses with the pages; continue it with --proceed FOLDER.")
    parser.add_argument("--ui", action="store_true", help="Open the browser desk")
    parser.add_argument(
        "--proceed",
        metavar="FOLDER",
        help="Continue a saved production into stills, including one the editor did not pass",
    )
    return parser.parse_args()


async def _stream(idea: str, pages_only: bool, dry_run: bool, aspect: str | None, runtime: int | None, max_shots: int | None = None, inspection_mode: str = "manual", approval: bool = True) -> None:
    studio = ScreenwriterStudio()
    async for chunk in studio.run(
        idea,
        pages_only=pages_only,
        dry_run=dry_run,
        aspect_ratio=aspect,
        runtime_seconds=runtime,
        max_shots=max_shots,
        inspection_mode=inspection_mode,
        approval=approval,
    ):
        print(chunk.text, end="", flush=True)


async def _proceed(folder: str, dry_run: bool, inspection_mode: str) -> None:
    path = Path(folder)
    if not path.is_absolute():
        # Paths are relative to where you run the command, like every other command;
        # the workspace root is kept as a fallback for older instructions.
        path = Path.cwd() / folder if (Path.cwd() / folder).is_dir() else WORKSPACE_ROOT / folder
    studio = ScreenwriterStudio()
    async for chunk in studio.proceed(path, dry_run=dry_run, inspection_mode=inspection_mode):
        print(chunk.text, end="", flush=True)


def main() -> int:
    args = _parse()
    if args.ui:
        from screenwriter_studio.web.server import serve

        serve()
    elif args.proceed:
        if args.idea:
            print("Pass either an idea or --proceed, not both.", file=sys.stderr)
            return 2
        try:
            asyncio.run(_proceed(args.proceed, args.dry_run, args.inspection))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
    elif not args.idea:
        print("Pass an idea, or run with --ui.", file=sys.stderr)
        return 2
    else:
        try:
            asyncio.run(_stream(args.idea, args.pages_only, args.dry_run, args.aspect, args.runtime, args.max_shots,
                                args.inspection, approval=not args.no_approval))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
