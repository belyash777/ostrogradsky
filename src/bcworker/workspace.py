"""Prepare the claude workspace under /data.

The image ships a static template (CLAUDE.md, .mcp.json) that the non-root user
cannot write to; on startup we copy it into the writable ``/data`` workspace (if
absent) and create the directories the syncer and code-save step write into. The
CLAUDE.md is only seeded when missing, so a version already refreshed from
Basecamp is never clobbered.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .config import Config

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_DIR = Path("/app/workspace-template")
_SEED_FILES = ("CLAUDE.md", ".mcp.json")


def template_dir() -> Path:
    """Directory holding the baked workspace template."""
    return Path(os.environ.get("WORKSPACE_TEMPLATE_DIR") or DEFAULT_TEMPLATE_DIR)


def ensure_workspace(config: Config) -> None:
    """Create the workspace layout and seed template files if missing."""
    workspace = config.claude_workspace_dir
    for path in (
        workspace,
        workspace / "documents",
        workspace / ".claude" / "skills",
        workspace / "results",
        config.claude_config_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    template = template_dir()
    for name in _SEED_FILES:
        src = template / name
        dest = workspace / name
        if src.is_file() and not dest.exists():
            shutil.copyfile(src, dest)
            logger.info("Seeded workspace %s from template", name)
