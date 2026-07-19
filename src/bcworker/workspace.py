"""Prepare the claude workspace under /data.

The image ships a static template that the non-root user cannot write to; on
startup we copy it into the writable ``/data`` workspace and create the
directories the code-save step writes into.

Two seeding policies:

* ``_SEED_FILES`` are copied only when missing, so local edits survive restarts
  (e.g. ``.mcp.json`` may carry hand-tuned MCP credentials).
* ``_REFRESH_FILES`` are copied on **every** start, overwriting the workspace
  copy from the baked template. This is how ``docker compose up --build`` pushes
  an updated ``CLAUDE.md`` / ``documents/MYSQL.md`` into ``./data``.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .config import Config

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_DIR = Path("/app/workspace-template")
# Copied only when the destination does not yet exist.
_SEED_FILES = (".mcp.json",)
# Copied on every start, overwriting the workspace copy from the template.
_REFRESH_FILES = ("CLAUDE.md", "documents/MYSQL.md")


def template_dir() -> Path:
    """Directory holding the baked workspace template."""
    return Path(os.environ.get("WORKSPACE_TEMPLATE_DIR") or DEFAULT_TEMPLATE_DIR)


def ensure_workspace(config: Config) -> None:
    """Create the workspace layout and copy template files into it."""
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

    for name in _REFRESH_FILES:
        src = template / name
        dest = workspace / name
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            logger.info("Refreshed workspace %s from template", name)
