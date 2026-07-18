"""Async-friendly SQLite persistence for de-duplicating processed to-dos.

The standard library ``sqlite3`` module is synchronous, so every call is run in
a worker thread via :func:`asyncio.to_thread`. A single connection is shared and
guarded by an :class:`asyncio.Lock`, which both serialises access (sqlite3
connections are not safe for concurrent use) and keeps writes ordered.
"""

from __future__ import annotations

import asyncio
import sqlite3
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
                        "(todo_id, title, status, accepted_at) VALUES (?, ?, ?, ?)",
                        (todo.id, todo.title, STATUS_CLAIMED, _now()),
                    )
                return True
            except sqlite3.IntegrityError:
                return False

        async with self._lock:
            return await asyncio.to_thread(_claim)

    async def pending_todos(self) -> list[tuple[int, str, str]]:
        """Return interrupted to-dos (todo_id, title, status) to resume."""

        def _pending() -> list[tuple[int, str, str]]:
            placeholders = ", ".join("?" for _ in _PENDING_STATUSES)
            rows = self.connection.execute(
                f"SELECT todo_id, title, status FROM processed_todos "
                f"WHERE status IN ({placeholders}) ORDER BY todo_id",
                _PENDING_STATUSES,
            ).fetchall()
            return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

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

    async def mark_done(self, todo_id: int) -> None:
        """Mark a claimed to-do as successfully completed."""

        def _done() -> None:
            with self.connection as conn:
                conn.execute(
                    "UPDATE processed_todos SET status = ?, completed_at = ?, error = NULL "
                    "WHERE todo_id = ?",
                    (STATUS_DONE, _now(), todo_id),
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
