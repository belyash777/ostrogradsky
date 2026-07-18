"""Tests for the async SQLite persistence layer."""

from __future__ import annotations

from pathlib import Path

from bcworker.basecamp import Todo
from bcworker.db import (
    STATUS_ACCEPTED,
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_ERROR,
    Database,
)


def _row(db: Database, todo_id: int) -> tuple:
    return db.connection.execute(
        "SELECT status, error, completed_at FROM processed_todos WHERE todo_id = ?",
        (todo_id,),
    ).fetchone()


async def test_try_claim_first_time_succeeds(db: Database) -> None:
    assert await db.try_claim(Todo(id=1, title="A")) is True
    status, error, completed = _row(db, 1)
    assert status == STATUS_CLAIMED
    assert error is None
    assert completed is None


async def test_mark_accepted(db: Database) -> None:
    await db.try_claim(Todo(id=5, title="E"))
    await db.mark_accepted(5)
    status, _, _ = _row(db, 5)
    assert status == STATUS_ACCEPTED


async def test_pending_todos_lists_only_active(db: Database) -> None:
    await db.try_claim(Todo(id=1, title="claimed"))
    await db.try_claim(Todo(id=2, title="accepted"))
    await db.mark_accepted(2)
    await db.try_claim(Todo(id=3, title="done"))
    await db.mark_done(3)
    await db.try_claim(Todo(id=4, title="errored"))
    await db.mark_error(4, "boom")

    pending = await db.pending_todos()
    assert pending == [(1, "claimed", STATUS_CLAIMED), (2, "accepted", STATUS_ACCEPTED)]


async def test_try_claim_is_idempotent(db: Database) -> None:
    assert await db.try_claim(Todo(id=7, title="First")) is True
    # A second claim (even with a different title) must not win.
    assert await db.try_claim(Todo(id=7, title="Second")) is False
    (title,) = db.connection.execute(
        "SELECT title FROM processed_todos WHERE todo_id = 7"
    ).fetchone()
    assert title == "First"


async def test_mark_done(db: Database) -> None:
    await db.try_claim(Todo(id=2, title="B"))
    await db.mark_done(2)
    status, error, completed = _row(db, 2)
    assert status == STATUS_DONE
    assert error is None
    assert completed is not None


async def test_mark_error(db: Database) -> None:
    await db.try_claim(Todo(id=3, title="C"))
    await db.mark_error(3, "boom")
    status, error, _ = _row(db, 3)
    assert status == STATUS_ERROR
    assert error == "boom"


async def test_dedup_survives_reconnect(tmp_path: Path) -> None:
    path = tmp_path / "persist.sqlite3"
    first = Database(path)
    await first.connect()
    await first.migrate()
    assert await first.try_claim(Todo(id=99, title="X")) is True
    await first.close()

    second = Database(path)
    await second.connect()
    await second.migrate()  # idempotent
    assert await second.try_claim(Todo(id=99, title="X")) is False
    await second.close()


async def test_connection_before_connect_raises(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.sqlite3")
    try:
        db.connection  # noqa: B018 - property access is the behaviour under test
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")
