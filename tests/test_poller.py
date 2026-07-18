"""Tests for the polling/dispatch loop."""

from __future__ import annotations

import asyncio

from bcworker.basecamp import Todo
from bcworker.config import ACCEPTED_MESSAGE, COMPLETED_MESSAGE, Config
from bcworker.db import (
    STATUS_DONE,
    STATUS_ERROR,
    Database,
)
from bcworker.poller import Poller

RESULT = "computed answer"


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

    async def create_comment(self, todo_id: int, text: str, project_id: int | None = None) -> None:
        if todo_id in self.fail_comment_on:
            raise RuntimeError("comment failed")
        self.comments.append((todo_id, text))


class FakeRunner:
    def __init__(self, result: str = RESULT):
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def run_task(self, task_text: str, session_id: str) -> str:
        self.calls.append((task_text, session_id))
        return self.result

    async def resume(self, session_id: str, instruction: str) -> str:  # pragma: no cover
        return self.result


class NoopSyncer:
    async def sync_project(self, project_id: int) -> None: ...
    async def sync_claude_md(self, project_id: int) -> None: ...


class CountingManager:
    def __init__(self) -> None:
        self.ticks = 0

    async def tick(self, *args: object) -> None:
        self.ticks += 1


def _status(db: Database, todo_id: int) -> str | None:
    row = db.connection.execute(
        "SELECT status FROM processed_todos WHERE todo_id = ?", (todo_id,)
    ).fetchone()
    return row[0] if row else None


def _make_poller(
    db: Database,
    client: FakeClient,
    *,
    runner: FakeRunner | None = None,
    followup: CountingManager | None = None,
    codesave: CountingManager | None = None,
    project_id: int = 0,
) -> Poller:
    return Poller(
        Config(poll_interval_seconds=1, basecamp_project_id=project_id),
        client,
        db,
        runner or FakeRunner(),
        NoopSyncer(),
        codesave or CountingManager(),
        followup or CountingManager(),
    )


async def test_processes_new_todos_in_order(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A"), Todo(id=2, title="B")])
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())
    await poller._await_inflight()

    assert client.comments == [
        (1, ACCEPTED_MESSAGE),
        (1, RESULT),
        (2, ACCEPTED_MESSAGE),
        (2, RESULT),
    ]
    assert _status(db, 1) == STATUS_DONE
    assert _status(db, 2) == STATUS_DONE


async def test_does_not_reprocess_seen_todos(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A")])
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())
    await poller._await_inflight()
    await poller._poll_once(asyncio.Event())  # same todo returned again
    await poller._await_inflight()

    assert client.comments == [(1, ACCEPTED_MESSAGE), (1, RESULT)]


async def test_error_is_recorded_and_loop_continues(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A"), Todo(id=2, title="B")])
    client.fail_comment_on = {1}
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())
    await poller._await_inflight()

    assert _status(db, 1) == STATUS_ERROR
    assert _status(db, 2) == STATUS_DONE
    assert (2, ACCEPTED_MESSAGE) in client.comments
    assert (2, RESULT) in client.comments


async def test_failed_todo_is_not_retried(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A")])
    client.fail_comment_on = {1}
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())
    await poller._await_inflight()
    client.fail_comment_on = set()
    await poller._poll_once(asyncio.Event())  # claim guard keeps it from being retried
    await poller._await_inflight()

    assert _status(db, 1) == STATUS_ERROR
    assert client.comments == []


async def test_poll_once_stops_between_todos(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A"), Todo(id=2, title="B")])
    poller = _make_poller(db, client)
    stop = asyncio.Event()
    stop.set()

    await poller._poll_once(stop)
    await poller._await_inflight()

    assert client.comments == []
    assert _status(db, 1) is None


async def test_result_is_posted_as_completion(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A", description="count rows")])
    runner = FakeRunner("42 rows")
    poller = _make_poller(db, client, runner=runner)

    await poller._poll_once(asyncio.Event())
    await poller._await_inflight()

    assert (1, "42 rows") in client.comments
    # The full task text (title + body) reaches claude.
    assert runner.calls[0][0] == "A\n\ncount rows"


async def test_filters_to_configured_project(db: Database) -> None:
    client = FakeClient(
        [Todo(id=1, title="mine", bucket_id=55), Todo(id=2, title="other", bucket_id=99)]
    )
    poller = _make_poller(db, client, project_id=55)

    await poller._poll_once(asyncio.Event())
    await poller._await_inflight()

    assert _status(db, 1) == STATUS_DONE
    assert _status(db, 2) is None  # dropped: different project


async def test_empty_result_falls_back_to_completed_message(db: Database) -> None:
    client = FakeClient([Todo(id=1, title="A")])
    poller = _make_poller(db, client, runner=FakeRunner(""))

    await poller._poll_once(asyncio.Event())
    await poller._await_inflight()

    assert (1, COMPLETED_MESSAGE) in client.comments


async def test_skips_code_save_child_todos(db: Database) -> None:
    # Register two under-to-dos via a code-save flow; they must never be ingested.
    await db.create_flow(9, 55, "sid", "2026-01-01T00:00:00+00:00")
    await db.set_flow_prompts(9, 501, 502)
    client = FakeClient([Todo(id=501, title="Зберегти код")])
    poller = _make_poller(db, client)

    await poller._poll_once(asyncio.Event())
    await poller._await_inflight()

    assert _status(db, 501) is None
    assert client.comments == []


async def test_periodic_concerns_are_cadence_gated(db: Database) -> None:
    client = FakeClient([])
    followup = CountingManager()
    poller = _make_poller(db, client, followup=followup)

    await poller._poll_once(asyncio.Event())
    await poller._poll_once(asyncio.Event())  # within the poll interval

    assert followup.ticks == 1  # second tick gated by comment_poll_seconds


async def test_recover_resumes_accepted_todo_without_reposting_acceptance(
    db: Database,
) -> None:
    await db.try_claim(Todo(id=9, title="Interrupted"))
    await db.mark_accepted(9)

    client = FakeClient([])
    poller = _make_poller(db, client)

    await poller._recover_pending(asyncio.Event())

    assert client.comments == [(9, RESULT)]
    assert _status(db, 9) == STATUS_DONE


async def test_recover_resumes_claimed_todo_with_full_sequence(db: Database) -> None:
    await db.try_claim(Todo(id=8, title="Barely started"))

    client = FakeClient([])
    poller = _make_poller(db, client)

    await poller._recover_pending(asyncio.Event())

    assert client.comments == [(8, ACCEPTED_MESSAGE), (8, RESULT)]
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
