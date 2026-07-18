"""Tests for the SQL migration runner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bcworker.migrations import DEFAULT_MIGRATIONS_DIR, apply_migrations


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_applies_repo_migrations(raw_conn: sqlite3.Connection) -> None:
    applied = apply_migrations(raw_conn)
    assert "0001_init" in applied
    assert "0002_claude_feature" in applied
    tables = _table_names(raw_conn)
    assert {"schema_migrations", "processed_todos", "synced_files", "code_save_flow"} <= tables
    # 0002 extends processed_todos with the Claude-feature columns.
    assert {"description", "session_id", "bucket_id", "result", "last_comment_id"} <= _columns(
        raw_conn, "processed_todos"
    )


def test_is_idempotent(raw_conn: sqlite3.Connection) -> None:
    first = apply_migrations(raw_conn)
    second = apply_migrations(raw_conn)
    assert set(first) == {"0001_init", "0002_claude_feature"}
    assert second == []  # nothing new applied on the second run


def test_applies_in_lexicographic_order(raw_conn: sqlite3.Connection, tmp_path: Path) -> None:
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0002_second.sql").write_text("CREATE TABLE b (id INTEGER);", encoding="utf-8")
    (mig / "0001_first.sql").write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")

    applied = apply_migrations(raw_conn, mig)
    assert applied == ["0001_first", "0002_second"]


def test_missing_directory_raises(raw_conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        apply_migrations(raw_conn, tmp_path / "does-not-exist")


def test_failed_migration_is_not_recorded(
    raw_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0001_bad.sql").write_text(
        "CREATE TABLE ok1 (id INTEGER);\nTHIS IS NOT VALID SQL;", encoding="utf-8"
    )

    with pytest.raises(sqlite3.Error):
        apply_migrations(raw_conn, mig)

    # The version must not be recorded as applied when the script fails.
    versions = {
        r[0] for r in raw_conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert "0001_bad" not in versions


def test_repo_migrations_dir_exists() -> None:
    assert DEFAULT_MIGRATIONS_DIR.is_dir()
