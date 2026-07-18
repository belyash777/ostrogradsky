"""Async-friendly SQLite persistence for de-duplicating processed to-dos.

The standard library ``sqlite3`` module is synchronous, so every call is run in
a worker thread via :func:`asyncio.to_thread`. A single connection is shared and
guarded by an :class:`asyncio.Lock`, which both serialises access (sqlite3
connections are not safe for concurrent use) and keeps writes ordered.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .basecamp import Todo
from .migrations import DEFAULT_MIGRATIONS_DIR, apply_migrations

# Status values stored in processed_todos.status. They also encode how far a
# to-do got, so an interrupted run can be resumed on the next startup:
#   claimed  -> row inserted, acceptance comment not yet confirmed posted
#   accepted -> acceptance comment posted, task/completion not yet done
#   done     -> fully processed
#   error    -> processing failed (left for inspection, not auto-retried)
STATUS_CLAIMED = "claimed"
STATUS_ACCEPTED = "accepted"
STATUS_DONE = "done"
STATUS_ERROR = "error"

# Statuses that represent an interrupted, resumable to-do.
_PENDING_STATUSES = (STATUS_CLAIMED, STATUS_ACCEPTED)

# code_save_flow.stage values.
FLOW_AWAITING_DELAY = "awaiting_delay"
FLOW_PROMPTS_CREATED = "prompts_created"
FLOW_SAVING = "saving"
FLOW_SAVED = "saved"
FLOW_DISCARDED = "discarded"
FLOW_ERROR = "error"


@dataclass(frozen=True, slots=True)
class PendingTodo:
    """An interrupted to-do to resume, with enough context to run it."""

    todo_id: int
    title: str
    description: str
    bucket_id: int
    status: str

    @property
    def task_text(self) -> str:
        """The full task text, reconstructed from the persisted title + body."""
        if self.description:
            return f"{self.title}\n\n{self.description}".strip()
        return self.title


@dataclass(frozen=True, slots=True)
class CodeSaveFlow:
    """A row of the post-completion code-save lifecycle."""

    todo_id: int
    project_id: int
    session_id: str
    stage: str
    prompt_due_at: str
    save_todo_id: int | None
    discard_todo_id: int | None


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


class Database:
    """Thin async wrapper around a single SQLite connection."""

    def __init__(self, path: Path):
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the live connection, or raise if the DB is not connected."""
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._conn

    async def connect(self) -> None:
        """Open the SQLite connection, creating parent directories as needed."""

        def _open() -> sqlite3.Connection:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: the connection is used from asyncio worker
            # threads, but the asyncio.Lock guarantees non-concurrent access.
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

        self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        """Close the connection if open, waiting for any in-flight operation."""
        async with self._lock:
            if self._conn is None:
                return
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    async def migrate(self, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
        """Apply pending migrations, returning the versions applied."""
        async with self._lock:
            return await asyncio.to_thread(apply_migrations, self.connection, migrations_dir)

    async def try_claim(self, todo: Todo) -> bool:
        """Atomically claim a to-do for processing.

        Inserts the to-do with status 'claimed'. Returns True if this call won
        the claim, or False if the to-do was already recorded (the primary-key
        conflict is the de-duplication guard, robust across restarts).
        """

        def _claim() -> bool:
            try:
                with self.connection as conn:
                    conn.execute(
                        "INSERT INTO processed_todos "
                        "(todo_id, title, description, status, accepted_at, "
                        "bucket_id, bucket_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            todo.id,
                            todo.title,
                            todo.description,
                            STATUS_CLAIMED,
                            _now(),
                            todo.bucket_id or None,
                            todo.bucket_name or None,
                        ),
                    )
                return True
            except sqlite3.IntegrityError:
                return False

        async with self._lock:
            return await asyncio.to_thread(_claim)

    async def pending_todos(self) -> list[PendingTodo]:
        """Return interrupted to-dos to resume, with the context needed to run them."""

        def _pending() -> list[PendingTodo]:
            placeholders = ", ".join("?" for _ in _PENDING_STATUSES)
            rows = self.connection.execute(
                f"SELECT todo_id, title, description, bucket_id, status "
                f"FROM processed_todos WHERE status IN ({placeholders}) ORDER BY todo_id",
                _PENDING_STATUSES,
            ).fetchall()
            return [
                PendingTodo(
                    todo_id=int(r[0]),
                    title=str(r[1]),
                    description=str(r[2] or ""),
                    bucket_id=int(r[3] or 0),
                    status=str(r[4]),
                )
                for r in rows
            ]

        async with self._lock:
            return await asyncio.to_thread(_pending)

    async def mark_accepted(self, todo_id: int) -> None:
        """Mark that the acceptance comment for a claimed to-do was posted."""

        def _accepted() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE processed_todos SET status = ? WHERE todo_id = ?",
                    (STATUS_ACCEPTED, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_accepted)

    async def mark_done(self, todo_id: int, result: str | None = None) -> None:
        """Mark a claimed to-do as successfully completed, storing the result."""

        def _done() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE processed_todos SET status = ?, completed_at = ?, result = ?, "
                    "error = NULL WHERE todo_id = ?",
                    (STATUS_DONE, _now(), result, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_done)

    async def mark_error(self, todo_id: int, error: str) -> None:
        """Record that processing of a claimed to-do failed."""

        def _error() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE processed_todos SET status = ?, error = ? WHERE todo_id = ?",
                    (STATUS_ERROR, error, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_error)

    # --- follow-up edits (new customer comments on a finished to-do) ---------

    async def active_done_todos(self) -> list[tuple[int, int, int | None]]:
        """Return finished to-dos not yet in the code-save flow.

        These are the to-dos still open to follow-up edits and to completion
        detection. Each row is (todo_id, bucket_id, last_comment_id).
        """

        def _query() -> list[tuple[int, int, int | None]]:
            rows = self.connection.execute(
                "SELECT p.todo_id, p.bucket_id, p.last_comment_id FROM processed_todos p "
                "LEFT JOIN code_save_flow f ON f.todo_id = p.todo_id "
                "WHERE p.status = ? AND f.todo_id IS NULL ORDER BY p.todo_id",
                (STATUS_DONE,),
            ).fetchall()
            return [(int(r[0]), int(r[1] or 0), None if r[2] is None else int(r[2])) for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_query)

    async def set_last_comment_id(self, todo_id: int, comment_id: int) -> None:
        """Record the highest customer comment already handled for a to-do."""

        def _update() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE processed_todos SET last_comment_id = ? WHERE todo_id = ?",
                    (comment_id, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_update)

    async def set_session_id(self, todo_id: int, session_id: str) -> None:
        """Persist the claude session id used for a to-do (audit / recovery)."""

        def _update() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE processed_todos SET session_id = ? WHERE todo_id = ?",
                    (session_id, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_update)

    async def set_result(self, todo_id: int, result: str) -> None:
        """Store the latest claude result posted back to a to-do."""

        def _update() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE processed_todos SET result = ? WHERE todo_id = ?",
                    (result, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_update)

    # --- synced skills / documents ------------------------------------------

    async def synced_files_for(self, project_id: int, kind: str) -> dict[int, tuple[str, str, str]]:
        """Return {file_id: (name, checksum, local_path)} for a project + kind."""

        def _query() -> dict[int, tuple[str, str, str]]:
            rows = self.connection.execute(
                "SELECT file_id, name, checksum, local_path FROM synced_files "
                "WHERE project_id = ? AND kind = ?",
                (project_id, kind),
            ).fetchall()
            return {int(r[0]): (str(r[1]), str(r[2] or ""), str(r[3])) for r in rows}

        async with self._lock:
            return await asyncio.to_thread(_query)

    async def upsert_synced_file(
        self,
        project_id: int,
        kind: str,
        file_id: int,
        name: str,
        checksum: str,
        local_path: str,
    ) -> None:
        """Insert or update the record of a synced skill/document file."""

        def _upsert() -> None:
            with self.connection as conn:
                conn.execute(
                    "INSERT INTO synced_files "
                    "(file_id, project_id, kind, name, checksum, local_path, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(project_id, kind, file_id) DO UPDATE SET "
                    "name = excluded.name, checksum = excluded.checksum, "
                    "local_path = excluded.local_path, synced_at = excluded.synced_at",
                    (file_id, project_id, kind, name, checksum, local_path, _now()),
                )

        async with self._lock:
            await asyncio.to_thread(_upsert)

    async def delete_synced_file(self, project_id: int, kind: str, file_id: int) -> None:
        """Forget a synced skill/document file (its remote copy is gone)."""

        def _delete() -> None:
            with self.connection as conn:
                conn.execute(
                    "DELETE FROM synced_files WHERE project_id = ? AND kind = ? AND file_id = ?",
                    (project_id, kind, file_id),
                )

        async with self._lock:
            await asyncio.to_thread(_delete)

    # --- code-save lifecycle ------------------------------------------------

    async def create_flow(
        self,
        todo_id: int,
        project_id: int,
        session_id: str,
        prompt_due_at: str,
    ) -> None:
        """Start a code-save flow for a customer-completed to-do."""

        def _create() -> None:
            with self.connection as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO code_save_flow "
                    "(todo_id, project_id, session_id, stage, customer_completed_at, "
                    "prompt_due_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (todo_id, project_id, session_id, FLOW_AWAITING_DELAY, _now(), prompt_due_at),
                )

        async with self._lock:
            await asyncio.to_thread(_create)

    async def flows_in_stage(self, stage: str) -> list[CodeSaveFlow]:
        """Return code-save flows currently at the given stage."""

        def _query() -> list[CodeSaveFlow]:
            rows = self.connection.execute(
                "SELECT todo_id, project_id, session_id, stage, prompt_due_at, "
                "save_todo_id, discard_todo_id FROM code_save_flow WHERE stage = ? "
                "ORDER BY todo_id",
                (stage,),
            ).fetchall()
            return [
                CodeSaveFlow(
                    todo_id=int(r[0]),
                    project_id=int(r[1]),
                    session_id=str(r[2]),
                    stage=str(r[3]),
                    prompt_due_at=str(r[4]),
                    save_todo_id=None if r[5] is None else int(r[5]),
                    discard_todo_id=None if r[6] is None else int(r[6]),
                )
                for r in rows
            ]

        async with self._lock:
            return await asyncio.to_thread(_query)

    async def set_flow_prompts(
        self, todo_id: int, save_todo_id: int, discard_todo_id: int
    ) -> None:
        """Record the two under-to-dos and advance the flow to prompts_created."""

        def _update() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE code_save_flow SET stage = ?, save_todo_id = ?, discard_todo_id = ? "
                    "WHERE todo_id = ?",
                    (FLOW_PROMPTS_CREATED, save_todo_id, discard_todo_id, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_update)

    async def set_flow_stage(self, todo_id: int, stage: str) -> None:
        """Move a flow to a new stage (no decision recorded)."""

        def _update() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE code_save_flow SET stage = ? WHERE todo_id = ?",
                    (stage, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_update)

    async def resolve_flow(self, todo_id: int, stage: str, decision: str) -> None:
        """Finish a flow with a terminal stage and the customer's decision."""

        def _update() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE code_save_flow SET stage = ?, decision = ?, resolved_at = ? "
                    "WHERE todo_id = ?",
                    (stage, decision, _now(), todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_update)

    async def fail_flow(self, todo_id: int, error: str) -> None:
        """Mark a flow as errored, leaving it for inspection."""

        def _update() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE code_save_flow SET stage = ?, error = ? WHERE todo_id = ?",
                    (FLOW_ERROR, error, todo_id),
                )

        async with self._lock:
            await asyncio.to_thread(_update)

    async def child_todo_ids(self) -> set[int]:
        """Return every under-to-do id created by the code-save flow.

        The poller skips these so the "save/discard" prompts are never ingested
        and driven through claude as if they were real tasks.
        """

        def _query() -> set[int]:
            rows = self.connection.execute(
                "SELECT save_todo_id, discard_todo_id FROM code_save_flow"
            ).fetchall()
            ids: set[int] = set()
            for save_id, discard_id in rows:
                if save_id is not None:
                    ids.add(int(save_id))
                if discard_id is not None:
                    ids.add(int(discard_id))
            return ids

        async with self._lock:
            return await asyncio.to_thread(_query)
