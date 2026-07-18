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
    log_level: str = "INFO"
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR

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
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            # Migrations live outside the installed package, so the location is
            # explicit in Docker (set via MIGRATIONS_DIR) and defaults to the
            # repo layout for local runs.
            migrations_dir=Path(os.environ.get("MIGRATIONS_DIR") or DEFAULT_MIGRATIONS_DIR),
        )
