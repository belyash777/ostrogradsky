"""Tests for the task-handler stub."""

from __future__ import annotations

import pytest

from bcworker.stub import run_task


async def test_run_task_prints_hello_world(capsys: pytest.CaptureFixture[str]) -> None:
    await run_task("Any task text")
    out = capsys.readouterr().out
    assert out.strip() == "hello world"


async def test_run_task_accepts_empty_text(capsys: pytest.CaptureFixture[str]) -> None:
    await run_task("")
    assert capsys.readouterr().out.strip() == "hello world"
