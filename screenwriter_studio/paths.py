"""Filesystem locations shared by the workflow and desk.

Keep productions beside the checkout so existing projects remain discoverable.
Mutable desk state stays outside the importable package.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
STATE_DIR = PROJECT_ROOT / ".state"
