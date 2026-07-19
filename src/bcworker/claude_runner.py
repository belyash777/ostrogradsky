"""Run Claude Code (`claude -p`) as the task handler.

A task's Notes are passed to ``claude -p`` and its stdout becomes the result
posted back to Basecamp. Each
to-do gets a deterministic ``--session-id`` so the same session can be resumed
later — both for customer follow-up edits and for the code-save step.

Everything goes through :meth:`ClaudeRunner._run`, which mirrors
``BasecampClient._run``: an argv list (no shell), a per-call timeout, and a
kill-on-timeout. ``claude`` is invoked with ``--output-format text``, so its
stdout is the plain result (no JSON envelope to parse).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Fixed namespace so session ids are deterministic across restarts: a resumed or
# recovered task recomputes the same id without any stored state.
NAMESPACE_BCWORKER = uuid.UUID("6f3d9c2e-7b1a-5e84-9a3d-2c1b0f4e8a77")


def session_id_for(todo_id: int) -> str:
    """Return the deterministic claude session id for a to-do."""
    return str(uuid.uuid5(NAMESPACE_BCWORKER, str(todo_id)))


class ClaudeError(RuntimeError):
    """Raised when a `claude` invocation fails or times out."""


class ClaudeRunner:
    """Runs `claude -p` for tasks and resumes sessions for edits/code-save."""

    def __init__(
        self,
        bin_path: str,
        workspace_dir: Path,
        config_dir: Path,
        timeout_seconds: int = 900,
        permission_mode: str = "acceptEdits",
        extra_env: dict[str, str] | None = None,
    ):
        self._bin = bin_path
        self._workspace = workspace_dir
        self._config_dir = config_dir
        self._timeout = timeout_seconds
        self._permission_mode = permission_mode
        self._extra_env = dict(extra_env or {})

    def _env(self) -> dict[str, str]:
        """Environment for the claude subprocess.

        CLAUDE_CONFIG_DIR points at the mounted OAuth credentials so the worker
        reuses the one-time subscription login. MCP server credentials are read
        from the ambient environment (``${VAR}`` in ``.mcp.json``); any explicit
        overrides in ``extra_env`` are layered on top.
        """
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(self._config_dir)
        env.update(self._extra_env)
        return env

    async def run_task(self, task_text: str, session_id: str) -> str:
        """Run a task through `claude -p` and return its stdout as the result."""
        return await self._run(
            "-p",
            task_text,
            "--session-id",
            session_id,
            "--output-format",
            "text",
            "--add-dir",
            str(self._workspace),
            "--permission-mode",
            self._permission_mode,
        )

    async def resume(self, session_id: str, instruction: str) -> str:
        """Continue an existing session with a new instruction; return stdout.

        Used for customer follow-up edits (the comment text) and for the
        code-save step (an instruction to save the used script into results/).
        """
        return await self._run(
            "-p",
            "--resume",
            session_id,
            instruction,
            "--output-format",
            "text",
            "--add-dir",
            str(self._workspace),
            "--permission-mode",
            self._permission_mode,
        )

    async def _run(self, *args: str) -> str:
        """Run `claude <args>` in the workspace and return decoded stdout."""
        cmd = (self._bin, *args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._workspace),
                env=self._env(),
            )
        except FileNotFoundError as exc:
            raise ClaudeError(f"Claude CLI not found: {self._bin!r}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise ClaudeError(f"Claude command timed out after {self._timeout}s") from None

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip() or "no error detail"
            raise ClaudeError(f"Claude command failed (exit {proc.returncode}): {detail}")

        return stdout.decode("utf-8", "replace").strip()
