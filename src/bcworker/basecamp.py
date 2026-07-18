"""Async wrapper around the Basecamp CLI (`basecamp`).

Each method shells out to the CLI with the global ``--json`` flag and parses the
response envelope (``{"ok": ..., "data": ..., "summary": ...}``). All commands
run through :meth:`BasecampClient._run`, which enforces a per-call timeout and
raises :class:`BasecampError` on any failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class BasecampError(RuntimeError):
    """Raised when a Basecamp CLI invocation fails or returns a non-ok envelope."""


@dataclass(frozen=True, slots=True)
class Todo:
    """A Basecamp to-do, normalised from the CLI JSON output."""

    id: int
    title: str
    description: str = ""
    # The project (bucket) the to-do lives in. Project-scoped CLI commands
    # (`todos show`, `comments create`, `files ...`) need it via `--in`.
    bucket_id: int = 0
    bucket_name: str = ""
    # Whether Basecamp reports the to-do as completed (closed by the customer).
    completed: bool = False

    @property
    def task_text(self) -> str:
        """The full text handed to the task handler (title plus any body)."""
        if self.description:
            return f"{self.title}\n\n{self.description}".strip()
        return self.title


def _extract_todos(data: object) -> list[dict]:
    """Pull the list of raw to-do dicts out of a `reports assigned` envelope.

    The CLI may serialise ``data`` either as a bare list or as an object with a
    ``todos`` key, so both shapes are accepted.
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        todos = data.get("todos")
        if isinstance(todos, list):
            return [item for item in todos if isinstance(item, dict)]
    return []


def _extract_files(data: object) -> list[dict]:
    """Pull the list of Docs & Files entries out of a `files list` envelope.

    The CLI may serialise ``data`` as a bare list or as an object with an
    ``entries``/``files`` key, so all shapes are accepted.
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("entries", "files"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _coerce_int(value: object) -> int:
    """Return an int for a real int value (rejecting bool), else 0."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _todo_from_dict(raw: dict) -> Todo | None:
    """Build a Todo from a raw CLI dict, or None if it lacks a usable id."""
    todo_id = raw.get("id")
    # bool is a subclass of int; reject it so `{"id": true}` is not treated as 1.
    if not isinstance(todo_id, int) or isinstance(todo_id, bool):
        return None
    # The Basecamp todo model exposes `content`, not `title`; keep `title` as a
    # forward-compatible fallback.
    title = str(raw.get("title") or raw.get("content") or "").strip()
    description = str(raw.get("description") or "").strip()
    bucket = raw.get("bucket") if isinstance(raw.get("bucket"), dict) else {}
    return Todo(
        id=todo_id,
        title=title,
        description=description,
        bucket_id=_coerce_int(bucket.get("id")),
        bucket_name=str(bucket.get("name") or "").strip(),
        completed=bool(raw.get("completed")),
    )


