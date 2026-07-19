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
    (template / "documents").mkdir(parents=True)
    (template / "CLAUDE.md").write_text("baked guidance", encoding="utf-8")
    (template / ".mcp.json").write_text("{}", encoding="utf-8")
    (template / "documents" / "MYSQL.md").write_text("baked mysql", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_TEMPLATE_DIR", str(template))

    ensure_workspace(_config(tmp_path))

    ws = tmp_path / "workspace"
    assert (ws / "documents").is_dir()
    assert (ws / ".claude" / "skills").is_dir()
    assert (ws / "results").is_dir()
    assert (tmp_path / "claude").is_dir()
    assert (ws / "CLAUDE.md").read_text() == "baked guidance"
    assert (ws / ".mcp.json").read_text() == "{}"
    assert (ws / "documents" / "MYSQL.md").read_text() == "baked mysql"


def test_refreshes_claude_md_and_mysql_from_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template"
    (template / "documents").mkdir(parents=True)
    (template / "CLAUDE.md").write_text("new guidance", encoding="utf-8")
    (template / "documents" / "MYSQL.md").write_text("new mysql", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_TEMPLATE_DIR", str(template))

    ws = tmp_path / "workspace"
    (ws / "documents").mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("stale guidance", encoding="utf-8")
    (ws / "documents" / "MYSQL.md").write_text("stale mysql", encoding="utf-8")

    ensure_workspace(_config(tmp_path))

    assert (ws / "CLAUDE.md").read_text() == "new guidance"
    assert (ws / "documents" / "MYSQL.md").read_text() == "new mysql"


def test_seeds_mcp_json_only_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / ".mcp.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_TEMPLATE_DIR", str(template))

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".mcp.json").write_text('{"local": true}', encoding="utf-8")

    ensure_workspace(_config(tmp_path))

    assert (ws / ".mcp.json").read_text() == '{"local": true}'
