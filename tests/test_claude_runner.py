"""Tests for the Claude Code runner."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import read_cli_records

from bcworker.claude_runner import ClaudeError, ClaudeRunner, session_id_for


def test_session_id_is_deterministic_and_valid_uuid() -> None:
    first = session_id_for(42)
    assert first == session_id_for(42)
    assert session_id_for(42) != session_id_for(43)
    # Must be a real UUID so `claude --session-id` accepts it.
    assert str(uuid.UUID(first)) == first


async def test_run_task_returns_stdout(make_runner: Callable[..., ClaudeRunner]) -> None:
    os.environ["FAKE_STDOUT"] = "The answer is 12345"
    runner = make_runner()
    result = await runner.run_task("count things", session_id_for(1))
    assert result == "The answer is 12345"


async def test_run_task_command_and_env(
    make_runner: Callable[..., ClaudeRunner], cli_records: Path, tmp_path: Path
) -> None:
    os.environ["FAKE_STDOUT"] = "ok"
    runner = make_runner(permission_mode="acceptEdits")
    sid = session_id_for(7)
    await runner.run_task("do the work", sid)

    (rec,) = read_cli_records(cli_records)
    argv = rec["argv"]
    assert argv[0] == "-p"
    assert argv[1] == "do the work"
    assert "--session-id" in argv and sid in argv
    assert "--output-format" in argv and "text" in argv
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert "--add-dir" in argv
    # Runs inside the workspace, with the OAuth config dir exported.
    assert rec["cwd"] == str((tmp_path / "workspace").resolve())
    assert rec["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude")


async def test_resume_command(
    make_runner: Callable[..., ClaudeRunner], cli_records: Path
) -> None:
    os.environ["FAKE_STDOUT"] = "done"
    runner = make_runner()
    await runner.resume(session_id_for(3), "also add a chart")

    (rec,) = read_cli_records(cli_records)
    argv = rec["argv"]
    assert argv[0] == "-p"
    assert "--resume" in argv
    assert "also add a chart" in argv


async def test_timeout_raises(make_runner: Callable[..., ClaudeRunner]) -> None:
    os.environ["FAKE_SLEEP"] = "5"
    runner = make_runner(timeout_seconds=1)
    with pytest.raises(ClaudeError, match="timed out"):
        await runner.run_task("slow", session_id_for(1))


async def test_nonzero_exit_raises(make_runner: Callable[..., ClaudeRunner]) -> None:
    os.environ["FAKE_EXIT"] = "2"
    os.environ["FAKE_STDERR"] = "boom"
    runner = make_runner()
    with pytest.raises(ClaudeError, match="boom"):
        await runner.run_task("x", session_id_for(1))


async def test_missing_binary_raises(tmp_path: Path) -> None:
    runner = ClaudeRunner(
        bin_path="/nonexistent/claude-xyz",
        workspace_dir=tmp_path,
        config_dir=tmp_path / "claude",
    )
    with pytest.raises(ClaudeError, match="not found"):
        await runner.run_task("x", session_id_for(1))
