"""
Screenwriter studio entry point.

    uv run screenwriter_studio/run.py "a 30-second ad for a dented thermos"
    uv run screenwriter_studio/run.py "idea" --pages-only
    uv run screenwriter_studio/run.py --ui

`--ui` opens the game desk in desk.html. Headless runs still print the same
status lines the desk listens to.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv(override=True)

from studio import ScreenwriterStudio


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the screenwriter crew")
    parser.add_argument("idea", nargs="?", help="The piece to develop")
    parser.add_argument("--pages-only", action="store_true", help="Stop after the script editor")
    parser.add_argument("--dry-run", action="store_true", help="Plan stills without image credits; chat calls still incur charges")
    parser.add_argument("--aspect", default=None, help="Lock 16:9, 9:16, or 1:1")
    parser.add_argument("--runtime", type=int, default=None, help="Lock runtime in seconds")
    parser.add_argument("--max-shots", type=int, default=None, help="Maximum shots/keyframes; default is one per 8 seconds")
    parser.add_argument("--ui", action="store_true", help="Open the game desk")
    return parser.parse_args()


async def _stream(idea: str, pages_only: bool, dry_run: bool, aspect: str | None, runtime: int | None, max_shots: int | None = None) -> None:
    studio = ScreenwriterStudio()
    async for chunk in studio.run(
        idea,
        pages_only=pages_only,
        dry_run=dry_run,
        aspect_ratio=aspect,
        runtime_seconds=runtime,
        max_shots=max_shots,
    ):
        print(chunk.text, end="", flush=True)


if __name__ == "__main__":
    args = _parse()
    if args.ui:
        from desk import serve

        serve()
    elif not args.idea:
        print("Pass an idea, or run with --ui.", file=sys.stderr)
        raise SystemExit(2)
    else:
        asyncio.run(_stream(args.idea, args.pages_only, args.dry_run, args.aspect, args.runtime, args.max_shots))
