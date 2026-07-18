"""Tests for follow-up edits driven by new customer comments."""

from __future__ import annotations

from bcworker.basecamp import Todo
from bcworker.claude_runner import ClaudeError, session_id_for
from bcworker.config import Config
from bcworker.db import Database
from bcworker.followup import FollowupManager

PROJECT = 55
WORKER_ID = "42"
CUSTOMER_ID = "99"


class FakeClient:
    def __init__(self) -> None:
        self.comments: dict[int, list[dict]] = {}
        self.posted: list[tuple[int, str, int | None]] = []

    async def auth_status(self) -> dict:
        return {"authenticated": True, "user_id": WORKER_ID}

    async def list_comments(self, todo_id: int, project_id: int) -> list[dict]:
        return list(self.comments.get(todo_id, []))

    async def create_comment(self, todo_id: int, text: str, project_id: int | None = None) -> None:
        self.posted.append((todo_id, text, project_id))


class FakeRunner:
    def __init__(self, reply: str = "updated result", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.resumed: list[tuple[str, str]] = []

    async def resume(self, session_id: str, instruction: str) -> str:
        self.resumed.append((session_id, instruction))
        if self.fail:
            raise ClaudeError("resume failed")
        return self.reply


def _comment(cid: int, creator: str, content: str) -> dict:
    return {"id": cid, "creator": {"id": creator}, "content": content}


async def _seed_done(db: Database, todo_id: int) -> None:
    await db.try_claim(Todo(id=todo_id, title="T", bucket_id=PROJECT))
    await db.mark_accepted(todo_id)
    await db.mark_done(todo_id)


def _manager(db: Database, client: FakeClient, runner: FakeRunner) -> FollowupManager:
    return FollowupManager(client, db, runner, Config(basecamp_project_id=PROJECT))


async def _last_comment_id(db: Database, todo_id: int) -> int | None:
    row = db.connection.execute(
        "SELECT last_comment_id FROM processed_todos WHERE todo_id = ?", (todo_id,)
    ).fetchone()
    return row[0]


async def test_first_tick_sets_baseline_without_acting(db: Database) -> None:
    client = FakeClient()
    client.comments[1] = [
        _comment(1, WORKER_ID, "Задача прийнята"),
        _comment(2, WORKER_ID, "Result"),
    ]
    runner = FakeRunner()
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)

    await mgr.tick()

    assert runner.resumed == []
    assert await _last_comment_id(db, 1) == 2


async def test_new_customer_comment_triggers_resume(db: Database) -> None:
    client = FakeClient()
    client.comments[1] = [_comment(1, WORKER_ID, "Result")]
    runner = FakeRunner("new answer")
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)
    await mgr.tick()  # baseline -> last_comment_id = 1

    client.comments[1].append(_comment(2, CUSTOMER_ID, "add a chart please"))
    await mgr.tick()

    assert len(runner.resumed) == 1
    # The SAME deterministic session as the original task is resumed.
    assert runner.resumed[0][0] == session_id_for(1)
    assert runner.resumed[0][1] == "add a chart please"
    assert client.posted == [(1, "new answer", PROJECT)]
    assert await _last_comment_id(db, 1) == 2


async def test_customer_comment_in_race_window_is_not_lost(db: Database) -> None:
    # Customer commented between task completion and the first follow-up poll:
    # baseline is the worker's own last comment, so the edit is still processed.
    client = FakeClient()
    client.comments[1] = [
        _comment(1, WORKER_ID, "Result"),
        _comment(2, CUSTOMER_ID, "one more metric"),
    ]
    runner = FakeRunner("v2")
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)

    await mgr.tick()  # very first sighting

    assert runner.resumed and runner.resumed[0][1] == "one more metric"
    assert client.posted == [(1, "v2", PROJECT)]


async def test_resume_failure_advances_baseline_without_loop(db: Database) -> None:
    client = FakeClient()
    client.comments[1] = [_comment(1, WORKER_ID, "Result")]
    runner = FakeRunner(fail=True)
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)
    await mgr.tick()

    client.comments[1].append(_comment(2, CUSTOMER_ID, "please change X"))
    await mgr.tick()  # resume raises

    assert len(runner.resumed) == 1
    assert client.posted == []  # nothing posted on failure
    assert await _last_comment_id(db, 1) == 2  # advanced, so it will not loop
    await mgr.tick()
    assert len(runner.resumed) == 1  # not retried


async def test_worker_own_comment_is_ignored(db: Database) -> None:
    client = FakeClient()
    client.comments[1] = [_comment(1, WORKER_ID, "Result")]
    runner = FakeRunner()
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)
    await mgr.tick()

    # A newer comment, but authored by the worker itself.
    client.comments[1].append(_comment(2, WORKER_ID, "another worker note"))
    await mgr.tick()

    assert runner.resumed == []
    assert await _last_comment_id(db, 1) == 2  # advanced past its own comment


async def test_no_comments_is_noop(db: Database) -> None:
    client = FakeClient()
    runner = FakeRunner()
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)

    await mgr.tick()

    assert runner.resumed == []
    assert await _last_comment_id(db, 1) is None
