"""Runtime configuration, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .migrations import DEFAULT_MIGRATIONS_DIR

# Messages posted back to the Basecamp to-do. Kept here so the wording lives in
# one place. Ukrainian is intentional: these are user-facing task comments.
ACCEPTED_MESSAGE = "Задача прийнята, виконую роботу"
COMPLETED_MESSAGE = "Роботу виконано"


def _get_int(name: str, default: int) -> int:
    """Return an int env var, falling back to ``default`` when unset/blank."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name!r} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"Environment variable {name!r} must be positive, got {value}")
    return value


def _get_int_or_zero(name: str) -> int:
    """Return a non-negative int env var, defaulting to 0 when unset/blank."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name!r} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"Environment variable {name!r} must not be negative, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable worker configuration resolved from the environment."""

    poll_interval_seconds: int = 5
    db_path: Path = Path("/data/bcworker.sqlite3")
    basecamp_bin: str = "basecamp"
    # Credentials dir; its basename must be `basecamp` (the CLI reads
    # $XDG_CONFIG_HOME/basecamp). Kept under /data so all local state is together.
    basecamp_config_dir: Path = Path("/data/basecamp")
    # Basecamp account id. Optional: when empty the worker auto-detects it if the
    # authenticated user has exactly one account.
    basecamp_account_id: str = ""
    basecamp_timeout_seconds: int = 30
    # The single Basecamp project the worker serves: it takes tasks from here and
    # reads the skills/documents/CLAUDE.md files here. 0 disables project
    # filtering (every assigned to-do is processed) — used mostly in tests.
    basecamp_project_id: int = 0
    log_level: str = "INFO"
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR

    # Claude Code invocation.
    claude_bin: str = "claude"
    claude_timeout_seconds: int = 900
    claude_workspace_dir: Path = Path("/data/workspace")
    claude_config_dir: Path = Path("/data/claude")
    claude_permission_mode: str = "acceptEdits"

    # Follow-up edits and the code-save lifecycle.
    comment_poll_seconds: int = 30
    codesave_poll_seconds: int = 30
    code_save_delay_seconds: int = 60
    # How long to wait for the customer's answer to the "save the code?" prompt
    # before giving up (and not saving). Default: 7 days.
    code_save_reply_timeout_seconds: int = 604800
    task_max_concurrency: int = 1

    @classmethod
    def from_env(cls) -> Config:
        """Build a Config from process environment variables."""
        return cls(
            poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 5),
            db_path=Path(os.environ.get("DB_PATH", "/data/bcworker.sqlite3")),
            basecamp_bin=os.environ.get("BASECAMP_BIN", "basecamp").strip() or "basecamp",
            basecamp_config_dir=Path(
                os.environ.get("BASECAMP_CONFIG_DIR", "/data/basecamp")
            ),
            basecamp_account_id=os.environ.get("BASECAMP_ACCOUNT_ID", "").strip(),
            basecamp_timeout_seconds=_get_int("BASECAMP_TIMEOUT_SECONDS", 30),
            basecamp_project_id=_get_int_or_zero("BASECAMP_PROJECT_ID"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            # Migrations live outside the installed package, so the location is
            # explicit in Docker (set via MIGRATIONS_DIR) and defaults to the
            # repo layout for local runs.
            migrations_dir=Path(os.environ.get("MIGRATIONS_DIR") or DEFAULT_MIGRATIONS_DIR),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude").strip() or "claude",
            claude_timeout_seconds=_get_int("CLAUDE_TIMEOUT_SECONDS", 900),
            claude_workspace_dir=Path(
                os.environ.get("CLAUDE_WORKSPACE_DIR", "/data/workspace")
            ),
            claude_config_dir=Path(os.environ.get("CLAUDE_CONFIG_DIR", "/data/claude")),
            claude_permission_mode=(
                os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits").strip() or "acceptEdits"
            ),
            comment_poll_seconds=_get_int("COMMENT_POLL_SECONDS", 30),
            codesave_poll_seconds=_get_int("CODESAVE_POLL_SECONDS", 30),
            code_save_delay_seconds=_get_int("CODE_SAVE_DELAY_SECONDS", 60),
            code_save_reply_timeout_seconds=_get_int(
                "CODE_SAVE_REPLY_TIMEOUT_SECONDS", 604800
            ),
            task_max_concurrency=_get_int("TASK_MAX_CONCURRENCY", 1),
        )
