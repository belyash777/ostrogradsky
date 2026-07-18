"""Tests for the polling/dispatch loop."""

from __future__ import annotations

import asyncio

import pytest

from bcworker.basecamp import Todo
from bcworker.config import ACCEPTED_MESSAGE, COMPLETED_MESSAGE, Config
from bcworker.db import (
    STATUS_ACCEPTED,
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_ERROR,
    Database,
)
from bcworker.poller import Poller


class FakeClient:
    """In-memory stand-in for BasecampClient that records posted comments."""

    def __init__(self, todos: list[Todo], *, authenticated: bool = True):
        self._todos = todos
        self.authenticated = authenticated
        self.account_id = "123"
        self.comments: list[tuple[int, str]] = []
        self.fail_comment_on: set[int] = set()

    async def is_authenticated(self) -> bool:
        return self.authenticated

    async def ensure_account(self) -> str:
        return self.account_id

    async def assigned_todos(self) -> list[Todo]:
        return list(self._todos)

    async def create_comment(self, todo_id: int, text: str) -> None:
        if todo_id in self.fail_comment_on:
            raise RuntimeError("comment failed")
        self.comments.append((todo_id, text))


def _status(db: Database, todo_id: int) -> str | None:
    row = db.connection.execute(
        "SELECT status FROM processed_todos WHERE todo_id = ?", (todo_id,)
    ).fetchone()
    return row[0] if row else None


def _make_poller(db: Database, client: FakeClient) -> Poller:
    return Poller(Config(poll_interval_seconds=1), client, db)


async def test_processes_new_todos_in_order(
    db: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeClient([Todo(id=1, title="A"), Todo(id=2, title="B")])
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())

    # Each todo gets an accepted then a completed comment.
    assert client.comments == [
        (1, ACCEPTED_MESSAGE),
        (1, COMPLETED_MESSAGE),
        (2, ACCEPTED_MESSAGE),
        (2, COMPLETED_MESSAGE),
    ]
    assert capsys.readouterr().out.count("hello world") == 2
    assert _status(db, 1) == STATUS_DONE
    assert _status(db, 2) == STATUS_DONE


async def test_does_not_reprocess_seen_todos(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A")])
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())
    await poller._poll_once(asyncio.Event())  # same todo returned again

    assert client.comments == [(1, ACCEPTED_MESSAGE), (1, COMPLETED_MESSAGE)]


async def test_error_is_recorded_and_loop_continues(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A"), Todo(id=2, title="B")])
    client.fail_comment_on = {1}
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())

    assert _status(db, 1) == STATUS_ERROR
    assert _status(db, 2) == STATUS_DONE
    assert (2, ACCEPTED_MESSAGE) in client.comments
    assert (2, COMPLETED_MESSAGE) in client.comments


async def test_failed_todo_is_not_retried(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A")])
    client.fail_comment_on = {1}
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())
    client.fail_comment_on = set()
    await poller._poll_once(asyncio.Event())  # claim guard keeps it from being retried

    assert _status(db, 1) == STATUS_ERROR
    assert client.comments == []  # never succeeded, never retried


async def test_poll_once_stops_between_todos(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A"), Todo(id=2, title="B")])
    poller = _make_poller(db, client)
    stop = asyncio.Event()
    stop.set()  # already stopped

    await poller._poll_once(stop)

    # No to-do processed because stop was observed before the first one.
    assert client.comments == []
    assert _status(db, 1) is None


async def test_recover_resumes_accepted_todo_without_reposting_acceptance(
    db: Database,
) -> None:
    # Simulate a crash after the acceptance comment but before completion.
    await db.try_claim(Todo(id=9, title="Interrupted"))
    await db.mark_accepted(9)

    client = FakeClient([])
    poller = _make_poller(db, client)

    await poller._recover_pending(asyncio.Event())

    # Only the completion comment is posted on resume (acceptance already sent).
    assert client.comments == [(9, COMPLETED_MESSAGE)]
    assert _status(db, 9) == STATUS_DONE


async def test_recover_resumes_claimed_todo_with_full_sequence(db: Database) -> None:
    # Crash right after claim, before the acceptance comment.
    await db.try_claim(Todo(id=8, title="Barely started"))
    assert _status(db, 8) == STATUS_CLAIMED

    client = FakeClient([])
    poller = _make_poller(db, client)

    await poller._recover_pending(asyncio.Event())

    assert client.comments == [(8, ACCEPTED_MESSAGE), (8, COMPLETED_MESSAGE)]
    assert _status(db, 8) == STATUS_DONE


async def test_wait_until_ready_blocks_until_authenticated(db: Database) -> None:
    client = FakeClient([], authenticated=False)
    poller = _make_poller(db, client)
    stop = asyncio.Event()

    async def flip() -> None:
        await asyncio.sleep(0.05)
        client.authenticated = True

    asyncio.create_task(flip())
    assert await asyncio.wait_for(poller._wait_until_ready(stop), timeout=2) is True


async def test_wait_until_ready_returns_false_when_stopped(db: Database) -> None:
    client = FakeClient([], authenticated=False)
    poller = _make_poller(db, client)
    stop = asyncio.Event()
    stop.set()
    assert await poller._wait_until_ready(stop) is False


async def test_run_stops_on_event(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A")])
    poller = _make_poller(db, client)
    stop = asyncio.Event()

    task = asyncio.create_task(poller.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert _status(db, 1) == STATUS_DONE
