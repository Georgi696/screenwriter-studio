"""Filesystem locations shared by the workflow and desk.

Productions live in <WORKSPACE_ROOT>/productions. By default that is the folder beside the
checkout, so existing projects remain discoverable; set STUDIO_WORKSPACE to keep them elsewhere.
Mutable desk state stays outside the importable package.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_configured = os.environ.get("STUDIO_WORKSPACE", "").strip()
WORKSPACE_ROOT = Path(_configured).expanduser().resolve() if _configured else PROJECT_ROOT.parent
STATE_DIR = PROJECT_ROOT / ".state"
