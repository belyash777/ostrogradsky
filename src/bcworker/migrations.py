"""SQL migration runner.

Applies ``*.sql`` files from the migrations directory in lexicographic order,
recording each applied file in ``schema_migrations`` so re-runs are idempotent.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# Repository migrations directory: <repo>/migrations, resolved relative to this
# file (src/bcworker/migrations.py -> ../../migrations).
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    """Create the schema_migrations bookkeeping table if it is missing."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def _applied_versions(conn: sqlite3.Connection) -> set[str]:
    """Return the set of migration versions already recorded as applied."""
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def apply_migrations(
    conn: sqlite3.Connection, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> list[str]:
    """Apply any pending migrations and return the versions that were applied.

    Each ``.sql`` file is executed inside a single transaction together with its
    schema_migrations bookkeeping insert, so a file is only ever marked applied
    if it ran to completion.
    """
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {migrations_dir}")

    _ensure_tracking_table(conn)
    applied = _applied_versions(conn)

    pending = sorted(p for p in migrations_dir.glob("*.sql") if p.stem not in applied)

    newly_applied: list[str] = []
    for path in pending:
        version = path.stem
        script = path.read_text(encoding="utf-8")
        with conn:  # commit on success, rollback on exception
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
        newly_applied.append(version)

    return newly_applied
