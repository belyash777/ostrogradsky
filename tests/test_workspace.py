"""Tests for workspace preparation."""

from __future__ import annotations

from pathlib import Path

import pytest

from bcworker.config import Config
from bcworker.workspace import ensure_workspace


def _config(tmp_path: Path) -> Config:
    return Config(
        claude_workspace_dir=tmp_path / "workspace",
        claude_config_dir=tmp_path / "claude",
    )


def test_creates_layout_and_seeds_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / "CLAUDE.md").write_text("baked guidance", encoding="utf-8")
    (template / ".mcp.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_TEMPLATE_DIR", str(template))

    ensure_workspace(_config(tmp_path))

    ws = tmp_path / "workspace"
    assert (ws / "documents").is_dir()
    assert (ws / ".claude" / "skills").is_dir()
    assert (ws / "snippets").is_dir()
    assert (tmp_path / "claude").is_dir()
    assert (ws / "CLAUDE.md").read_text() == "baked guidance"
    assert (ws / ".mcp.json").read_text() == "{}"


def test_does_not_overwrite_existing_claude_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / "CLAUDE.md").write_text("baked", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_TEMPLATE_DIR", str(template))

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("synced from basecamp", encoding="utf-8")

    ensure_workspace(_config(tmp_path))

    assert (ws / "CLAUDE.md").read_text() == "synced from basecamp"
