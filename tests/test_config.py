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


def test_non_integer_interval_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "abc")
    with pytest.raises(ValueError, match="integer"):
        Config.from_env()


def test_non_positive_interval_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    with pytest.raises(ValueError, match="positive"):
        Config.from_env()
