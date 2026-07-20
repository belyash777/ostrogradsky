"""Tests for environment-driven configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from bcworker.config import Config


def test_defaults_when_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "POLL_INTERVAL_SECONDS",
        "DB_PATH",
        "BASECAMP_BIN",
        "BASECAMP_CONFIG_DIR",
        "BASECAMP_TIMEOUT_SECONDS",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env()
    assert cfg.poll_interval_seconds == 5
    assert cfg.basecamp_bin == "basecamp"
    assert cfg.log_level == "INFO"


def test_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("DB_PATH", "/tmp/x.sqlite3")
    monkeypatch.setenv("BASECAMP_BIN", "/usr/local/bin/basecamp")
    monkeypatch.setenv("BASECAMP_ACCOUNT_ID", "999")
    monkeypatch.setenv("BASECAMP_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("MIGRATIONS_DIR", "/app/migrations")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    cfg = Config.from_env()
    assert cfg.poll_interval_seconds == 10
    assert cfg.db_path == Path("/tmp/x.sqlite3")
    assert cfg.basecamp_bin == "/usr/local/bin/basecamp"
    assert cfg.basecamp_account_id == "999"
    assert cfg.basecamp_timeout_seconds == 45
    assert cfg.migrations_dir == Path("/app/migrations")
    assert cfg.log_level == "DEBUG"


def test_claude_and_sync_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BASECAMP_PROJECT_ID",
        "CLAUDE_BIN",
        "CLAUDE_TIMEOUT_SECONDS",
        "CODE_SAVE_DELAY_SECONDS",
        "TASK_MAX_CONCURRENCY",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env()
    assert cfg.basecamp_project_id == 0
    assert cfg.claude_bin == "claude"
    assert cfg.claude_timeout_seconds == 900
    assert cfg.claude_workspace_dir == Path("/data/workspace")
    assert cfg.code_save_delay_seconds == 60
    assert cfg.task_max_concurrency == 1


def test_reads_claude_and_sync_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASECAMP_PROJECT_ID", "12345")
    monkeypatch.setenv("CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")
    monkeypatch.setenv("CODE_SAVE_DELAY_SECONDS", "600")
    cfg = Config.from_env()
    assert cfg.basecamp_project_id == 12345
    assert cfg.claude_bin == "/usr/local/bin/claude"
    assert cfg.claude_timeout_seconds == 120
    assert cfg.claude_permission_mode == "bypassPermissions"
    assert cfg.code_save_delay_seconds == 600


def test_negative_project_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASECAMP_PROJECT_ID", "-1")
    with pytest.raises(ValueError, match="negative"):
        Config.from_env()


def test_non_integer_interval_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "abc")
    with pytest.raises(ValueError, match="integer"):
        Config.from_env()


def test_non_positive_interval_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    with pytest.raises(ValueError, match="positive"):
        Config.from_env()