class BasecampClient:
    """Runs Basecamp CLI commands and returns parsed results."""

    def __init__(
        self,
        bin_path: str,
        config_dir: Path,
        account_id: str = "",
        timeout_seconds: int = 30,
    ):
        self._bin = bin_path
        self._config_dir = config_dir
        self._account_id = account_id
        self._timeout = timeout_seconds

    @property
    def account_id(self) -> str:
        """The account id currently used for account-scoped commands."""
        return self._account_id

    def _env(self) -> dict[str, str]:
        """Environment for the CLI subprocess.

        - XDG_CONFIG_HOME selects the mounted credentials volume; the CLI reads
          ``$XDG_CONFIG_HOME/basecamp`` (so ``config_dir`` must end in
          ``/basecamp``, which the defaults guarantee).
        - BASECAMP_NO_KEYRING forces the file-based credential store so the
          `auth` container and the worker share credentials deterministically,
          regardless of whether an OS keyring is present.
        - BASECAMP_NONINTERACTIVE prevents the CLI from ever blocking on a prompt.
        - BASECAMP_ACCOUNT_ID supplies the account context required by
          account-scoped commands (`reports assigned`, `comments create`).
        """
        env = dict(os.environ)
        env["XDG_CONFIG_HOME"] = str(self._config_dir.parent)
        env["BASECAMP_NO_KEYRING"] = "1"
        env["BASECAMP_NONINTERACTIVE"] = "1"
        if self._account_id:
            env["BASECAMP_ACCOUNT_ID"] = self._account_id
        return env

    async def _run(self, *args: str) -> object:
        """Run ``basecamp <args> --json`` and return the parsed ``data`` field."""
        cmd = (self._bin, *args, "--json")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
        except FileNotFoundError as exc:
            raise BasecampError(f"Basecamp CLI not found: {self._bin!r}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            # The child may already have exited in the race window; ignore that.
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise BasecampError(
                f"Basecamp command timed out after {self._timeout}s: {' '.join(args)}"
            ) from None

        if proc.returncode != 0:
            raise BasecampError(self._error_message(proc.returncode, stdout, stderr, args))

        return self._parse_envelope(stdout, args)

    @staticmethod
    def _error_message(
        returncode: int, stdout: bytes, stderr: bytes, args: tuple[str, ...]
    ) -> str:
        """Build a helpful error string from a failed command.

        On failure the CLI writes a JSON error envelope
        (``{"ok": false, "error": ..., "hint": ...}``) to *stdout*, so prefer it
        over stderr, which is usually empty.
        """
        detail = ""
        text = stdout.decode("utf-8", "replace").strip()
        if text:
            try:
                envelope = json.loads(text)
            except json.JSONDecodeError:
                envelope = None
            if isinstance(envelope, dict) and envelope.get("ok") is False:
                error = str(envelope.get("error") or "").strip()
                hint = str(envelope.get("hint") or "").strip()
                detail = f"{error} ({hint})" if hint else error
        if not detail:
            detail = stderr.decode("utf-8", "replace").strip() or "no error detail"
        return f"Basecamp command failed (exit {returncode}): {' '.join(args)}: {detail}"

    @staticmethod
    def _parse_envelope(stdout: bytes, args: tuple[str, ...]) -> object:
        """Validate the JSON envelope and return its ``data`` payload."""
        text = stdout.decode("utf-8", "replace").strip()
        if not text:
            raise BasecampError(f"Empty output from: {' '.join(args)}")
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BasecampError(f"Invalid JSON from {' '.join(args)}: {exc}") from exc
        if not isinstance(envelope, dict):
            raise BasecampError(f"Unexpected JSON shape from {' '.join(args)}: {type(envelope)}")
        if envelope.get("ok") is False:
            summary = envelope.get("summary") or envelope.get("error") or "unknown error"
            raise BasecampError(f"Basecamp reported failure for {' '.join(args)}: {summary}")
        return envelope.get("data")

    async def auth_status(self) -> dict:
        """Return the parsed `auth status` payload (includes ``authenticated``)."""
        data = await self._run("auth", "status")
        return data if isinstance(data, dict) else {}

    async def is_authenticated(self) -> bool:
        """Return True when the CLI reports a usable authenticated session."""
        try:
            status = await self.auth_status()
        except BasecampError:
            logger.warning("Could not determine auth status", exc_info=True)
            return False
        return bool(status.get("authenticated"))

    async def list_accounts(self) -> list[dict]:
        """Return the accounts the authenticated user can access."""
        data = await self._run("accounts", "list")
        return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []

    async def ensure_account(self) -> str:
        """Resolve and cache the account id for account-scoped commands.

        Account-scoped commands require an account, and in non-interactive mode
        the CLI will not pick one automatically. If no account was configured,
        auto-detect it when the user has exactly one; otherwise raise with a
        clear instruction to set BASECAMP_ACCOUNT_ID.
        """
        if self._account_id:
            return self._account_id

        accounts = await self.list_accounts()
        if not accounts:
            raise BasecampError("No Basecamp accounts are accessible for this login")
        if len(accounts) > 1:
            ids = ", ".join(str(a.get("id")) for a in accounts)
            raise BasecampError(
                "Multiple Basecamp accounts are accessible "
                f"({ids}); set BASECAMP_ACCOUNT_ID to choose one"
            )

        self._account_id = str(accounts[0].get("id"))
        return self._account_id

    async def assigned_todos(self) -> list[Todo]:
        """Return to-dos assigned to the current (CLI) account, all projects."""
        # `reports assigned` with no person defaults to "me" (the CLI account).
        data = await self._run("reports", "assigned")
        todos: list[Todo] = []
        for raw in _extract_todos(data):
            todo = _todo_from_dict(raw)
            if todo is not None:
                todos.append(todo)
        return todos

    async def create_comment(
        self, todo_id: int, text: str, project_id: int | None = None
    ) -> None:
        """Post a comment onto the given to-do (recording)."""
        args = ["comments", "create", str(todo_id), text]
        if project_id:
            args += ["--in", str(project_id)]
        await self._run(*args)

    async def list_comments(self, recording_id: int, project_id: int) -> list[dict]:
        """Return the comments on a recording (to-do), oldest-to-newest per the CLI."""
        data = await self._run("comments", "list", str(recording_id), "--in", str(project_id))
        return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []

    async def list_todos(self, project_id: int) -> list[Todo]:
        """List the to-dos in a project (used to make child-to-do creation idempotent)."""
        data = await self._run("todos", "list", "--in", str(project_id))
        todos: list[Todo] = []
        for raw in _extract_todos(data):
            todo = _todo_from_dict(raw)
            if todo is not None:
                todos.append(todo)
        return todos

    async def show_todo(self, todo_id: int, project_id: int) -> dict:
        """Return the full to-do payload (includes `description` and `completed`)."""
        data = await self._run("todos", "show", str(todo_id), "--in", str(project_id))
        return data if isinstance(data, dict) else {}

    async def is_todo_completed(self, todo_id: int, project_id: int) -> bool:
        """Return True when Basecamp reports the to-do as completed (closed)."""
        return bool((await self.show_todo(todo_id, project_id)).get("completed"))

    async def create_todo(
        self,
        project_id: int,
        content: str,
        list_id: int | None = None,
        assignee_ids: list[int] | None = None,
    ) -> int:
        """Create a to-do in the project and return its new id.

        `list_id`/`assignee_ids` are best-effort: exact flag names must be
        verified against the CLI. When assignment is unavailable the to-do is
        still created; the code-save flow relies on an id-skip guard, not on the
        assignee, so correctness does not depend on it.
        """
        args = ["todos", "create", content, "--in", str(project_id)]
        if list_id:
            args += ["--list", str(list_id)]
        for assignee in assignee_ids or []:
            args += ["--assignee", str(assignee)]
        data = await self._run(*args)
        new_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(new_id, int) or isinstance(new_id, bool):
            raise BasecampError(f"todos create did not return an id: {data!r}")
        return new_id

    async def list_files(self, project_id: int, vault_id: int | None = None) -> list[dict]:
        """List Docs & Files entries in a project (or inside a given folder/vault)."""
        args = ["files", "list", "--in", str(project_id)]
        if vault_id:
            args += ["--vault", str(vault_id)]
        data = await self._run(*args)
        return _extract_files(data)

    async def download_file(self, file_id: int, project_id: int, out_dir: Path) -> None:
        """Download an uploaded file into ``out_dir``."""
        await self._run(
            "files", "download", str(file_id), "--in", str(project_id), "--out", str(out_dir)
        )
